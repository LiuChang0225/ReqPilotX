"""Professional finalization gate for object-batched parent recognition.

This module deliberately sits after object execution and global Host compilation.
It cannot recover evidence, run a model, or reconcile adjacent objects.  Its only
authority is to prove that one successful aggregate is the exact aggregate that
the canonical session, Agent Kit policy closure, and existing parent-provision
validator are about to publish.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalAppServerDocumentSessionAttestationV1,
    CanonicalAppServerStageActivation,
)
from reqpilotx_server.adapters.agents.codex_app_server.d3_stage_input_workspace import (
    D3StageInputBundleAttestationV1,
)
from reqpilotx_server.adapters.agents.codex_app_server.d3_region_stage_input_workspace import (
    D3RegionStageInputWorkspaceManifestV1,
)
from reqpilotx_server.adapters.agents.d3_parent_provision_attestation import (
    compile_d3_parent_provision_policy_closure,
    compile_d3_parent_provision_runner_authority,
)
from reqpilotx_server.adapters.agents.portable_kit import PortableAgentKit
from reqpilotx_server.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
    attest_d3_parent_provision_policy,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provision_compiler import (
    D3HostCompiledParentProvisionDecisionV1,
    D3ParentBoundaryClassificationCandidateV2,
    compile_d3_parent_provision_decision,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
    PARENT_PROVISION_VALIDATOR_ID,
    PARENT_PROVISION_VALIDATOR_VERSION,
    D3ExactSourceSegmentV1,
    D3ValidatedD2ArtifactIdentityV1,
    ParentProvisionProposalV3,
    ParentProvisionStatus,
    Requirementness,
    ParentProvisionValidationReceiptV3,
    build_professional_policy_attestation_identity,
    canonical_json_sha256,
    parent_provision_evidence_refs,
    validate_parent_provision_proposal,
)
from reqpilotx_server.application.agent_workflow.d3_region_stage_input import (
    D3UnifiedExactSourceSegmentSetV3,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure_v2 import (
    D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AGENT_BASELINE_STAGE_ORDER,
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentRunnerIdentityV1,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
    AgentStageFinalReceiptV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionBatchStatus,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectExecutionMode,
)
from reqpilotx_server.application.agent_workflow.parent_provision_nested_evidence import (
    ParentProvisionNestedEvidenceAssignmentClosureV1,
    ParentProvisionNestedEvidenceAssignmentStatus,
    ParentProvisionNestedEvidenceEligibilityV1,
    ParentProvisionNestedEvidenceError,
    build_nested_evidence_eligibilities,
    build_parent_provision_nested_evidence_assignment_closure,
    validate_parent_provision_nested_evidence_assignment_closure,
)


PARENT_PROVISION_AGGREGATE_PROFESSIONAL_PROOF_SCHEMA_VERSION = (
    "parent-provision-aggregate-professional-proof/v1"
)
PARENT_PROVISION_RECOGNITION_FINALIZED_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-recognition-finalized-execution-receipt/v1"
)


class ParentProvisionProfessionalAggregateError(ValueError):
    """Fail-closed professional-finalization boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionRecognitionFinalizationMode(StrEnum):
    DIRECT = "DIRECT"
    RECONCILED = "RECONCILED"


class ParentProvisionRecognitionFinalizedExecutionReceiptV1(_StrictFrozenModel):
    """Successful execution closure after optional cross-object reconciliation."""

    schema_version: Literal[
        "parent-provision-recognition-finalized-execution-receipt/v1"
    ] = PARENT_PROVISION_RECOGNITION_FINALIZED_EXECUTION_RECEIPT_SCHEMA_VERSION
    execution_id: str = Field(
        pattern=r"^parent-provision-recognition-finalized-execution-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalization_mode: ParentProvisionRecognitionFinalizationMode
    initial_aggregate_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_aggregate_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_batch_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replaced_batch_ids: tuple[str, ...] = ()
    replaced_initial_batch_receipt_sha256s: tuple[str, ...] = ()
    replaced_initial_batches_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reconciliation_execution_receipts_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    global_aggregate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    global_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: Literal[
        ParentProvisionRecognitionAggregateStatus.COMPLETE,
        ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
    ]
    semantic_unresolved_scope_count: int = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: int = Field(ge=0, le=65536)
    predecessor_partial: bool
    nested_evidence_assignment_closure_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )
    final_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_finalized_execution(
        self,
    ) -> ParentProvisionRecognitionFinalizedExecutionReceiptV1:
        if len(self.replaced_batch_ids) != len(
            self.replaced_initial_batch_receipt_sha256s
        ) or len(self.replaced_batch_ids) != len(set(self.replaced_batch_ids)):
            raise ValueError("replaced batch receipt bindings are incomplete")
        if any(
            re.fullmatch(r"parent-provision-object-batch-[0-9a-f]{32}", item) is None
            for item in self.replaced_batch_ids
        ):
            raise ValueError("replaced batch identity is invalid")
        expected_replaced_root = canonical_json_sha256(
            self.replaced_initial_batch_receipt_sha256s
        )
        if self.replaced_initial_batches_sha256 != expected_replaced_root:
            raise ValueError("replaced initial batch receipt root drifted")
        reconciliation_fields = (
            self.reconciliation_plan_sha256,
            self.reconciliation_execution_receipts_sha256,
        )
        if self.finalization_mode is ParentProvisionRecognitionFinalizationMode.DIRECT:
            if (
                self.replaced_batch_ids
                or self.replaced_initial_batch_receipt_sha256s
                or any(reconciliation_fields)
            ):
                raise ValueError("direct finalization cannot claim reconciliation")
        elif not self.replaced_batch_ids or not all(reconciliation_fields):
            raise ValueError(
                "reconciled finalization requires plan and execution receipt roots"
            )
        semantically_complete = not (
            self.semantic_unresolved_scope_count
            or self.semantic_unresolved_entry_count
            or self.predecessor_partial
        )
        if (
            self.status is ParentProvisionRecognitionAggregateStatus.COMPLETE
        ) != semantically_complete:
            raise ValueError(
                "finalized status does not match semantic unresolved scope"
            )
        trace_payload = {
            "run_id": self.run_id,
            "source_sha256": self.source_sha256,
            "predecessor_artifact_sha256": self.predecessor_artifact_sha256,
            "kit_bundle_sha256": self.kit_bundle_sha256,
            "policy_closure_sha256": self.policy_closure_sha256,
            "runner_profile_sha256": self.runner_profile_sha256,
            "model_id": self.model_id,
            "reasoning_effort": self.reasoning_effort,
            "plan_id": self.plan_id,
            "plan_sha256": self.plan_sha256,
            "finalization_mode": self.finalization_mode.value,
            "initial_aggregate_execution_receipt_sha256": (
                self.initial_aggregate_execution_receipt_sha256
            ),
            "initial_aggregate_execution_trace_sha256": (
                self.initial_aggregate_execution_trace_sha256
            ),
            "initial_batch_receipts_sha256": self.initial_batch_receipts_sha256,
            "replaced_batch_ids": self.replaced_batch_ids,
            "replaced_initial_batches_sha256": (self.replaced_initial_batches_sha256),
            "reconciliation_plan_sha256": self.reconciliation_plan_sha256,
            "reconciliation_execution_receipts_sha256": (
                self.reconciliation_execution_receipts_sha256
            ),
            "global_aggregate_sha256": self.global_aggregate_sha256,
            "global_candidate_sha256": self.global_candidate_sha256,
            "parent_provision_decision_sha256": (self.parent_provision_decision_sha256),
            "evidence_refs_sha256": self.evidence_refs_sha256,
            "status": self.status.value,
            "semantic_unresolved_scope_count": self.semantic_unresolved_scope_count,
            "semantic_unresolved_entry_count": self.semantic_unresolved_entry_count,
            "predecessor_partial": self.predecessor_partial,
        }
        if self.nested_evidence_assignment_closure_sha256 is not None:
            trace_payload["nested_evidence_assignment_closure_sha256"] = (
                self.nested_evidence_assignment_closure_sha256
            )
        if self.final_execution_trace_sha256 != canonical_json_sha256(trace_payload):
            raise ValueError("finalized execution trace drifted")
        payload = self.model_dump(
            mode="json",
            exclude={"execution_id", "receipt_sha256"},
        )
        expected = canonical_json_sha256(payload)
        if self.receipt_sha256 != expected:
            raise ValueError("finalized execution receipt SHA drifted")
        if self.execution_id != (
            f"parent-provision-recognition-finalized-execution-{expected[:32]}"
        ):
            raise ValueError("finalized execution identity drifted")
        return self


class ParentProvisionAggregateProfessionalProofV1(_StrictFrozenModel):
    """Content-free root that joins batch execution to professional policy."""

    schema_version: Literal["parent-provision-aggregate-professional-proof/v1"] = (
        PARENT_PROVISION_AGGREGATE_PROFESSIONAL_PROOF_SCHEMA_VERSION
    )
    proof_id: str = Field(
        pattern=r"^parent-provision-aggregate-professional-proof-[0-9a-f]{32}$"
    )
    proof_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal[AgentExtractionStage.PARENT_PROVISION_RECOGNITION] = (
        AgentExtractionStage.PARENT_PROVISION_RECOGNITION
    )
    kit_identity: AgentKitIdentityV1
    runner_identity: AgentRunnerIdentityV1
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_aggregate_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_aggregate_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    finalized_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_aggregate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_global_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    nested_evidence_assignment_closure_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_proof(self) -> ParentProvisionAggregateProfessionalProofV1:
        payload = self.model_dump(
            mode="json",
            exclude={"proof_id", "proof_sha256"},
        )
        expected = canonical_json_sha256(payload)
        if self.proof_sha256 != expected:
            raise ValueError("aggregate professional proof SHA drifted")
        if self.proof_id != (
            f"parent-provision-aggregate-professional-proof-{expected[:32]}"
        ):
            raise ValueError("aggregate professional proof identity drifted")
        return self


@dataclass(frozen=True, slots=True)
class ParentProvisionProfessionalAggregateResult:
    professional_proof: ParentProvisionAggregateProfessionalProofV1
    compiled_decision: D3HostCompiledParentProvisionDecisionV1
    professional_execution_receipt: D3ParentProvisionExecutionReceiptV1
    professional_policy_attestation: D3ParentProvisionPolicyAttestationV1
    proposal: ParentProvisionProposalV3
    host_validation_receipt: ParentProvisionValidationReceiptV3
    stage_artifact: AgentStageArtifactV1
    final_receipt: AgentStageFinalReceiptV1
    nested_evidence_assignment_closure: (
        ParentProvisionNestedEvidenceAssignmentClosureV1 | None
    ) = None


def build_parent_provision_recognition_finalized_execution_receipt(
    *,
    initial_aggregate_execution_receipt: (
        ParentProvisionRecognitionAggregateExecutionReceiptV2
    ),
    global_aggregate_sha256: str,
    global_candidate_sha256: str,
    parent_provision_decision_sha256: str,
    evidence_refs_sha256: str,
    status: ParentProvisionRecognitionAggregateStatus,
    semantic_unresolved_scope_count: int,
    semantic_unresolved_entry_count: int = 0,
    predecessor_partial: bool = False,
    nested_evidence_assignment_closure: (
        ParentProvisionNestedEvidenceAssignmentClosureV1 | None
    ) = None,
    replaced_batch_ids: tuple[str, ...] = (),
    reconciliation_plan_sha256: str | None = None,
    reconciliation_execution_receipts_sha256: str | None = None,
) -> ParentProvisionRecognitionFinalizedExecutionReceiptV1:
    """Close a direct or reconciled aggregate without erasing initial execution."""

    try:
        initial = ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
            initial_aggregate_execution_receipt.model_dump(mode="json")
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_INITIAL_AGGREGATE_RECEIPT_INVALID"
        ) from exc
    assignment_closure = _validated_nested_evidence_assignment_closure(
        nested_evidence_assignment_closure
    )
    if len(initial.batches) != initial.planned_batch_count:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_INITIAL_AGGREGATE_BATCH_SET_INCOMPLETE"
        )
    success_statuses = {
        ParentProvisionRecognitionAggregateStatus.COMPLETE,
        ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
    }
    nonaccepted = tuple(
        item
        for item in initial.batches
        if item.status
        not in {
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
        }
    )
    if initial.status in success_statuses:
        mode = ParentProvisionRecognitionFinalizationMode.DIRECT
        if nonaccepted:
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_DIRECT_FINALIZATION_HAS_NONACCEPTED_BATCH"
            )
        if any((reconciliation_plan_sha256, reconciliation_execution_receipts_sha256)):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_DIRECT_FINALIZATION_RECONCILIATION_FORBIDDEN"
            )
        if (
            initial.object_batch_aggregate_sha256 != global_aggregate_sha256
            or initial.decision_candidate_sha256 != global_candidate_sha256
            or initial.parent_provision_decision_sha256
            != parent_provision_decision_sha256
            or initial.evidence_refs_sha256 != evidence_refs_sha256
            or initial.status is not status
            or initial.semantic_unresolved_scope_count
            != semantic_unresolved_scope_count
            or getattr(initial, "semantic_unresolved_entry_count", 0)
            != semantic_unresolved_entry_count
            or getattr(initial, "predecessor_partial", False) != predecessor_partial
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_DIRECT_FINAL_HASH_DRIFT"
            )
        replaced_batches = ()
    else:
        mode = ParentProvisionRecognitionFinalizationMode.RECONCILED
        if not nonaccepted or any(
            item.status
            is not ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
            for item in nonaccepted
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_INPUT_CONTAINS_EXECUTION_FAILURE"
            )
        if not all(
            (reconciliation_plan_sha256, reconciliation_execution_receipts_sha256)
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_EXECUTION_PROOF_MISSING"
            )
        batch_by_id = {item.batch_id: item for item in initial.batches}
        if (
            not replaced_batch_ids
            or len(replaced_batch_ids) != len(set(replaced_batch_ids))
            or any(item not in batch_by_id for item in replaced_batch_ids)
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_REPLACEMENT_SCOPE_INVALID"
            )
        replaced_batches = tuple(batch_by_id[item] for item in replaced_batch_ids)
        if tuple(item.batch_order for item in replaced_batches) != tuple(
            sorted(item.batch_order for item in replaced_batches)
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_REPLACEMENT_ORDER_INVALID"
            )
        marked_ids = {item.batch_id for item in nonaccepted}
        replacement_ids = set(replaced_batch_ids)
        if not marked_ids.issubset(replacement_ids) or any(
            item.batch_id not in marked_ids
            and item.status
            not in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
            for item in replaced_batches
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_REPLACEMENT_SCOPE_INVALID"
            )
    if status not in success_statuses:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_FINALIZED_STATUS_NOT_SUCCESSFUL"
        )
    if assignment_closure is not None:
        if (
            assignment_closure.status
            is not ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_INCOMPLETE"
            )
        if (
            assignment_closure.run_id != initial.run_id
            or assignment_closure.source_sha256 != initial.source_sha256
            or assignment_closure.predecessor_artifact_id
            != initial.predecessor_artifact_id
            or assignment_closure.predecessor_artifact_sha256
            != initial.predecessor_artifact_sha256
            or assignment_closure.parent_provision_decision_sha256
            != parent_provision_decision_sha256
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_LINEAGE_DRIFT"
            )
    for value, subject in (
        (global_aggregate_sha256, "GLOBAL_AGGREGATE"),
        (global_candidate_sha256, "GLOBAL_CANDIDATE"),
        (parent_provision_decision_sha256, "PARENT_DECISION"),
        (evidence_refs_sha256, "EVIDENCE_REFS"),
    ):
        if re.fullmatch(r"[0-9a-f]{64}", value) is None:
            raise ParentProvisionProfessionalAggregateError(
                f"PARENT_PROVISION_{subject}_SHA_INVALID"
            )
    values = {
        "content_free": True,
        "run_id": initial.run_id,
        "source_sha256": initial.source_sha256,
        "predecessor_artifact_id": initial.predecessor_artifact_id,
        "predecessor_artifact_sha256": initial.predecessor_artifact_sha256,
        "kit_bundle_sha256": initial.kit_bundle_sha256,
        "policy_closure_sha256": initial.policy_closure_sha256,
        "runner_profile_sha256": initial.runner_profile_sha256,
        "model_id": initial.model_id,
        "reasoning_effort": initial.reasoning_effort,
        "plan_id": initial.plan_id,
        "plan_sha256": initial.plan_sha256,
        "finalization_mode": mode,
        "initial_aggregate_execution_receipt_sha256": initial.receipt_sha256,
        "initial_aggregate_execution_trace_sha256": (
            initial.aggregate_execution_trace_sha256
        ),
        "initial_batch_receipts_sha256": initial.batch_receipts_sha256,
        "replaced_batch_ids": tuple(item.batch_id for item in replaced_batches),
        "replaced_initial_batch_receipt_sha256s": tuple(
            item.receipt_sha256 for item in replaced_batches
        ),
        "replaced_initial_batches_sha256": canonical_json_sha256(
            tuple(item.receipt_sha256 for item in replaced_batches)
        ),
        "reconciliation_plan_sha256": reconciliation_plan_sha256,
        "reconciliation_execution_receipts_sha256": (
            reconciliation_execution_receipts_sha256
        ),
        "global_aggregate_sha256": global_aggregate_sha256,
        "global_candidate_sha256": global_candidate_sha256,
        "parent_provision_decision_sha256": parent_provision_decision_sha256,
        "evidence_refs_sha256": evidence_refs_sha256,
        "status": status,
        "semantic_unresolved_scope_count": semantic_unresolved_scope_count,
        "semantic_unresolved_entry_count": semantic_unresolved_entry_count,
        "predecessor_partial": predecessor_partial,
        "nested_evidence_assignment_closure_sha256": (
            assignment_closure.closure_sha256
            if assignment_closure is not None
            else None
        ),
    }
    trace_payload = {
        "run_id": initial.run_id,
        "source_sha256": initial.source_sha256,
        "predecessor_artifact_sha256": initial.predecessor_artifact_sha256,
        "kit_bundle_sha256": initial.kit_bundle_sha256,
        "policy_closure_sha256": initial.policy_closure_sha256,
        "runner_profile_sha256": initial.runner_profile_sha256,
        "model_id": initial.model_id,
        "reasoning_effort": initial.reasoning_effort,
        "plan_id": initial.plan_id,
        "plan_sha256": initial.plan_sha256,
        "finalization_mode": mode.value,
        "initial_aggregate_execution_receipt_sha256": initial.receipt_sha256,
        "initial_aggregate_execution_trace_sha256": (
            initial.aggregate_execution_trace_sha256
        ),
        "initial_batch_receipts_sha256": initial.batch_receipts_sha256,
        "replaced_batch_ids": values["replaced_batch_ids"],
        "replaced_initial_batches_sha256": values["replaced_initial_batches_sha256"],
        "reconciliation_plan_sha256": reconciliation_plan_sha256,
        "reconciliation_execution_receipts_sha256": (
            reconciliation_execution_receipts_sha256
        ),
        "global_aggregate_sha256": global_aggregate_sha256,
        "global_candidate_sha256": global_candidate_sha256,
        "parent_provision_decision_sha256": parent_provision_decision_sha256,
        "evidence_refs_sha256": evidence_refs_sha256,
        "status": status.value,
        "semantic_unresolved_scope_count": semantic_unresolved_scope_count,
        "semantic_unresolved_entry_count": semantic_unresolved_entry_count,
        "predecessor_partial": predecessor_partial,
    }
    if assignment_closure is not None:
        trace_payload["nested_evidence_assignment_closure_sha256"] = (
            assignment_closure.closure_sha256
        )
    values["final_execution_trace_sha256"] = canonical_json_sha256(trace_payload)
    draft = ParentProvisionRecognitionFinalizedExecutionReceiptV1.model_construct(
        execution_id="parent-provision-recognition-finalized-execution-" + "0" * 32,
        receipt_sha256="0" * 64,
        **values,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"execution_id", "receipt_sha256"})
    )
    return ParentProvisionRecognitionFinalizedExecutionReceiptV1(
        execution_id=(
            f"parent-provision-recognition-finalized-execution-{digest[:32]}"
        ),
        receipt_sha256=digest,
        **values,
    )


def finalize_parent_provision_professional_aggregate(
    *,
    kit: PortableAgentKit,
    activation: CanonicalAppServerStageActivation,
    plan: ParentProvisionObjectBatchPlanV1,
    initial_aggregate_execution_receipt: (
        ParentProvisionRecognitionAggregateExecutionReceiptV2
    ),
    finalized_execution_receipt: (
        ParentProvisionRecognitionFinalizedExecutionReceiptV1
    ),
    global_aggregate_sha256: str,
    global_candidate: D3ParentBoundaryClassificationCandidateV2,
    compiled_decision: D3HostCompiledParentProvisionDecisionV1,
    ordered_batch_result_sha256s: tuple[str, ...] | None = None,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    nested_evidence_assignment_closure: (
        ParentProvisionNestedEvidenceAssignmentClosureV1 | None
    ) = None,
) -> ParentProvisionProfessionalAggregateResult:
    """Publish one globally compiled aggregate, or reject it without an artifact."""

    try:
        session = CanonicalAppServerDocumentSessionAttestationV1.model_validate(
            activation.session_attestation.model_dump(mode="json")
        )
        stage_input = _validated_stage_input(activation)
        plan = ParentProvisionObjectBatchPlanV1.model_validate(
            plan.model_dump(mode="json")
        )
        initial_aggregate_execution_receipt = (
            ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
                initial_aggregate_execution_receipt.model_dump(mode="json")
            )
        )
        finalized_execution_receipt = (
            ParentProvisionRecognitionFinalizedExecutionReceiptV1.model_validate(
                finalized_execution_receipt.model_dump(mode="json")
            )
        )
        global_candidate = D3ParentBoundaryClassificationCandidateV2.model_validate(
            global_candidate.model_dump(mode="json")
        )
        compiled_decision = D3HostCompiledParentProvisionDecisionV1.model_validate(
            compiled_decision.model_dump(mode="json")
        )
        d2_identity = D3ValidatedD2ArtifactIdentityV1.model_validate(
            d2_identity.model_dump(mode="json")
        )
        source_segments = tuple(
            D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
            for item in source_segments
        )
        nested_evidence_assignment_closure = (
            _validated_nested_evidence_assignment_closure(
                nested_evidence_assignment_closure
            )
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_CONTRACT_INVALID"
        ) from exc

    closure = activation.d3_policy_closure
    if (
        activation.stage is not AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        or closure is None
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_PROFESSIONAL_POLICY_CLOSURE_MISSING"
        )
    try:
        expected_closure = compile_d3_parent_provision_policy_closure(kit)
        runner_authority = compile_d3_parent_provision_runner_authority(
            kit,
            session.runner_profile_id,
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_PROFESSIONAL_KIT_AUTHORITY_INVALID"
        ) from exc
    if closure != expected_closure:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_PROFESSIONAL_POLICY_CLOSURE_DRIFT"
        )

    session_sha256 = canonical_json_sha256(session.model_dump(mode="json"))
    policy_closure_sha256 = canonical_json_sha256(closure.model_dump(mode="json"))
    runner_identity = AgentRunnerIdentityV1(
        runner_profile_id=session.runner_profile_id,
        runner_profile_sha256=session.runner_profile_sha256,
        runner_kind=session.runner_kind,
        adapter_id=session.adapter_id,
        adapter_version=session.adapter_version,
        runtime_version=str(
            activation.runtime.protocol.get("codex_version", "unknown")
        ),
    )
    if (
        session.kit_identity != kit.identity
        or d2_identity.kit_identity != kit.identity
        or closure.kit_identity != kit.identity
        or session.d3_policy_closure_sha256 != policy_closure_sha256
        or session.runner_profile_sha256 != runner_authority.runner_profile_sha256
        or session.runner_kind is not runner_authority.runner_kind
        or session.adapter_id != runner_authority.adapter_id
        or session.adapter_version != runner_authority.adapter_version
        or session.model_id != runner_authority.model_id
        or session.reasoning_effort != runner_authority.reasoning_effort
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_PROFESSIONAL_SESSION_AUTHORITY_DRIFT"
        )

    _require_stage_input_lineage(
        activation=activation,
        session=session,
        stage_input=stage_input,
        plan=plan,
        d2_identity=d2_identity,
    )
    _require_aggregate_execution_eligibility(
        initial_receipt=initial_aggregate_execution_receipt,
        finalized_receipt=finalized_execution_receipt,
        plan=plan,
        activation=activation,
        session=session,
        session_sha256=session_sha256,
        policy_closure_sha256=policy_closure_sha256,
        global_aggregate_sha256=global_aggregate_sha256,
        ordered_batch_result_sha256s=ordered_batch_result_sha256s,
        d2_identity=d2_identity,
        nested_evidence_assignment_closure=nested_evidence_assignment_closure,
    )

    try:
        replayed_decision = compile_d3_parent_provision_decision(
            d2_identity=d2_identity,
            source_segments=source_segments,
            kit_identity=kit.identity,
            candidate=global_candidate,
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_GLOBAL_COMPILER_REPLAY_FAILED"
        ) from exc
    if replayed_decision != compiled_decision:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_GLOBAL_COMPILER_REPLAY_DRIFT"
        )
    decision = replayed_decision
    _require_nested_evidence_assignment_closure(
        activation=activation,
        stage_input=stage_input,
        decision=decision,
        source_segments=source_segments,
        finalized_receipt=finalized_execution_receipt,
        closure=nested_evidence_assignment_closure,
    )
    evidence_refs = parent_provision_evidence_refs(
        declared_logical_object_ids=decision.declared_logical_object_ids,
        unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
        entries=decision.entries,
    )
    evidence_refs_sha256 = canonical_json_sha256(evidence_refs)
    _require_final_hash_closure(
        finalized_receipt=finalized_execution_receipt,
        global_aggregate_sha256=global_aggregate_sha256,
        global_candidate=global_candidate,
        decision=decision,
        evidence_refs_sha256=evidence_refs_sha256,
    )

    proof = _build_professional_proof(
        activation=activation,
        session_sha256=session_sha256,
        policy_closure_sha256=policy_closure_sha256,
        runner_identity=runner_identity,
        plan=plan,
        initial_aggregate_execution_receipt=initial_aggregate_execution_receipt,
        finalized_execution_receipt=finalized_execution_receipt,
        global_aggregate_sha256=global_aggregate_sha256,
        global_candidate_sha256=global_candidate.candidate_sha256,
        decision=decision,
        evidence_refs_sha256=evidence_refs_sha256,
        nested_evidence_assignment_closure=nested_evidence_assignment_closure,
    )
    professional_execution = D3ParentProvisionExecutionReceiptV1(
        run_id=decision.run_id,
        source_sha256=decision.source_sha256,
        d2_artifact_id=decision.predecessor_d2_artifact_id,
        d2_artifact_sha256=decision.predecessor_d2_artifact_sha256,
        kit_identity=decision.kit_identity,
        agent_id=closure.agent_id,
        runner_identity=runner_identity,
        model_id=session.model_id,
        reasoning_effort=session.reasoning_effort,
        requested_stages=AGENT_BASELINE_STAGE_ORDER[:4],
        loaded_policy_closure=closure,
        stage_session_attestation_sha256=session_sha256,
        # The proof is the aggregate execution closure: it includes the raw
        # aggregate trace as well as the receipt and final-decision roots.
        execution_trace_sha256=proof.proof_sha256,
        decision_candidate_sha256=decision.decision_candidate_sha256,
        parent_provision_decision_sha256=decision.decision_sha256,
        evidence_refs=evidence_refs,
    )
    professional_attestation = attest_d3_parent_provision_policy(
        expected_policy_closure=closure,
        expected_runner=runner_authority,
        expected_run_id=decision.run_id,
        expected_source_sha256=decision.source_sha256,
        expected_d2_artifact_id=decision.predecessor_d2_artifact_id,
        expected_d2_artifact_sha256=decision.predecessor_d2_artifact_sha256,
        expected_stage_session_attestation_sha256=session_sha256,
        expected_execution_trace_sha256=proof.proof_sha256,
        expected_decision_candidate_sha256=decision.decision_candidate_sha256,
        expected_parent_provision_decision_sha256=decision.decision_sha256,
        expected_evidence_refs=evidence_refs,
        execution_receipt=professional_execution,
    )
    if (
        not professional_attestation.policy_attested
        or professional_attestation.diagnostic_codes
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_PROFESSIONAL_POLICY_NOT_ATTESTED"
        )

    professional_identity = build_professional_policy_attestation_identity(
        parent_execution_receipt=professional_execution,
        parent_policy_attestation=professional_attestation,
        attested_decision_sha256=decision.decision_sha256,
    )
    proposal = ParentProvisionProposalV3(
        run_id=decision.run_id,
        source_sha256=decision.source_sha256,
        kit_identity=decision.kit_identity,
        predecessor_d2_artifact_id=decision.predecessor_d2_artifact_id,
        predecessor_d2_artifact_sha256=decision.predecessor_d2_artifact_sha256,
        policy_attestation=professional_identity,
        status=decision.status,
        declared_logical_object_ids=decision.declared_logical_object_ids,
        unresolved_logical_object_ids=decision.unresolved_logical_object_ids,
        entries=decision.entries,
        diagnostic_codes=decision.diagnostic_codes,
    )
    try:
        host_receipt = validate_parent_provision_proposal(
            d2_identity=d2_identity,
            source_segments=source_segments,
            parent_execution_receipt=professional_execution,
            parent_policy_attestation=professional_attestation,
            proposal=proposal,
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_HOST_VALIDATION_FAILED"
        ) from exc
    artifact, final_receipt = _build_artifact_and_final(proposal, host_receipt)
    return ParentProvisionProfessionalAggregateResult(
        professional_proof=proof,
        compiled_decision=decision,
        professional_execution_receipt=professional_execution,
        professional_policy_attestation=professional_attestation,
        proposal=proposal,
        host_validation_receipt=host_receipt,
        stage_artifact=artifact,
        final_receipt=final_receipt,
        nested_evidence_assignment_closure=nested_evidence_assignment_closure,
    )


def build_nested_evidence_assignment_closure_for_activation(
    *,
    activation: CanonicalAppServerStageActivation,
    decision: D3HostCompiledParentProvisionDecisionV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> ParentProvisionNestedEvidenceAssignmentClosureV1 | None:
    """Build the mandatory region closure; legacy V1 has no nested ledger."""

    stage_input = _validated_stage_input(activation)
    if isinstance(stage_input, D3StageInputBundleAttestationV1):
        return None
    eligibilities = _load_region_nested_evidence_eligibilities(
        activation=activation,
        stage_input=stage_input,
    )
    try:
        return build_parent_provision_nested_evidence_assignment_closure(
            stage_input_bundle_sha256=stage_input.bundle_sha256,
            stage_input_manifest_sha256=(
                stage_input.stage_input_manifest.manifest_sha256
            ),
            decision=decision,
            source_segments=source_segments,
            eligibilities=eligibilities,
        )
    except ParentProvisionNestedEvidenceError as exc:
        raise ParentProvisionProfessionalAggregateError(str(exc)) from exc


def _validated_stage_input(
    activation: CanonicalAppServerStageActivation,
) -> D3StageInputBundleAttestationV1 | D3RegionStageInputWorkspaceManifestV1:
    legacy = activation.d3_stage_input_attestation
    region = activation.d3_region_stage_input_attestation
    if (legacy is None) == (region is None):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_STAGE_INPUT_ATTESTATION_NOT_EXACTLY_ONE"
        )
    if legacy is not None:
        return D3StageInputBundleAttestationV1.model_validate(
            legacy.model_dump(mode="json")
        )
    assert region is not None
    return D3RegionStageInputWorkspaceManifestV1.model_validate(
        region.model_dump(mode="json")
    )


def _require_stage_input_lineage(
    *,
    activation: CanonicalAppServerStageActivation,
    session: CanonicalAppServerDocumentSessionAttestationV1,
    stage_input: D3StageInputBundleAttestationV1
    | D3RegionStageInputWorkspaceManifestV1,
    plan: ParentProvisionObjectBatchPlanV1,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
) -> None:
    predecessor = activation.predecessor
    if predecessor is None or (
        predecessor.run_id != d2_identity.run_id
        or predecessor.source_sha256 != d2_identity.source_sha256
        or predecessor.artifact_id != d2_identity.artifact_id
        or predecessor.artifact_sha256 != d2_identity.artifact_sha256
        or predecessor.validator_receipt_sha256 != d2_identity.validator_receipt_sha256
        or plan.run_id != d2_identity.run_id
        or plan.source_sha256 != d2_identity.source_sha256
        or plan.kit_bundle_sha256 != d2_identity.kit_identity.bundle_sha256
        or plan.predecessor_artifact_id != d2_identity.artifact_id
        or plan.predecessor_artifact_sha256 != d2_identity.artifact_sha256
        or plan.predecessor_validator_receipt_sha256
        != d2_identity.validator_receipt_sha256
        or plan.source_scope_sha256 != d2_identity.source_scope_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_STAGE_INPUT_LINEAGE_DRIFT"
        )
    if isinstance(stage_input, D3StageInputBundleAttestationV1):
        if (
            session.d3_stage_input != stage_input
            or session.d3_region_stage_input is not None
            or activation.d3_stage_input_attestation != stage_input
            or activation.d3_region_stage_input_attestation is not None
            or stage_input.run_id != d2_identity.run_id
            or stage_input.source_sha256 != d2_identity.source_sha256
            or stage_input.kit_bundle_sha256 != d2_identity.kit_identity.bundle_sha256
            or stage_input.predecessor.artifact_id != d2_identity.artifact_id
            or stage_input.predecessor.artifact_sha256 != d2_identity.artifact_sha256
            or stage_input.predecessor.validator_receipt_sha256
            != d2_identity.validator_receipt_sha256
            or stage_input.source_scope_sha256 != d2_identity.source_scope_sha256
            or plan.stage_input_bundle_sha256 != stage_input.bundle_sha256
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_STAGE_INPUT_LINEAGE_DRIFT"
            )
        return

    manifest = stage_input.stage_input_manifest
    lineage = manifest.lineage
    if (
        session.d3_region_stage_input != stage_input
        or session.d3_stage_input is not None
        or activation.d3_region_stage_input_attestation != stage_input
        or activation.d3_stage_input_attestation is not None
        or predecessor.proposal_schema_id
        != D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
        or stage_input.run_id != d2_identity.run_id
        or stage_input.source_sha256 != d2_identity.source_sha256
        or stage_input.kit_bundle_sha256 != d2_identity.kit_identity.bundle_sha256
        or stage_input.predecessor_artifact_id != d2_identity.artifact_id
        or stage_input.predecessor_artifact_sha256 != d2_identity.artifact_sha256
        or stage_input.predecessor_validation_receipt_sha256
        != d2_identity.validator_receipt_sha256
        or lineage.run_id != d2_identity.run_id
        or lineage.source_sha256 != d2_identity.source_sha256
        or lineage.kit_bundle_sha256 != d2_identity.kit_identity.bundle_sha256
        or lineage.v2_artifact_id != d2_identity.artifact_id
        or lineage.v2_artifact_sha256 != d2_identity.artifact_sha256
        or lineage.v2_validation_receipt_sha256 != d2_identity.validator_receipt_sha256
        or manifest.ordered_logical_object_ids != d2_identity.logical_object_ids
        or manifest.ordered_segment_ids != d2_identity.source_segment_ids
        or plan.stage_input_bundle_sha256 != stage_input.bundle_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_LINEAGE_DRIFT"
        )


def _require_aggregate_execution_eligibility(
    *,
    initial_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2,
    finalized_receipt: ParentProvisionRecognitionFinalizedExecutionReceiptV1,
    plan: ParentProvisionObjectBatchPlanV1,
    activation: CanonicalAppServerStageActivation,
    session: CanonicalAppServerDocumentSessionAttestationV1,
    session_sha256: str,
    policy_closure_sha256: str,
    global_aggregate_sha256: str,
    ordered_batch_result_sha256s: tuple[str, ...] | None,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    nested_evidence_assignment_closure: (
        ParentProvisionNestedEvidenceAssignmentClosureV1 | None
    ),
) -> None:
    accepted_statuses = {
        ParentProvisionRecognitionBatchStatus.ACCEPTED,
        ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
    }
    eligible_initial_statuses = set(accepted_statuses)
    if (
        finalized_receipt.finalization_mode
        is ParentProvisionRecognitionFinalizationMode.RECONCILED
    ):
        eligible_initial_statuses.add(
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
        )
    if (
        len(initial_receipt.batches) != len(plan.targets)
        or initial_receipt.planned_batch_count != len(plan.targets)
        or any(
            item.status not in eligible_initial_statuses
            for item in initial_receipt.batches
        )
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_BATCH_SET_NOT_FINAL"
        )
    if (
        initial_receipt.run_id != d2_identity.run_id
        or initial_receipt.source_sha256 != d2_identity.source_sha256
        or initial_receipt.predecessor_artifact_id != d2_identity.artifact_id
        or initial_receipt.predecessor_artifact_sha256 != d2_identity.artifact_sha256
        or initial_receipt.kit_bundle_sha256 != d2_identity.kit_identity.bundle_sha256
        or initial_receipt.policy_closure_sha256 != policy_closure_sha256
        or initial_receipt.runner_profile_sha256 != session.runner_profile_sha256
        or initial_receipt.model_id != session.model_id
        or initial_receipt.reasoning_effort != session.reasoning_effort
        or initial_receipt.plan_id != plan.plan_id
        or initial_receipt.plan_sha256 != plan.plan_sha256
        or finalized_receipt.run_id != initial_receipt.run_id
        or finalized_receipt.source_sha256 != initial_receipt.source_sha256
        or finalized_receipt.predecessor_artifact_id
        != initial_receipt.predecessor_artifact_id
        or finalized_receipt.predecessor_artifact_sha256
        != initial_receipt.predecessor_artifact_sha256
        or finalized_receipt.kit_bundle_sha256 != initial_receipt.kit_bundle_sha256
        or finalized_receipt.policy_closure_sha256
        != initial_receipt.policy_closure_sha256
        or finalized_receipt.runner_profile_sha256
        != initial_receipt.runner_profile_sha256
        or finalized_receipt.model_id != initial_receipt.model_id
        or finalized_receipt.reasoning_effort != initial_receipt.reasoning_effort
        or finalized_receipt.plan_id != initial_receipt.plan_id
        or finalized_receipt.plan_sha256 != initial_receipt.plan_sha256
        or finalized_receipt.initial_aggregate_execution_receipt_sha256
        != initial_receipt.receipt_sha256
        or finalized_receipt.initial_aggregate_execution_trace_sha256
        != initial_receipt.aggregate_execution_trace_sha256
        or finalized_receipt.initial_batch_receipts_sha256
        != initial_receipt.batch_receipts_sha256
        or finalized_receipt.global_aggregate_sha256 != global_aggregate_sha256
        or finalized_receipt.nested_evidence_assignment_closure_sha256
        != (
            nested_evidence_assignment_closure.closure_sha256
            if nested_evidence_assignment_closure is not None
            else None
        )
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_EXECUTION_LINEAGE_DRIFT"
        )
    initial_reconciliation_batches = tuple(
        item
        for item in initial_receipt.batches
        if item.status
        is ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
    )
    if (
        finalized_receipt.finalization_mode
        is ParentProvisionRecognitionFinalizationMode.DIRECT
    ):
        if (
            initial_receipt.status
            not in {
                ParentProvisionRecognitionAggregateStatus.COMPLETE,
                ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
            }
            or initial_reconciliation_batches
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_DIRECT_FINALIZATION_INPUT_DRIFT"
            )
    else:
        batch_by_id = {item.batch_id: item for item in initial_receipt.batches}
        replaced = tuple(
            batch_by_id.get(item) for item in finalized_receipt.replaced_batch_ids
        )
        if (
            not initial_reconciliation_batches
            or any(item is None for item in replaced)
            or not {item.batch_id for item in initial_reconciliation_batches}.issubset(
                finalized_receipt.replaced_batch_ids
            )
            or finalized_receipt.replaced_initial_batch_receipt_sha256s
            != tuple(item.receipt_sha256 for item in replaced if item is not None)
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_RECONCILIATION_REPLACEMENT_SCOPE_DRIFT"
            )

    stage_binding = next(
        (
            item
            for item in session.stages
            if item.stage is AgentExtractionStage.PARENT_PROVISION_RECOGNITION
        ),
        None,
    )
    if stage_binding is None:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_SESSION_STAGE_BINDING_MISSING"
        )
    if (
        activation.context_sha256 != stage_binding.context_sha256
        or activation.validator_id != PARENT_PROVISION_VALIDATOR_ID
        or activation.validator_version != PARENT_PROVISION_VALIDATOR_VERSION
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_ACTIVATION_STAGE_BINDING_DRIFT"
        )
    result_sha256s: list[str] = []
    for target, batch in zip(plan.targets, initial_receipt.batches, strict=True):
        if (
            batch.batch_id != target.batch_id
            or batch.batch_order != target.batch_order
            or batch.target_scope_sha256 != target.target_scope_sha256
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_AGGREGATE_BATCH_LINEAGE_DRIFT"
            )
        if (
            target.execution_mode
            is ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_AGGREGATE_EVIDENCE_RECOVERY_PENDING"
            )
        if (
            target.execution_mode
            is ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER
        ):
            if (
                batch.status
                is not ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
            ):
                raise ParentProvisionProfessionalAggregateError(
                    "PARENT_PROVISION_HOST_EMPTY_BATCH_NOT_ACCEPTED"
                )
            continue
        if batch.status not in {
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
        }:
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_MODEL_BATCH_NOT_ACCEPTED"
            )
        accepted = next(
            (
                item
                for item in batch.attempts
                if item.attempt_id == batch.accepted_attempt_id
            ),
            None,
        )
        if accepted is None or accepted.batch_compilation_result_sha256 is None:
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_ACCEPTED_BATCH_COMPILATION_MISSING"
            )
        if (
            accepted.stage_context_sha256 != stage_binding.context_sha256
            or accepted.skill_sha256 != stage_binding.skill.sha256
            or accepted.runner_output_schema_sha256
            != activation.runner_output_schema_sha256
            or accepted.model_id != session.model_id
            or accepted.reasoning_effort != session.reasoning_effort
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_ACCEPTED_ATTEMPT_SESSION_DRIFT"
            )
        if batch.status is not (
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
        ):
            result_sha256s.append(accepted.batch_compilation_result_sha256)
    if (
        finalized_receipt.finalization_mode
        is ParentProvisionRecognitionFinalizationMode.DIRECT
        and ordered_batch_result_sha256s is not None
        and tuple(result_sha256s) != ordered_batch_result_sha256s
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_ACCEPTED_ATTEMPT_AGGREGATE_DRIFT"
        )
    if len(global_aggregate_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in global_aggregate_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_GLOBAL_AGGREGATE_SHA_INVALID"
        )


def _validated_nested_evidence_assignment_closure(
    value: ParentProvisionNestedEvidenceAssignmentClosureV1 | None,
) -> ParentProvisionNestedEvidenceAssignmentClosureV1 | None:
    if value is None:
        return None
    try:
        return ParentProvisionNestedEvidenceAssignmentClosureV1.model_validate(
            value.model_dump(mode="json")
        )
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_CONTRACT_INVALID"
        ) from exc


def _require_nested_evidence_assignment_closure(
    *,
    activation: CanonicalAppServerStageActivation,
    stage_input: D3StageInputBundleAttestationV1
    | D3RegionStageInputWorkspaceManifestV1,
    decision: D3HostCompiledParentProvisionDecisionV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    finalized_receipt: ParentProvisionRecognitionFinalizedExecutionReceiptV1,
    closure: ParentProvisionNestedEvidenceAssignmentClosureV1 | None,
) -> None:
    if isinstance(stage_input, D3StageInputBundleAttestationV1):
        if (
            closure is not None
            or finalized_receipt.nested_evidence_assignment_closure_sha256 is not None
        ):
            raise ParentProvisionProfessionalAggregateError(
                "PARENT_PROVISION_LEGACY_STAGE_INPUT_NESTED_ASSIGNMENT_FORBIDDEN"
            )
        return
    if closure is None:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_NESTED_EVIDENCE_ASSIGNMENT_MISSING"
        )
    if (
        finalized_receipt.nested_evidence_assignment_closure_sha256
        != closure.closure_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_RECEIPT_DRIFT"
        )
    expected_eligibilities = _load_region_nested_evidence_eligibilities(
        activation=activation,
        stage_input=stage_input,
    )
    try:
        validate_parent_provision_nested_evidence_assignment_closure(
            expected_stage_input_bundle_sha256=stage_input.bundle_sha256,
            expected_stage_input_manifest_sha256=(
                stage_input.stage_input_manifest.manifest_sha256
            ),
            decision=decision,
            source_segments=source_segments,
            expected_eligibilities=expected_eligibilities,
            closure=closure,
        )
    except ParentProvisionNestedEvidenceError as exc:
        raise ParentProvisionProfessionalAggregateError(str(exc)) from exc


def _load_region_nested_evidence_eligibilities(
    *,
    activation: CanonicalAppServerStageActivation,
    stage_input: D3RegionStageInputWorkspaceManifestV1,
) -> tuple[ParentProvisionNestedEvidenceEligibilityV1, ...]:
    manifest_path = activation.d3_region_stage_input_manifest_path
    if manifest_path is None:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_PATH_MISSING"
        )
    source_path = manifest_path.parent / "d3-unified-exact-source-segments-v3.json"
    expected_file = next(
        (
            item
            for item in stage_input.files
            if item.path == "d3-unified-exact-source-segments-v3.json"
        ),
        None,
    )
    try:
        payload = source_path.read_bytes()
    except OSError as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_UNREADABLE"
        ) from exc
    if (
        expected_file is None
        or hashlib.sha256(payload).hexdigest() != expected_file.sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_FILE_DRIFT"
        )
    try:
        segment_set = D3UnifiedExactSourceSegmentSetV3.model_validate_json(payload)
    except Exception as exc:
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_PAYLOAD_INVALID"
        ) from exc
    manifest = stage_input.stage_input_manifest
    if (
        segment_set.segment_set_id != manifest.segment_set_id
        or segment_set.segment_set_sha256 != manifest.segment_set_sha256
        or segment_set.source_scope_sha256 != manifest.source_scope_sha256
        or tuple(item.segment_id for item in segment_set.segments)
        != manifest.ordered_segment_ids
        or tuple(item.ledger_sha256 for item in segment_set.evidence_ledger)
        != manifest.evidence_ledger_sha256s
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_REGION_STAGE_INPUT_MANIFEST_DRIFT"
        )
    try:
        return build_nested_evidence_eligibilities(segment_set.evidence_ledger)
    except ParentProvisionNestedEvidenceError as exc:
        raise ParentProvisionProfessionalAggregateError(str(exc)) from exc


def _require_final_hash_closure(
    *,
    finalized_receipt: ParentProvisionRecognitionFinalizedExecutionReceiptV1,
    global_aggregate_sha256: str,
    global_candidate: D3ParentBoundaryClassificationCandidateV2,
    decision: D3HostCompiledParentProvisionDecisionV1,
    evidence_refs_sha256: str,
) -> None:
    if (
        finalized_receipt.global_aggregate_sha256 != global_aggregate_sha256
        or finalized_receipt.global_candidate_sha256
        != global_candidate.candidate_sha256
        or finalized_receipt.parent_provision_decision_sha256
        != decision.decision_sha256
        or finalized_receipt.evidence_refs_sha256 != evidence_refs_sha256
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_FINAL_HASH_DRIFT"
        )
    expected_status = (
        ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
        if decision.status is ParentProvisionStatus.PARTIAL
        else ParentProvisionRecognitionAggregateStatus.COMPLETE
    )
    unresolved_entry_count = sum(
        entry.requirementness is Requirementness.UNRESOLVED
        for entry in decision.entries
    )
    predecessor_partial = "PREDECESSOR_D2_PARTIAL" in decision.diagnostic_codes
    if (
        finalized_receipt.status is not expected_status
        or finalized_receipt.semantic_unresolved_scope_count
        != len(decision.unresolved_logical_object_ids)
        or finalized_receipt.semantic_unresolved_entry_count != unresolved_entry_count
        or finalized_receipt.predecessor_partial != predecessor_partial
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_SEMANTIC_STATUS_DRIFT"
        )


def _build_professional_proof(
    *,
    activation: CanonicalAppServerStageActivation,
    session_sha256: str,
    policy_closure_sha256: str,
    runner_identity: AgentRunnerIdentityV1,
    plan: ParentProvisionObjectBatchPlanV1,
    initial_aggregate_execution_receipt: (
        ParentProvisionRecognitionAggregateExecutionReceiptV2
    ),
    finalized_execution_receipt: ParentProvisionRecognitionFinalizedExecutionReceiptV1,
    global_aggregate_sha256: str,
    global_candidate_sha256: str,
    decision: D3HostCompiledParentProvisionDecisionV1,
    evidence_refs_sha256: str,
    nested_evidence_assignment_closure: (
        ParentProvisionNestedEvidenceAssignmentClosureV1 | None
    ),
) -> ParentProvisionAggregateProfessionalProofV1:
    values = {
        "content_free": True,
        "run_id": decision.run_id,
        "source_sha256": decision.source_sha256,
        "predecessor_artifact_id": decision.predecessor_d2_artifact_id,
        "predecessor_artifact_sha256": decision.predecessor_d2_artifact_sha256,
        "stage": AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        "kit_identity": decision.kit_identity,
        "runner_identity": runner_identity,
        "model_id": activation.session_attestation.model_id,
        "reasoning_effort": activation.session_attestation.reasoning_effort,
        "stage_session_attestation_sha256": session_sha256,
        "policy_closure_sha256": policy_closure_sha256,
        "plan_id": plan.plan_id,
        "plan_sha256": plan.plan_sha256,
        "initial_aggregate_execution_receipt_sha256": (
            initial_aggregate_execution_receipt.receipt_sha256
        ),
        "initial_aggregate_execution_trace_sha256": (
            initial_aggregate_execution_receipt.aggregate_execution_trace_sha256
        ),
        "finalized_execution_receipt_sha256": (
            finalized_execution_receipt.receipt_sha256
        ),
        "final_execution_trace_sha256": (
            finalized_execution_receipt.final_execution_trace_sha256
        ),
        "object_batch_aggregate_sha256": global_aggregate_sha256,
        "object_batch_global_candidate_sha256": global_candidate_sha256,
        "decision_candidate_sha256": decision.decision_candidate_sha256,
        "parent_provision_decision_sha256": decision.decision_sha256,
        "evidence_refs_sha256": evidence_refs_sha256,
        "nested_evidence_assignment_closure_sha256": (
            nested_evidence_assignment_closure.closure_sha256
            if nested_evidence_assignment_closure is not None
            else None
        ),
    }
    draft = ParentProvisionAggregateProfessionalProofV1.model_construct(
        proof_id="parent-provision-aggregate-professional-proof-" + "0" * 32,
        proof_sha256="0" * 64,
        **values,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"proof_id", "proof_sha256"})
    )
    return ParentProvisionAggregateProfessionalProofV1(
        proof_id=f"parent-provision-aggregate-professional-proof-{digest[:32]}",
        proof_sha256=digest,
        **values,
    )


def _build_artifact_and_final(
    proposal: ParentProvisionProposalV3,
    receipt: ParentProvisionValidationReceiptV3,
) -> tuple[AgentStageArtifactV1, AgentStageFinalReceiptV1]:
    if (
        receipt.run_id != proposal.run_id
        or receipt.source_sha256 != proposal.source_sha256
        or receipt.kit_bundle_sha256 != proposal.kit_identity.bundle_sha256
        or receipt.artifact_sha256
        != canonical_json_sha256(proposal.model_dump(mode="json"))
        or receipt.predecessor_d2_artifact_id != proposal.predecessor_d2_artifact_id
        or receipt.predecessor_d2_artifact_sha256
        != proposal.predecessor_d2_artifact_sha256
        or receipt.status is not proposal.status
        or receipt.validator_id != PARENT_PROVISION_VALIDATOR_ID
        or receipt.validator_version != PARENT_PROVISION_VALIDATOR_VERSION
    ):
        raise ParentProvisionProfessionalAggregateError(
            "PARENT_PROVISION_AGGREGATE_HOST_RECEIPT_INVALID"
        )
    artifact_status = (
        AgentStageArtifactStatus.VALIDATED
        if proposal.status is ParentProvisionStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    unresolved_count = (
        0
        if artifact_status is AgentStageArtifactStatus.VALIDATED
        else max(
            1,
            receipt.unresolved_scope_count + receipt.unresolved_entry_count,
        )
    )
    artifact = AgentStageArtifactV1(
        artifact_id=receipt.artifact_id,
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        status=artifact_status,
        proposal_schema_id=PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION,
        payload_sha256=receipt.artifact_sha256,
        evidence_sha256=receipt.evidence_sha256,
        validator_receipt_sha256=receipt.receipt_sha256,
        predecessor_artifact_ids=(proposal.predecessor_d2_artifact_id,),
        unresolved_scope_count=unresolved_count,
        diagnostic_codes=(
            ()
            if artifact_status is AgentStageArtifactStatus.VALIDATED
            else proposal.diagnostic_codes
        ),
    )
    final_receipt = AgentStageFinalReceiptV1(
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
    return artifact, final_receipt


__all__ = [
    "PARENT_PROVISION_AGGREGATE_PROFESSIONAL_PROOF_SCHEMA_VERSION",
    "PARENT_PROVISION_RECOGNITION_FINALIZED_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "ParentProvisionAggregateProfessionalProofV1",
    "ParentProvisionProfessionalAggregateError",
    "ParentProvisionProfessionalAggregateResult",
    "ParentProvisionRecognitionFinalizationMode",
    "ParentProvisionRecognitionFinalizedExecutionReceiptV1",
    "build_nested_evidence_assignment_closure_for_activation",
    "build_parent_provision_recognition_finalized_execution_receipt",
    "finalize_parent_provision_professional_aggregate",
]
