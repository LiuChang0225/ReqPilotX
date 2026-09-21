"""Source-window Agent parent recognition over one accepted native baseline."""

from __future__ import annotations

from collections import Counter
from io import BytesIO
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from reqpilotx_server.adapters.documents.pdf_source_crop_renderer import (
    inspect_pdf_page_geometry,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_document_session import (
    CanonicalD3RegionStageInputRequest,
    CanonicalProductEvidenceRequest,
    CanonicalStagePredecessorBindingV1,
    prepare_canonical_app_server_document_session,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_hosts import (
    CanonicalParentProvisionObjectBatchCompiler,
)
from reqpilotx_server.adapters.agents.codex_app_server.canonical_stage_transport import (
    LocalCodexAppServerStructuredStageTransport,
)
from reqpilotx_server.adapters.agents.codex_app_server.lifecycle import _canary_runtime_state
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_object_workflow import (
    CanonicalParentProvisionObjectWorkflowOrchestrator,
)
from reqpilotx_server.adapters.agents.codex_app_server.parent_provision_professional_aggregate import (
    ParentProvisionAggregateProfessionalProofV1,
    ParentProvisionRecognitionFinalizedExecutionReceiptV1,
)
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.d3_parent_provision_attestation import (
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
)
from reqpilotx_server.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionProposalV3,
    ParentProvisionStatus,
    ParentProvisionValidationReceiptV3,
    Requirementness,
    canonical_json_sha256,
)
from reqpilotx_server.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
    AgentStageFinalReceiptV1,
)
from reqpilotx_server.application.agent_workflow.native_structure_baseline import (
    NativeStructureAcceptedBaselineV1,
    NativeStructureBaselineArtifactV1,
    native_structure_baseline_object_key,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _canonical_sha256,
    _document_source_sha256,
    _no_store_headers,
    _require_project_document,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionBatchReceiptV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchCompilationV1,
    ParentProvisionObjectBatchPlanV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_reconciliation_receipts import (
    ParentProvisionReconciliationAggregateReceiptV1,
    ParentProvisionReconciliationGroupReceiptV1,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_reconciliation import (
    ParentProvisionObjectReconciliationCompilationV1,
    ParentProvisionObjectReconciliationGroupV1,
    ParentProvisionObjectReconciliationPlanV1,
)
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.application.agent_workflow.region_parent_runtime_adapter import (
    adapt_region_parent_runtime,
)
from reqpilotx_server.application.agent_workflow.d3_region_stage_input import (
    materialize_d3_region_stage_input,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobStatus,
    NonRetryableJobError,
)


NATIVE_PARENT_PROVISION_JOB_KIND = "agent.parent-provision.recognize"
NATIVE_PARENT_PROVISION_JOB_SCHEMA_VERSION = "native-parent-provision-job/v1"
NATIVE_PARENT_PROVISION_RESULT_SCHEMA_VERSION = "native-parent-provision-job-result/v1"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
_AGENT_KIT_ROOT = _REPOSITORY_ROOT / "agent-kit"


class NativeParentProvisionResultArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["native-parent-provision-result-artifact/v1"] = (
        "native-parent-provision-result-artifact/v1"
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    baseline: NativeStructureAcceptedBaselineV1
    proposal: ParentProvisionProposalV3
    professional_execution_receipt: D3ParentProvisionExecutionReceiptV1
    professional_policy_attestation: D3ParentProvisionPolicyAttestationV1
    host_validation_receipt: ParentProvisionValidationReceiptV3
    stage_artifact: AgentStageArtifactV1
    final_receipt: AgentStageFinalReceiptV1
    aggregate_execution_receipt: ParentProvisionRecognitionAggregateExecutionReceiptV2
    finalized_execution_receipt: ParentProvisionRecognitionFinalizedExecutionReceiptV1
    professional_proof: ParentProvisionAggregateProfessionalProofV1
    reconciliation_execution_receipt: (
        ParentProvisionReconciliationAggregateReceiptV1 | None
    ) = None
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> NativeParentProvisionResultArtifactV1:
        if (
            self.proposal.source_sha256 != self.baseline.source_sha256
            or self.proposal.kit_identity != self.baseline.kit_identity
            or self.proposal.predecessor_d2_artifact_id
            != self.baseline.accepted_structure_artifact_id
            or self.proposal.predecessor_d2_artifact_sha256
            != self.baseline.accepted_structure_artifact_sha256
            or self.stage_artifact.payload_sha256
            != self.host_validation_receipt.artifact_sha256
            or self.final_receipt.proposal_sha256 != self.stage_artifact.payload_sha256
            or self.final_receipt.validation_receipt_sha256
            != self.host_validation_receipt.receipt_sha256
            or self.professional_proof.finalized_execution_receipt_sha256
            != self.finalized_execution_receipt.receipt_sha256
            or self.professional_proof.initial_aggregate_execution_receipt_sha256
            != self.aggregate_execution_receipt.receipt_sha256
        ):
            raise ValueError("native parent result artifact lineage drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"artifact_sha256"})
        )
        if self.artifact_sha256 != expected:
            raise ValueError("native parent result private artifact SHA drifted")
        return self


class NativeParentWindowCheckpointArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["native-parent-window-checkpoint/v1"] = (
        "native-parent-window-checkpoint/v1"
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    baseline_id: str = Field(pattern=r"^native-structure-baseline-[0-9a-f]{32}$")
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    window_ordinal: int = Field(ge=1)
    batch_receipts: tuple[ParentProvisionRecognitionBatchReceiptV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    compilations: tuple[ParentProvisionObjectBatchCompilationV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(self) -> NativeParentWindowCheckpointArtifactV1:
        receipt_ids = tuple(item.batch_id for item in self.batch_receipts)
        compilation_ids = tuple(item.batch_id for item in self.compilations)
        if receipt_ids != compilation_ids or len(receipt_ids) != len(set(receipt_ids)):
            raise ValueError("native parent window checkpoint batch scope drifted")
        if any(
            item.plan_id != self.plan_id or item.plan_sha256 != self.plan_sha256
            for item in (*self.batch_receipts, *self.compilations)
        ):
            raise ValueError("native parent window checkpoint plan drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("native parent window checkpoint SHA drifted")
        return self


class NativeParentReconciliationCheckpointArtifactV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)

    schema_version: Literal["native-parent-reconciliation-checkpoint/v1"] = (
        "native-parent-reconciliation-checkpoint/v1"
    )
    content_bearing: Literal[True] = True
    private_object_store_only: Literal[True] = True
    baseline_id: str = Field(pattern=r"^native-structure-baseline-[0-9a-f]{32}$")
    baseline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    object_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
    )
    group_order: int = Field(ge=0)
    group_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_receipt: ParentProvisionReconciliationGroupReceiptV1
    compilation: ParentProvisionObjectReconciliationCompilationV1
    checkpoint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_checkpoint(
        self,
    ) -> NativeParentReconciliationCheckpointArtifactV1:
        if (
            self.group_receipt.object_batch_plan_id != self.object_plan_id
            or self.group_receipt.object_batch_plan_sha256 != self.object_plan_sha256
            or self.compilation.object_batch_plan_id != self.object_plan_id
            or self.compilation.object_batch_plan_sha256 != self.object_plan_sha256
            or self.group_receipt.reconciliation_plan_id != self.reconciliation_plan_id
            or self.group_receipt.reconciliation_plan_sha256
            != self.reconciliation_plan_sha256
            or self.compilation.reconciliation_plan_id != self.reconciliation_plan_id
            or self.compilation.reconciliation_plan_sha256
            != self.reconciliation_plan_sha256
            or self.group_receipt.group_id != self.group_id
            or self.compilation.group_id != self.group_id
            or self.group_receipt.group_order != self.group_order
            or self.compilation.group_order != self.group_order
            or self.group_receipt.group_scope_sha256 != self.group_scope_sha256
            or self.compilation.group_scope_sha256 != self.group_scope_sha256
            or self.group_receipt.reconciliation_compilation_result_sha256
            != self.compilation.result_sha256
        ):
            raise ValueError("native reconciliation checkpoint lineage drifted")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"checkpoint_sha256"})
        )
        if self.checkpoint_sha256 != expected:
            raise ValueError("native reconciliation checkpoint SHA drifted")
        return self


def enqueue_native_parent_provision_recognition(
    *,
    project_id: str,
    document_id: str,
    baseline: NativeStructureAcceptedBaselineV1,
    baseline_object_key: str,
    baseline_object_sha256: str,
    baseline_artifact_sha256: str,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _durable_jobs

    accepted = NativeStructureAcceptedBaselineV1.model_validate(
        baseline.model_dump(mode="json")
    )
    if (
        accepted.project_id != project_id
        or accepted.document_id != document_id
        or baseline_object_key != native_structure_baseline_object_key(accepted)
    ):
        raise ValueError("native parent baseline enqueue identity drifted")
    payload = {
        "schema_version": NATIVE_PARENT_PROVISION_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": accepted.source_sha256,
        "baseline": accepted.model_dump(mode="json"),
        "baseline_object_key": baseline_object_key,
        "baseline_object_sha256": baseline_object_sha256,
        "baseline_artifact_sha256": baseline_artifact_sha256,
        "execution_strategy": "SOURCE_WINDOW_AGENT",
        "max_stage_attempts": 3,
        "max_targets_per_turn": 12,
        "max_source_characters_per_turn": 25_000,
        "deadline_seconds": 600,
    }
    jobs = _durable_jobs()
    existing = jobs.get_latest(
        kind=NATIVE_PARENT_PROVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if _parent_job_matches(existing, accepted):
        return _job_projection(existing)
    if (
        _parent_job_payload_matches(existing, accepted)
        and existing is not None
        and existing.status
        in {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED}
    ):
        return _job_projection(existing)
    retry_after_job_id = (
        existing.job_id
        if _parent_job_payload_matches(existing, accepted)
        and existing is not None
        and existing.status in {JobStatus.FAILED, JobStatus.CANCELLED}
        else ""
    )
    job = jobs.enqueue(
        kind=NATIVE_PARENT_PROVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(
            {"payload": payload, "retry_after_job_id": retry_after_job_id}
        ),
        payload=payload,
        priority=23,
        max_attempts=3,
    )
    return _job_projection(job)


def get_native_parent_provision_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.jobs is None:
        return {
            "schema_version": "native-parent-provision-status/v1",
            "status": "UNAVAILABLE",
            "result_ready": False,
            "job": None,
            "message": "Durable Agent parent recognition is unavailable.",
        }
    job = _durable_jobs().get_latest(
        kind=NATIVE_PARENT_PROVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    return {
        "schema_version": "native-parent-provision-status/v1",
        "status": "IDLE" if job is None else job.status.value,
        "result_ready": bool(job and job.status is JobStatus.SUCCEEDED),
        "job": None if job is None else _job_projection(job),
        "message": _status_message(job),
    }


def get_native_parent_provision_result(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    container = RUNTIME_STATE.container
    if container is None or container.jobs is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "native_parent_provision_runtime_unavailable",
                "message": "Durable Agent parent recognition is unavailable.",
            },
        )
    job = _durable_jobs().get_latest_succeeded(
        kind=NATIVE_PARENT_PROVISION_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if job is None:
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "native_parent_provision_result_not_found",
                "message": "No validated native parent-provision result is available.",
            },
        )
    result = dict(job.result or {})
    if (
        result.get("schema_version") != NATIVE_PARENT_PROVISION_RESULT_SCHEMA_VERSION
        or result.get("status") != "COMPLETE"
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_parent_provision_result_incomplete",
                "message": "The latest parent-provision result is incomplete.",
            },
        )
    baseline = NativeStructureAcceptedBaselineV1.model_validate(
        job.payload.get("baseline")
    )
    if (
        baseline.project_id != project_id
        or baseline.document_id != document_id
        or baseline.source_sha256 != _document_source_sha256(document)
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_parent_provision_result_stale",
                "message": "The validated parent-provision result is stale.",
            },
        )
    baseline_artifact = _load_baseline_artifact(
        container.object_store,
        key=str(job.payload.get("baseline_object_key") or ""),
        expected_object_sha256=str(job.payload.get("baseline_object_sha256") or ""),
        expected_artifact_sha256=str(job.payload.get("baseline_artifact_sha256") or ""),
        expected_baseline=baseline,
    )
    parent_artifact = _load_parent_result_artifact(
        container.object_store,
        result=result,
        expected_baseline=baseline,
    )
    stage_input = materialize_d3_region_stage_input(
        baseline_artifact.source_ir,
        proposal=baseline_artifact.d2_proposal,
        validation_receipt=baseline_artifact.d2_validation_receipt,
        parent_input=baseline_artifact.parent_input,
        parent_input_receipt=baseline_artifact.parent_input_receipt,
        expected_lineage=baseline_artifact.expected_lineage,
    )
    source_path = resolve_source_record_path(document).resolve(strict=True)
    geometry = {
        item.page_number: item.bounds
        for item in inspect_pdf_page_geometry(
            source_path,
            expected_source_sha256=baseline.source_sha256,
        )
    }
    source_nodes = {
        item.source_node_id: item for item in baseline_artifact.source_ir.nodes
    }
    segment_by_id = {item.segment_id: item for item in stage_input.segment_set.segments}
    entries = [
        _public_parent_entry(
            entry,
            segment_by_id=segment_by_id,
            source_nodes=source_nodes,
            page_bounds=geometry,
        )
        for entry in parent_artifact.proposal.entries
    ]
    return {
        "schema_version": "native-parent-provision-baseline-public/v1",
        "source_sha256": baseline.source_sha256,
        "baseline_id": baseline.baseline_id,
        "baseline_sha256": baseline.baseline_sha256,
        "artifact_id": parent_artifact.stage_artifact.artifact_id,
        "artifact_sha256": parent_artifact.stage_artifact.payload_sha256,
        "status": parent_artifact.proposal.status.value,
        "professional_policy_attested": (
            parent_artifact.professional_policy_attestation.policy_attested
        ),
        "summary": {
            "parent_provision_count": len(entries),
            "declared_scope_count": len(
                parent_artifact.proposal.declared_logical_object_ids
            ),
            "requirementness_counts": dict(
                sorted(Counter(item["requirementness"] for item in entries).items())
            ),
        },
        "entries": entries,
    }


def run_durable_native_parent_provision_recognition(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != NATIVE_PARENT_PROVISION_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="native_parent_provision_job_schema_invalid",
            message="Native parent-provision job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Native parent worker runtime is not initialized.")
    progress = _NativeParentProgress(context, initial=durable_job.progress)
    try:
        baseline = NativeStructureAcceptedBaselineV1.model_validate(
            payload.get("baseline")
        )
    except ValueError as exc:
        raise NonRetryableJobError(
            error_code="native_parent_provision_baseline_invalid",
            message="Accepted native baseline identity is invalid.",
        ) from exc
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    document = _require_project_document(project_id, document_id)
    if (
        baseline.project_id != project_id
        or baseline.document_id != document_id
        or _document_source_sha256(document) != baseline.source_sha256
        or payload.get("source_sha256") != baseline.source_sha256
    ):
        raise NonRetryableJobError(
            error_code="native_parent_provision_source_changed",
            message="Document source changed after native baseline acceptance.",
        )
    kit = load_portable_agent_kit(_AGENT_KIT_ROOT)
    if kit.identity != baseline.kit_identity:
        raise NonRetryableJobError(
            error_code="native_parent_provision_kit_changed",
            message="Portable Agent Kit changed after baseline acceptance.",
        )
    baseline_artifact = _load_baseline_artifact(
        container.object_store,
        key=str(payload.get("baseline_object_key") or ""),
        expected_object_sha256=str(payload.get("baseline_object_sha256") or ""),
        expected_artifact_sha256=str(payload.get("baseline_artifact_sha256") or ""),
        expected_baseline=baseline,
    )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="native_parent_provision_cancelled",
            message="Native parent recognition was cancelled before execution.",
        )
    settings = container.settings
    if settings.codex_bin is None or settings.codex_home is None:
        raise NonRetryableJobError(
            error_code="native_parent_provision_runtime_unavailable",
            message="Codex App Server runtime is unavailable.",
        )
    source_path = resolve_source_record_path(document).resolve(strict=True)
    predecessor = CanonicalStagePredecessorBindingV1(
        stage=AgentExtractionStage.PARENT_PROVISION_RECOGNITION,
        predecessor_stage=AgentExtractionStage.LOGICAL_STRUCTURE,
        run_id=baseline_artifact.d2_proposal.run_id,
        source_sha256=baseline.source_sha256,
        artifact_id=baseline.accepted_structure_artifact_id,
        artifact_sha256=baseline.accepted_structure_artifact_sha256,
        validator_receipt_sha256=(baseline.accepted_structure_validator_receipt_sha256),
        proposal_schema_id="logical-structure-proposal/v2",
        status=(
            AgentStageArtifactStatus.VALIDATED
            if baseline_artifact.d2_proposal.status.value == "COMPLETE"
            else AgentStageArtifactStatus.PARTIAL
        ),
    )
    request = CanonicalD3RegionStageInputRequest(
        source_ir=baseline_artifact.source_ir,
        d2_proposal=baseline_artifact.d2_proposal,
        d2_validation_receipt=baseline_artifact.d2_validation_receipt,
        parent_input=baseline_artifact.parent_input,
        parent_input_receipt=baseline_artifact.parent_input_receipt,
        expected_lineage=baseline_artifact.expected_lineage,
    )
    progress.heartbeat(
        progress={
            "stage": "PREPARING_AGENT_DOCUMENT_SESSION",
            "message": "正在准备来源窗口 Agent 会话与已接受原文基线。",
            "strategy": "SOURCE_WINDOW_AGENT",
        }
    )
    with _canary_runtime_state(
        codex_bin=settings.codex_bin,
        codex_home=settings.codex_home,
        repository_root=_REPOSITORY_ROOT,
    ) as base_runtime:
        with prepare_canonical_app_server_document_session(
            base_runtime=base_runtime,
            canonical_kit_root=_AGENT_KIT_ROOT,
            stages=(AgentExtractionStage.PARENT_PROVISION_RECOGNITION,),
            evidence_request=CanonicalProductEvidenceRequest(
                source_path=source_path,
                expected_source_sha256=baseline.source_sha256,
                document_role="CUSTOMER_SOR",
                authorized_corpus_root=source_path.parent,
                run_id=baseline_artifact.d2_proposal.run_id,
                opaque_source_handle=("source-handle-" + baseline.baseline_sha256[:32]),
            ),
            stage_predecessors=(predecessor,),
            repository_root=_REPOSITORY_ROOT,
            d3_region_stage_input_request=request,
        ) as session:
            workspace = session.d3_region_stage_input
            if workspace is None:
                raise NonRetryableJobError(
                    error_code="native_parent_provision_stage_input_missing",
                    message="Canonical D3 region stage input is missing.",
                )
            stage_input = materialize_d3_region_stage_input(
                request.source_ir,
                proposal=request.d2_proposal,
                validation_receipt=request.d2_validation_receipt,
                parent_input=request.parent_input,
                parent_input_receipt=request.parent_input_receipt,
                expected_lineage=request.expected_lineage,
            )
            if stage_input.manifest != workspace.attestation.stage_input_manifest:
                raise NonRetryableJobError(
                    error_code="native_parent_provision_stage_input_drift",
                    message="Canonical D3 region stage input replay drifted.",
                )
            runtime = adapt_region_parent_runtime(
                proposal=request.d2_proposal,
                validation_receipt=request.d2_validation_receipt,
                stage_input=stage_input,
                expected_kit_identity=kit.identity,
                expected_source_sha256=baseline.source_sha256,
                expected_source_ir_canonical_sha256=(
                    source_ir_canonical_sha256(request.source_ir)
                ),
                expected_stage_input_manifest_sha256=(
                    workspace.attestation.stage_input_manifest.manifest_sha256
                ),
                expected_workspace_bundle_sha256=(workspace.attestation.bundle_sha256),
            )
            object_compiler = (
                CanonicalParentProvisionObjectBatchCompiler.from_region_runtime(
                    runtime=runtime,
                    workspace_attestation=workspace.attestation,
                    kit_identity=kit.identity,
                )
            )
            plan = object_compiler.build_plan()
            resumed_receipts, resumed_compilations = _load_window_checkpoints(
                container.object_store,
                records=progress.window_checkpoints,
                baseline=baseline,
                plan=plan,
            )
            (
                resumed_reconciliation_receipts,
                resumed_reconciliation_compilations,
            ) = _load_reconciliation_checkpoints(
                container.object_store,
                records=progress.reconciliation_checkpoints,
                baseline=baseline,
                object_plan=plan,
            )
            max_targets_per_turn = int(payload.get("max_targets_per_turn") or 12)
            max_source_characters_per_turn = int(
                payload.get("max_source_characters_per_turn") or 25_000
            )
            estimated_windows = _estimated_source_window_count(
                plan.targets,
                max_targets=max_targets_per_turn,
                max_source_characters=max_source_characters_per_turn,
            )
            resumed_batch_ids = frozenset(item.batch_id for item in resumed_receipts)
            remaining_windows = _estimated_source_window_count(
                plan.targets,
                max_targets=max_targets_per_turn,
                max_source_characters=max_source_characters_per_turn,
                excluded_batch_ids=resumed_batch_ids,
            )
            total_model_batches = sum(
                item.execution_mode.value == "MODEL_TARGET" for item in plan.targets
            )
            task_binding = canonical_json_sha256(
                {
                    "baseline_sha256": baseline.baseline_sha256,
                    "accepted_structure_artifact_sha256": (
                        baseline.accepted_structure_artifact_sha256
                    ),
                    "source_scope_sha256": runtime.d2_identity.source_scope_sha256,
                    "stage_input_bundle_sha256": workspace.attestation.bundle_sha256,
                    "plan_id": plan.plan_id,
                    "plan_sha256": plan.plan_sha256,
                    "strategy": "SOURCE_WINDOW_AGENT",
                }
            )
            reconciliation_binding = canonical_json_sha256(
                {
                    "object_task_binding_sha256": task_binding,
                    "plan_sha256": plan.plan_sha256,
                    "strategy": "MINIMAL_ADJACENT_RECONCILIATION",
                }
            )
            timeout_seconds = float(payload.get("deadline_seconds") or 600)
            workflow = CanonicalParentProvisionObjectWorkflowOrchestrator(
                kit=kit,
                object_compiler=object_compiler,
                object_transport=_ProgressTransport(
                    LocalCodexAppServerStructuredStageTransport(
                        timeout_seconds=timeout_seconds,
                        cancellation_requested=context.cancellation_requested,
                    ),
                    progress=progress,
                    stage="AGENT_RECOGNIZING_SOURCE_WINDOWS",
                    total=remaining_windows,
                    overall_total=estimated_windows,
                    total_model_batches=total_model_batches,
                ),
                reconciliation_transport=_ProgressTransport(
                    LocalCodexAppServerStructuredStageTransport(
                        timeout_seconds=timeout_seconds,
                        cancellation_requested=context.cancellation_requested,
                    ),
                    progress=progress,
                    stage="AGENT_RECONCILING_ADJACENT_WINDOWS",
                    total=None,
                    overall_total=None,
                    total_model_batches=None,
                ),
                max_attempts_per_object=int(payload.get("max_stage_attempts") or 2),
                max_attempts_per_group=int(payload.get("max_stage_attempts") or 2),
                max_targets_per_turn=max_targets_per_turn,
                max_source_characters_per_turn=(max_source_characters_per_turn),
                window_checkpoint_sink=_window_checkpoint_sink(
                    object_store=container.object_store,
                    baseline=baseline,
                    plan=plan,
                    progress=progress,
                ),
                reconciliation_checkpoint_sink=_reconciliation_checkpoint_sink(
                    object_store=container.object_store,
                    baseline=baseline,
                    object_plan=plan,
                    progress=progress,
                ),
            ).run(
                session,
                object_task_binding_sha256=task_binding,
                reconciliation_task_binding_sha256=reconciliation_binding,
                plan=plan,
                resumed_compilations=resumed_compilations,
                resumed_batch_receipts=resumed_receipts,
                resumed_reconciliation_receipts=(resumed_reconciliation_receipts),
                resumed_reconciliation_compilations=(
                    resumed_reconciliation_compilations
                ),
            )
            if not workflow.publication_ready or workflow.professional_result is None:
                reason = (
                    workflow.reason_codes[0]
                    if workflow.reason_codes
                    else "SOURCE_WINDOW_WORKFLOW_INCOMPLETE"
                )
                message = f"Source-window Agent workflow is incomplete: {reason}"
                if workflow.object_result.execution_receipt.status.value == "FAILED":
                    # Accepted objects have already been checkpointed. Let the
                    # durable queue retry only the remaining technical/model
                    # failures instead of turning one rejected output into a
                    # permanent workflow stop.
                    raise RuntimeError(message)
                raise NonRetryableJobError(
                    error_code="native_parent_source_window_incomplete",
                    message=message,
                )
            professional = workflow.professional_result
            if not _has_complete_working_decisions(professional.proposal):
                raise NonRetryableJobError(
                    error_code="native_parent_working_decision_incomplete",
                    message="Agent did not return a working judgment for every scope.",
                )
    artifact = _parent_result_artifact(
        baseline=baseline,
        workflow=workflow,
        professional=professional,
    )
    encoded = json.dumps(
        artifact.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = _parent_result_object_key(baseline, artifact)
    progress.heartbeat(
        progress={
            "stage": "PERSISTING_PARENT_PROVISION_RESULT",
            "message": "Host 验证已通过，正在保存父条款与需求属性结果。",
        }
    )
    context.begin_publication()
    stored = container.object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type="application/vnd.reqpilotx.parent-provisions+json",
    )
    requirementness = Counter(
        item.requirementness.value for item in professional.proposal.entries
    )
    return {
        "schema_version": NATIVE_PARENT_PROVISION_RESULT_SCHEMA_VERSION,
        "status": "COMPLETE",
        "source_sha256": baseline.source_sha256,
        "baseline_id": baseline.baseline_id,
        "baseline_sha256": baseline.baseline_sha256,
        "execution_strategy": "SOURCE_WINDOW_AGENT",
        "source_window_count": estimated_windows,
        "artifact_id": professional.stage_artifact.artifact_id,
        "artifact_sha256": professional.stage_artifact.payload_sha256,
        "validation_receipt_sha256": (
            professional.host_validation_receipt.receipt_sha256
        ),
        "professional_policy_attested": (
            professional.professional_policy_attestation.policy_attested
        ),
        "parent_provision_count": len(professional.proposal.entries),
        "declared_scope_count": len(professional.proposal.declared_logical_object_ids),
        "requirementness_counts": dict(sorted(requirementness.items())),
        "private_artifact": {
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "artifact_sha256": artifact.artifact_sha256,
            "size": stored.size,
            "media_type": stored.media_type,
        },
        "next_action": "PROJECT_NATIVE_REQUIREMENT_BASELINE",
    }


class _NativeParentProgress:
    def __init__(
        self,
        context: JobExecutionContext,
        *,
        initial: dict[str, Any] | None = None,
    ) -> None:
        self._context = context
        initial_records = (initial or {}).get("window_checkpoints", [])
        self._window_checkpoints: list[dict[str, Any]] = [
            dict(item) for item in initial_records if isinstance(item, dict)
        ]
        initial_reconciliation_records = (initial or {}).get(
            "reconciliation_checkpoints", []
        )
        self._reconciliation_checkpoints: list[dict[str, Any]] = sorted(
            (
                dict(item)
                for item in initial_reconciliation_records
                if isinstance(item, dict)
            ),
            key=lambda item: int(item.get("group_order") or 0),
        )

    @property
    def window_checkpoints(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._window_checkpoints)

    @property
    def validated_batch_count(self) -> int:
        return len(
            {
                str(batch_id)
                for item in self._window_checkpoints
                for batch_id in item.get("batch_ids", ())
                if str(batch_id)
            }
        )

    @property
    def reconciliation_checkpoints(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._reconciliation_checkpoints)

    def heartbeat(self, progress: dict[str, Any]) -> None:
        self._context.heartbeat(
            progress={
                **progress,
                "window_checkpoints": [dict(item) for item in self._window_checkpoints],
                "completed_window_count": len(self._window_checkpoints),
                "checkpoint_count": len(self._window_checkpoints),
                "validated_batch_count": self.validated_batch_count,
                "reconciliation_checkpoints": [
                    dict(item) for item in self._reconciliation_checkpoints
                ],
                "reconciliation_checkpoint_count": len(
                    self._reconciliation_checkpoints
                ),
            }
        )

    def add_window_checkpoint(self, record: dict[str, Any]) -> None:
        self._window_checkpoints.append(dict(record))
        self.heartbeat(
            {
                "stage": "WINDOW_CHECKPOINT_SAVED",
                "message": (
                    f"已保存第 {len(self._window_checkpoints)} 个 Host 验证检查点；"
                    f"累计验证 {self.validated_batch_count} 个对象。"
                ),
                "outcome": "CHECKPOINTED",
                "strategy": "SOURCE_WINDOW_AGENT",
            }
        )

    def add_reconciliation_checkpoint(self, record: dict[str, Any]) -> None:
        group_id = str(record.get("group_id") or "")
        if any(
            str(item.get("group_id") or "") == group_id
            for item in self._reconciliation_checkpoints
        ):
            return
        self._reconciliation_checkpoints.append(dict(record))
        self._reconciliation_checkpoints.sort(
            key=lambda item: int(item.get("group_order") or 0)
        )
        self.heartbeat(
            {
                "stage": "RECONCILIATION_CHECKPOINT_SAVED",
                "message": (
                    f"已保存第 {len(self._reconciliation_checkpoints)} 个跨对象协调检查点。"
                ),
                "outcome": "CHECKPOINTED",
                "strategy": "SOURCE_WINDOW_AGENT",
            }
        )


class _ProgressTransport:
    def __init__(
        self,
        transport,
        *,
        progress: _NativeParentProgress,
        stage: str,
        total: int | None,
        overall_total: int | None,
        total_model_batches: int | None,
    ) -> None:  # type: ignore[no-untyped-def]
        self._transport = transport
        self._progress = progress
        self._stage = stage
        self._total = total
        self._overall_total = overall_total
        self._total_model_batches = total_model_batches
        self._calls = 0
        self._current_window = 0

    def invoke(self, invocation):  # type: ignore[no-untyped-def]
        self._calls += 1
        prompt = next(
            (
                str(item.get("text") or "")
                for item in invocation.input_items
                if item.get("type") == "text"
            ),
            "",
        )
        attempt_match = re.search(
            r"^attempt_ordinal=(\d+)$",
            prompt,
            re.MULTILINE,
        )
        attempt = int(attempt_match.group(1)) if attempt_match else 1
        targets_match = re.search(
            r"^window_targets=(.+)$",
            prompt,
            re.MULTILINE,
        )
        active_target_count = 0
        if targets_match:
            try:
                parsed_targets = json.loads(targets_match.group(1))
                if isinstance(parsed_targets, list):
                    active_target_count = len(parsed_targets)
            except json.JSONDecodeError:
                active_target_count = 0
        failure_match = re.search(
            r"^previous_window_failures=(.+)$",
            prompt,
            re.MULTILINE,
        )
        reconciliation_feedback_match = re.search(
            r"^previous_failure_feedback=(.+)$",
            prompt,
            re.MULTILINE,
        )
        previous_failures: list[dict[str, Any]] = []
        if failure_match and failure_match.group(1) != "NONE":
            try:
                parsed_failures = json.loads(failure_match.group(1))
                if isinstance(parsed_failures, list):
                    previous_failures = [
                        dict(item) for item in parsed_failures if isinstance(item, dict)
                    ]
            except json.JSONDecodeError:
                previous_failures = []
        elif (
            reconciliation_feedback_match
            and reconciliation_feedback_match.group(1) != "NONE"
        ):
            try:
                parsed_failure = json.loads(reconciliation_feedback_match.group(1))
                if isinstance(parsed_failure, dict):
                    previous_failures = [dict(parsed_failure)]
            except json.JSONDecodeError:
                previous_failures = []
        previous_failure_codes = sorted(
            {
                str(code or "")
                for item in previous_failures
                for code in (
                    (item.get("failure_code"),)
                    if item.get("failure_code")
                    else tuple(item.get("reason_codes") or ())
                )
                if str(code or "")
            }
        )
        if self._total is not None and attempt == 1:
            self._current_window += 1
        reconciliation_group_match = re.search(
            r"^reconciliation_group_order=(\d+)$",
            prompt,
            re.MULTILINE,
        )
        reconciliation_group_count_match = re.search(
            r"^reconciliation_group_count=(\d+)$",
            prompt,
            re.MULTILINE,
        )
        reconciliation_group = (
            int(reconciliation_group_match.group(1)) + 1
            if reconciliation_group_match
            else 0
        )
        reconciliation_group_count = (
            int(reconciliation_group_count_match.group(1))
            if reconciliation_group_count_match
            else 0
        )
        visible_window = (
            self._current_window
            if self._total is not None
            else reconciliation_group or self._calls
        )
        checkpoint_count = len(self._progress.window_checkpoints)
        validated_batches = self._progress.validated_batch_count
        self._progress.heartbeat(
            progress={
                "stage": self._stage,
                "message": (
                    f"Agent 正在处理待补来源窗口 {visible_window}/{self._total}；"
                    f"当前 {active_target_count} 个对象，"
                    f"已验证 {validated_batches}/{self._total_model_batches} 个对象，"
                    f"保存 {checkpoint_count} 个检查点"
                    + (
                        f"，第 {attempt} 次尝试；正在修正 "
                        f"{', '.join(previous_failure_codes) or '上一轮Host拒绝'}。"
                        if attempt > 1
                        else "。"
                    )
                    if self._total is not None
                    else (
                        f"Agent 正在协调相邻范围 {reconciliation_group}/"
                        f"{reconciliation_group_count}，第 {attempt} 次尝试"
                        + (
                            f"；正在修正 {', '.join(previous_failure_codes)}。"
                            if previous_failure_codes
                            else "。"
                        )
                    )
                ),
                "current_window": visible_window,
                "total_windows": (
                    self._total
                    if self._total is not None
                    else reconciliation_group_count or None
                ),
                "overall_total_windows": self._overall_total,
                "checkpoint_count": checkpoint_count,
                "validated_batch_count": validated_batches,
                "total_model_batch_count": self._total_model_batches,
                "active_target_count": active_target_count,
                "attempt": attempt,
                "agent_call_count": self._calls,
                "previous_failure_codes": previous_failure_codes,
                "strategy": "SOURCE_WINDOW_AGENT",
                "current_reconciliation_group": reconciliation_group or None,
                "total_reconciliation_groups": (reconciliation_group_count or None),
            }
        )
        started = time.monotonic()
        try:
            result = self._transport.invoke(invocation)
        except BaseException as exc:
            reason_code = str(getattr(exc, "reason_code", "") or "")
            self._progress.heartbeat(
                progress={
                    "stage": self._stage,
                    "message": (
                        f"来源窗口 {visible_window}/{self._total} 的第 {attempt} "
                        "次尝试未通过，Host 将按合同决定是否重试。"
                        if self._total is not None
                        else (
                            f"相邻范围 {reconciliation_group}/"
                            f"{reconciliation_group_count} 的第 {attempt} 次尝试未通过。"
                        )
                    ),
                    "current_window": visible_window,
                    "total_windows": (
                        self._total
                        if self._total is not None
                        else reconciliation_group_count or None
                    ),
                    "overall_total_windows": self._overall_total,
                    "checkpoint_count": checkpoint_count,
                    "validated_batch_count": validated_batches,
                    "total_model_batch_count": self._total_model_batches,
                    "active_target_count": active_target_count,
                    "attempt": attempt,
                    "agent_call_count": self._calls,
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                    "outcome": "FAILED",
                    "reason_code": reason_code,
                    "strategy": "SOURCE_WINDOW_AGENT",
                    "current_reconciliation_group": reconciliation_group or None,
                    "total_reconciliation_groups": (reconciliation_group_count or None),
                }
            )
            raise
        self._progress.heartbeat(
            progress={
                "stage": self._stage,
                "message": (
                    f"来源窗口 {visible_window}/{self._total} 的第 {attempt} "
                    "次 Agent 输出已返回，正在执行 Host 对象级验证。"
                    if self._total is not None
                    else (
                        f"相邻范围 {reconciliation_group}/{reconciliation_group_count} "
                        "输出已返回，正在执行 Host 验证。"
                    )
                ),
                "current_window": visible_window,
                "total_windows": (
                    self._total
                    if self._total is not None
                    else reconciliation_group_count or None
                ),
                "overall_total_windows": self._overall_total,
                "checkpoint_count": checkpoint_count,
                "validated_batch_count": validated_batches,
                "total_model_batch_count": self._total_model_batches,
                "active_target_count": active_target_count,
                "attempt": attempt,
                "agent_call_count": self._calls,
                "elapsed_seconds": round(time.monotonic() - started, 1),
                "outcome": "RETURNED",
                "strategy": "SOURCE_WINDOW_AGENT",
                "current_reconciliation_group": reconciliation_group or None,
                "total_reconciliation_groups": reconciliation_group_count or None,
            }
        )
        return result


def _estimated_source_window_count(
    targets,
    *,
    max_targets: int,
    max_source_characters: int,
    excluded_batch_ids: frozenset[str] = frozenset(),
) -> int:  # type: ignore[no-untyped-def]
    count = 0
    window_targets = 0
    window_characters = 0
    for target in targets:
        if (
            target.batch_id in excluded_batch_ids
            or target.execution_mode.value != "MODEL_TARGET"
        ):
            if window_targets:
                count += 1
                window_targets = 0
                window_characters = 0
            continue
        characters = sum(item.source_text_length for item in target.segment_bindings)
        if window_targets and (
            window_targets >= max_targets
            or window_characters + characters > max_source_characters
        ):
            count += 1
            window_targets = 0
            window_characters = 0
        window_targets += 1
        window_characters += characters
    if window_targets:
        count += 1
    return count


def _window_checkpoint_sink(
    *,
    object_store,
    baseline: NativeStructureAcceptedBaselineV1,
    plan: ParentProvisionObjectBatchPlanV1,
    progress: _NativeParentProgress,
):  # type: ignore[no-untyped-def]
    def save(
        values: tuple[
            tuple[
                ParentProvisionRecognitionBatchReceiptV1,
                ParentProvisionObjectBatchCompilationV1,
            ],
            ...,
        ],
    ) -> None:
        window_ordinal = len(progress.window_checkpoints) + 1
        checkpoint_values = {
            "content_bearing": True,
            "private_object_store_only": True,
            "baseline_id": baseline.baseline_id,
            "baseline_sha256": baseline.baseline_sha256,
            "plan_id": plan.plan_id,
            "plan_sha256": plan.plan_sha256,
            "window_ordinal": window_ordinal,
            "batch_receipts": tuple(item[0] for item in values),
            "compilations": tuple(item[1] for item in values),
        }
        draft = NativeParentWindowCheckpointArtifactV1.model_construct(
            **checkpoint_values,
            checkpoint_sha256="0" * 64,
        )
        checkpoint = NativeParentWindowCheckpointArtifactV1(
            **checkpoint_values,
            checkpoint_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
            ),
        )
        encoded = json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        object_key = (
            "runtime/agent-parent-window-checkpoints/"
            f"{baseline.project_id}/{baseline.document_id}/{baseline.source_sha256}/"
            f"{plan.plan_sha256}/{window_ordinal:04d}-{checkpoint.checkpoint_sha256}.json"
        )
        stored = object_store.put(
            key=object_key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type="application/vnd.reqpilotx.parent-window-checkpoint+json",
        )
        progress.add_window_checkpoint(
            {
                "schema_version": "native-parent-window-checkpoint-receipt/v1",
                "window_ordinal": window_ordinal,
                "batch_ids": [item[0].batch_id for item in values],
                "object_key": stored.key,
                "object_sha256": stored.sha256,
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
            }
        )

    return save


def _load_window_checkpoints(
    object_store,
    *,
    records: tuple[dict[str, Any], ...],
    baseline: NativeStructureAcceptedBaselineV1,
    plan: ParentProvisionObjectBatchPlanV1,
) -> tuple[
    tuple[ParentProvisionRecognitionBatchReceiptV1, ...],
    tuple[ParentProvisionObjectBatchCompilationV1, ...],
]:  # type: ignore[no-untyped-def]
    if not records:
        return (), ()
    receipts: list[ParentProvisionRecognitionBatchReceiptV1] = []
    compilations: list[ParentProvisionObjectBatchCompilationV1] = []
    observed_batch_ids: set[str] = set()
    prefix = (
        "runtime/agent-parent-window-checkpoints/"
        f"{baseline.project_id}/{baseline.document_id}/{baseline.source_sha256}/"
        f"{plan.plan_sha256}/"
    )
    try:
        for expected_ordinal, record in enumerate(records, start=1):
            if (
                record.get("schema_version")
                != "native-parent-window-checkpoint-receipt/v1"
                or int(record.get("window_ordinal") or 0) != expected_ordinal
            ):
                raise ValueError("checkpoint receipt order drifted")
            key = str(record.get("object_key") or "")
            if not key.startswith(prefix):
                raise ValueError("checkpoint key drifted")
            with object_store.open(key) as stream:
                encoded = stream.read()
            if hashlib.sha256(encoded).hexdigest() != str(
                record.get("object_sha256") or ""
            ):
                raise ValueError("checkpoint object SHA drifted")
            checkpoint = NativeParentWindowCheckpointArtifactV1.model_validate_json(
                encoded
            )
            batch_ids = tuple(str(item) for item in record.get("batch_ids", ()))
            if (
                checkpoint.baseline_id != baseline.baseline_id
                or checkpoint.baseline_sha256 != baseline.baseline_sha256
                or checkpoint.plan_id != plan.plan_id
                or checkpoint.plan_sha256 != plan.plan_sha256
                or checkpoint.window_ordinal != expected_ordinal
                or checkpoint.checkpoint_sha256
                != str(record.get("checkpoint_sha256") or "")
                or tuple(item.batch_id for item in checkpoint.batch_receipts)
                != batch_ids
                or any(item in observed_batch_ids for item in batch_ids)
            ):
                raise ValueError("checkpoint lineage drifted")
            observed_batch_ids.update(batch_ids)
            receipts.extend(checkpoint.batch_receipts)
            compilations.extend(checkpoint.compilations)
    except (OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="native_parent_window_checkpoint_invalid",
            message="A saved parent-window checkpoint is invalid or stale.",
        ) from exc
    rank = {item.batch_id: item.batch_order for item in plan.targets}
    receipts.sort(key=lambda item: rank[item.batch_id])
    compilations.sort(key=lambda item: rank[item.batch_id])
    return tuple(receipts), tuple(compilations)


def _reconciliation_checkpoint_sink(
    *,
    object_store,
    baseline: NativeStructureAcceptedBaselineV1,
    object_plan: ParentProvisionObjectBatchPlanV1,
    progress: _NativeParentProgress,
):  # type: ignore[no-untyped-def]
    def save(
        reconciliation_plan: ParentProvisionObjectReconciliationPlanV1,
        group: ParentProvisionObjectReconciliationGroupV1,
        receipt: ParentProvisionReconciliationGroupReceiptV1,
        compilation: ParentProvisionObjectReconciliationCompilationV1,
    ) -> None:
        checkpoint_values = {
            "content_bearing": True,
            "private_object_store_only": True,
            "baseline_id": baseline.baseline_id,
            "baseline_sha256": baseline.baseline_sha256,
            "object_plan_id": object_plan.plan_id,
            "object_plan_sha256": object_plan.plan_sha256,
            "reconciliation_plan_id": reconciliation_plan.reconciliation_plan_id,
            "reconciliation_plan_sha256": (
                reconciliation_plan.reconciliation_plan_sha256
            ),
            "group_id": group.group_id,
            "group_order": group.group_order,
            "group_scope_sha256": group.group_scope_sha256,
            "group_receipt": receipt,
            "compilation": compilation,
        }
        draft = NativeParentReconciliationCheckpointArtifactV1.model_construct(
            **checkpoint_values,
            checkpoint_sha256="0" * 64,
        )
        checkpoint = NativeParentReconciliationCheckpointArtifactV1(
            **checkpoint_values,
            checkpoint_sha256=canonical_json_sha256(
                draft.model_dump(mode="json", exclude={"checkpoint_sha256"})
            ),
        )
        encoded = json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object_sha256 = hashlib.sha256(encoded).hexdigest()
        object_key = (
            "runtime/agent-parent-reconciliation-checkpoints/"
            f"{baseline.project_id}/{baseline.document_id}/{baseline.source_sha256}/"
            f"{object_plan.plan_sha256}/"
            f"{reconciliation_plan.reconciliation_plan_sha256}/"
            f"{group.group_order:04d}-{checkpoint.checkpoint_sha256}.json"
        )
        stored = object_store.put(
            key=object_key,
            source=BytesIO(encoded),
            expected_sha256=object_sha256,
            media_type=(
                "application/vnd.reqpilotx.parent-reconciliation-checkpoint+json"
            ),
        )
        progress.add_reconciliation_checkpoint(
            {
                "schema_version": (
                    "native-parent-reconciliation-checkpoint-receipt/v1"
                ),
                "group_id": group.group_id,
                "group_order": group.group_order,
                "object_key": stored.key,
                "object_sha256": stored.sha256,
                "checkpoint_sha256": checkpoint.checkpoint_sha256,
                "reconciliation_plan_sha256": (
                    reconciliation_plan.reconciliation_plan_sha256
                ),
            }
        )

    return save


def _load_reconciliation_checkpoints(
    object_store,
    *,
    records: tuple[dict[str, Any], ...],
    baseline: NativeStructureAcceptedBaselineV1,
    object_plan: ParentProvisionObjectBatchPlanV1,
) -> tuple[
    tuple[ParentProvisionReconciliationGroupReceiptV1, ...],
    tuple[ParentProvisionObjectReconciliationCompilationV1, ...],
]:  # type: ignore[no-untyped-def]
    if not records:
        return (), ()
    receipts: list[ParentProvisionReconciliationGroupReceiptV1] = []
    compilations: list[ParentProvisionObjectReconciliationCompilationV1] = []
    observed_group_ids: set[str] = set()
    prefix = (
        "runtime/agent-parent-reconciliation-checkpoints/"
        f"{baseline.project_id}/{baseline.document_id}/{baseline.source_sha256}/"
        f"{object_plan.plan_sha256}/"
    )
    try:
        ordered_records = tuple(
            sorted(records, key=lambda item: int(item.get("group_order") or 0))
        )
        for record in ordered_records:
            if (
                record.get("schema_version")
                != "native-parent-reconciliation-checkpoint-receipt/v1"
            ):
                raise ValueError("reconciliation checkpoint receipt schema drifted")
            key = str(record.get("object_key") or "")
            if not key.startswith(prefix):
                raise ValueError("reconciliation checkpoint key drifted")
            with object_store.open(key) as stream:
                encoded = stream.read()
            if hashlib.sha256(encoded).hexdigest() != str(
                record.get("object_sha256") or ""
            ):
                raise ValueError("reconciliation checkpoint object SHA drifted")
            checkpoint = (
                NativeParentReconciliationCheckpointArtifactV1.model_validate_json(
                    encoded
                )
            )
            if (
                checkpoint.baseline_id != baseline.baseline_id
                or checkpoint.baseline_sha256 != baseline.baseline_sha256
                or checkpoint.object_plan_id != object_plan.plan_id
                or checkpoint.object_plan_sha256 != object_plan.plan_sha256
                or checkpoint.group_id != str(record.get("group_id") or "")
                or checkpoint.group_order != int(record.get("group_order") or 0)
                or checkpoint.checkpoint_sha256
                != str(record.get("checkpoint_sha256") or "")
                or checkpoint.reconciliation_plan_sha256
                != str(record.get("reconciliation_plan_sha256") or "")
                or checkpoint.group_id in observed_group_ids
            ):
                raise ValueError("reconciliation checkpoint lineage drifted")
            observed_group_ids.add(checkpoint.group_id)
            receipts.append(checkpoint.group_receipt)
            compilations.append(checkpoint.compilation)
    except (OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="native_parent_reconciliation_checkpoint_invalid",
            message="A saved reconciliation checkpoint is invalid or stale.",
        ) from exc
    return tuple(receipts), tuple(compilations)


def _has_complete_working_decisions(proposal: ParentProvisionProposalV3) -> bool:
    return (
        proposal.status is ParentProvisionStatus.COMPLETE
        and not proposal.unresolved_logical_object_ids
        and not proposal.diagnostic_codes
        and all(
            item.requirementness is not Requirementness.UNRESOLVED
            and getattr(item.requirement_kind, "value", None) != "UNRESOLVED"
            and getattr(item.technicality, "value", None) != "UNRESOLVED"
            for item in proposal.entries
        )
    )


def _load_baseline_artifact(
    object_store,
    *,
    key: str,
    expected_object_sha256: str,
    expected_artifact_sha256: str,
    expected_baseline: NativeStructureAcceptedBaselineV1,
) -> NativeStructureBaselineArtifactV1:  # type: ignore[no-untyped-def]
    if key != native_structure_baseline_object_key(expected_baseline):
        raise NonRetryableJobError(
            error_code="native_parent_baseline_object_key_invalid",
            message="Accepted baseline object key is invalid.",
        )
    try:
        with object_store.open(key) as stream:
            encoded = stream.read()
        if hashlib.sha256(encoded).hexdigest() != expected_object_sha256:
            raise ValueError("object SHA drifted")
        artifact = NativeStructureBaselineArtifactV1.model_validate_json(encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise NonRetryableJobError(
            error_code="native_parent_baseline_artifact_invalid",
            message="Accepted baseline private artifact is missing or invalid.",
        ) from exc
    if (
        artifact.identity != expected_baseline
        or artifact.artifact_sha256 != expected_artifact_sha256
    ):
        raise NonRetryableJobError(
            error_code="native_parent_baseline_artifact_changed",
            message="Accepted baseline private artifact changed before D3 execution.",
        )
    return artifact


def _load_parent_result_artifact(
    object_store,
    *,
    result: dict[str, Any],
    expected_baseline: NativeStructureAcceptedBaselineV1,
) -> NativeParentProvisionResultArtifactV1:  # type: ignore[no-untyped-def]
    private = result.get("private_artifact")
    private = private if isinstance(private, dict) else {}
    key = str(private.get("object_key") or "")
    expected_object_sha256 = str(private.get("object_sha256") or "")
    expected_artifact_sha256 = str(private.get("artifact_sha256") or "")
    if not key.startswith(
        "runtime/agent-parent-provisions/"
        f"{expected_baseline.project_id}/{expected_baseline.document_id}/"
        f"{expected_baseline.source_sha256}/"
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_parent_provision_artifact_invalid",
                "message": "The validated parent-provision artifact key is invalid.",
            },
        )
    try:
        with object_store.open(key) as stream:
            encoded = stream.read()
        if hashlib.sha256(encoded).hexdigest() != expected_object_sha256:
            raise ValueError("parent result object SHA drifted")
        artifact = NativeParentProvisionResultArtifactV1.model_validate_json(encoded)
    except (OSError, TypeError, ValueError) as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_parent_provision_artifact_invalid",
                "message": "The validated parent-provision artifact is invalid.",
            },
        ) from exc
    if (
        artifact.baseline != expected_baseline
        or artifact.artifact_sha256 != expected_artifact_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_parent_provision_artifact_changed",
                "message": "The validated parent-provision artifact changed.",
            },
        )
    return artifact


def _public_parent_entry(
    entry,
    *,
    segment_by_id: dict[str, Any],
    source_nodes: dict[str, Any],
    page_bounds: dict[int, tuple[float, float, float, float]],
) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    ranges: list[dict[str, Any]] = []
    for source_range in entry.source_ranges:
        segment = segment_by_id.get(source_range.segment_id)
        if segment is None:
            raise ApplicationError(
                status_code=409,
                detail={
                    "code": "native_parent_provision_segment_missing",
                    "message": "A validated parent source segment is missing.",
                },
            )
        page = int(segment.page_numbers[0]) if segment.page_numbers else 0
        bbox, precision = _segment_bbox(
            segment,
            source_nodes=source_nodes,
        )
        ranges.append(
            {
                "segment_id": source_range.segment_id,
                "start_offset": source_range.start_offset,
                "end_offset": source_range.end_offset,
                "verbatim_text": source_range.verbatim_text,
                "verbatim_text_sha256": source_range.verbatim_text_sha256,
                "page": page,
                "bbox": list(bbox) if bbox is not None else None,
                "source_bounds": (
                    list(page_bounds[page]) if page in page_bounds else None
                ),
                "precision": precision,
            }
        )
    return {
        "parent_provision_id": entry.parent_provision_id,
        "source_order": entry.source_order,
        "logical_object_ids": list(entry.logical_object_ids),
        "explicit_label": entry.explicit_label,
        "ancestor_labels": list(entry.ancestor_labels),
        "requirementness": entry.requirementness.value,
        "requirement_kind": (
            entry.requirement_kind.value if entry.requirement_kind is not None else None
        ),
        "technicality": (
            entry.technicality.value if entry.technicality is not None else None
        ),
        "content_role": entry.content_role.value,
        "confidence": entry.confidence,
        "reason_codes": list(entry.reason_codes),
        "source_ranges": ranges,
        "verbatim_text": "\n".join(item["verbatim_text"] for item in ranges),
    }


def _segment_bbox(
    segment,
    *,
    source_nodes: dict[str, Any],
) -> tuple[tuple[float, float, float, float] | None, str]:  # type: ignore[no-untyped-def]
    node = source_nodes.get(str(segment.source_node_id))
    if node is None:
        return None, "SOURCE_NODE_MISSING"
    if str(segment.evidence_kind.value) == "TABLE_CELL" and node.table_payload:
        cells = tuple(
            sorted(
                node.table_payload.cells,
                key=lambda item: (item.row, item.column, item.cell_id),
            )
        )
        ordinal = int(segment.intra_node_ordinal)
        if 0 <= ordinal < len(cells) and cells[ordinal].native_locator.bbox:
            return cells[ordinal].native_locator.bbox, "EXACT_TABLE_CELL"
    return node.native_locator.bbox, "EXACT_SOURCE_BLOCK"


def _parent_result_artifact(
    *,
    baseline,
    workflow,
    professional,
):  # type: ignore[no-untyped-def]
    finalized = workflow.finalized_execution_receipt
    if finalized is None:
        raise ValueError("native parent workflow has no finalized execution receipt")
    values = {
        "content_bearing": True,
        "private_object_store_only": True,
        "baseline": baseline,
        "proposal": professional.proposal,
        "professional_execution_receipt": (professional.professional_execution_receipt),
        "professional_policy_attestation": (
            professional.professional_policy_attestation
        ),
        "host_validation_receipt": professional.host_validation_receipt,
        "stage_artifact": professional.stage_artifact,
        "final_receipt": professional.final_receipt,
        "aggregate_execution_receipt": workflow.object_result.execution_receipt,
        "finalized_execution_receipt": finalized,
        "professional_proof": professional.professional_proof,
        "reconciliation_execution_receipt": (
            workflow.coordination_result.execution_receipt
            if workflow.coordination_result is not None
            else None
        ),
    }
    draft = NativeParentProvisionResultArtifactV1.model_construct(
        **values,
        artifact_sha256="0" * 64,
    )
    return NativeParentProvisionResultArtifactV1(
        **values,
        artifact_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"artifact_sha256"})
        ),
    )


def _parent_result_object_key(
    baseline: NativeStructureAcceptedBaselineV1,
    artifact: NativeParentProvisionResultArtifactV1,
) -> str:
    return (
        "runtime/agent-parent-provisions/"
        f"{baseline.project_id}/{baseline.document_id}/{baseline.source_sha256}/"
        f"{artifact.artifact_sha256}.json"
    )


def _parent_job_matches(
    job: DurableJob | None,
    baseline: NativeStructureAcceptedBaselineV1,
) -> bool:
    result = dict(job.result or {}) if job is not None else {}
    return bool(
        job
        and job.status is JobStatus.SUCCEEDED
        and result.get("schema_version")
        == NATIVE_PARENT_PROVISION_RESULT_SCHEMA_VERSION
        and result.get("status") == "COMPLETE"
        and result.get("baseline_id") == baseline.baseline_id
        and result.get("baseline_sha256") == baseline.baseline_sha256
    )


def _parent_job_payload_matches(
    job: DurableJob | None,
    baseline: NativeStructureAcceptedBaselineV1,
) -> bool:
    if job is None:
        return False
    payload_baseline = job.payload.get("baseline")
    if not isinstance(payload_baseline, dict):
        return False
    return bool(
        payload_baseline.get("baseline_id") == baseline.baseline_id
        and payload_baseline.get("baseline_sha256") == baseline.baseline_sha256
        and job.payload.get("source_sha256") == baseline.source_sha256
    )


def _job_projection(job: DurableJob) -> dict[str, Any]:
    result = dict(job.result or {})
    return {
        "schema_version": "native-parent-provision-job-public/v1",
        "job_id": job.job_id,
        "status": job.status.value,
        "generation": job.generation,
        "progress": dict(job.progress or {}),
        "error_code": job.error_code,
        "execution_strategy": str(
            result.get("execution_strategy")
            or job.payload.get("execution_strategy")
            or ""
        ),
        "artifact_id": str(result.get("artifact_id") or ""),
        "artifact_sha256": str(result.get("artifact_sha256") or ""),
        "parent_provision_count": int(result.get("parent_provision_count") or 0),
        "declared_scope_count": int(result.get("declared_scope_count") or 0),
        "source_window_count": int(result.get("source_window_count") or 0),
        "requirementness_counts": dict(result.get("requirementness_counts") or {}),
        "professional_policy_attested": (
            result.get("professional_policy_attested") is True
        ),
        "next_action": str(result.get("next_action") or ""),
    }


def _status_message(job: DurableJob | None) -> str:
    if job is None:
        return "accepted baseline 就绪后，将由 Agent 按来源窗口判断父条款和需求属性。"
    if job.status is JobStatus.SUCCEEDED:
        return "整文父条款与需求属性已由 Agent 判断并通过 Host 全文验证。"
    if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
        return str(job.progress.get("message") or "Agent 正在处理整文需求基线。")
    if job.status is JobStatus.FAILED:
        return "来源窗口结果尚未通过 Host 全文验证；原文结构与 accepted baseline 保持可用。"
    if job.status is JobStatus.CANCELLED:
        return "父条款识别已取消；accepted baseline 保持可用。"
    return "父条款识别正在处理。"


__all__ = [
    "NATIVE_PARENT_PROVISION_JOB_KIND",
    "NativeParentProvisionResultArtifactV1",
    "enqueue_native_parent_provision_recognition",
    "get_native_parent_provision_result",
    "get_native_parent_provision_status",
    "run_durable_native_parent_provision_recognition",
]
