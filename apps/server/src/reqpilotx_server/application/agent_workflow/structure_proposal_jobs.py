from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import TypeAdapter

from reqpilotx_server.application.agent_workflow.contracts import (
    AgentStructureProposalReviseRequest,
    AgentStructureProposalStartRequest,
    AgentStructureProposalStatus,
    AgentStructureProposalSteerRequest,
)
from reqpilotx_server.application.agent_workflow.structure_steers import (
    create_agent_structure_steer,
    serialize_agent_structure_steer,
)
from reqpilotx_server.application.agent_workflow.structure_proposals import (
    create_ready_agent_structure_proposal,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.revise_structure import (
    create_structure_patch_proposal,
    validate_structure_patch_proposal,
)
from reqpilotx_server.application.extraction.structure_optimization_jobs import (
    _load_current_structure,
)
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.domain.extraction.structure_revision import canonical_model_sha256
from reqpilotx_server.domain.extraction.structure_revision import StructurePatchOperationV1
from reqpilotx_server.ports.agent_runtime import (
    AgentRuntimeInvocationError,
    AgentRuntimeSteeringPort,
    AgentRuntimeUnavailableError,
    AgentStructureProposalTaskV1,
    AgentStructureSteerV1,
)
from reqpilotx_server.ports.agent_structure_steer_repository import (
    AgentStructureSteerCommand,
    AgentStructureSteerConflict,
    AgentStructureSteerRepository,
)
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobStatus,
    NonRetryableJobError,
)


AGENT_STRUCTURE_PROPOSAL_JOB_SCHEMA_VERSION = "agent-structure-proposal-job/v1"
AGENT_STRUCTURE_PROPOSAL_JOB_KIND = "agent.structure.propose"


def start_agent_structure_proposal(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalStartRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    return _start_agent_structure_proposal(
        project_id,
        document_id,
        payload,
        response,
        supersedes_proposal_record_id="",
    )


def start_agent_structure_proposal_revision(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalReviseRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import _agent_structure_proposals

    predecessor = _agent_structure_proposals().get(payload.proposal_record_id)
    if (
        predecessor is None
        or predecessor.project_id != project_id
        or predecessor.document_id != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_structure_proposal_not_found",
                "message": "Agent Structure proposal was not found.",
            },
        )
    if (
        predecessor.proposal_payload_sha256
        != payload.expected_proposal_payload_sha256
        or predecessor.status is not AgentStructureProposalStatus.READY
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_proposal_conflict",
                "message": "Only the current READY proposal can be revised.",
            },
        )
    return _start_agent_structure_proposal(
        project_id,
        document_id,
        payload,
        response,
        supersedes_proposal_record_id=predecessor.proposal_record_id,
    )


def _start_agent_structure_proposal(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalStartRequest,
    response: ResponseSink,
    *,
    supersedes_proposal_record_id: str,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_customer_data_enabled,
        _agent_runtime,
        _durable_jobs,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    try:
        runtime = _agent_runtime()
    except RuntimeError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_runtime_not_configured",
                "message": "Agent Runtime is not configured for this deployment.",
            },
        ) from exc
    if not _agent_customer_data_enabled():
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_customer_data_disabled",
                "message": (
                    "Agent Runtime customer-data execution is disabled for this "
                    "deployment."
                ),
            },
        )
    (
        _document,
        _extraction_job,
        _extraction_result,
        _agent,
        source_ir,
        base_tree,
        _revisions,
        current,
    ) = _load_current_structure(project_id, document_id)
    if (
        payload.expected_source_sha256 != source_ir.source_sha256
        or payload.expected_revision_number != current.revision_number
        or payload.expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_proposal_stale_base",
                "message": "Structure changed after the proposal command was prepared.",
                "current_source_sha256": source_ir.source_sha256,
                "current_revision_number": current.revision_number,
                "current_effective_snapshot_id": current.effective_snapshot_id,
            },
        )
    current_node_ids = {node.structure_node_id for node in current.nodes}
    if any(
        node_id not in current_node_ids for node_id in payload.focus_structure_node_ids
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_focus_stale",
                "message": "One or more focused Structure nodes are no longer current.",
            },
        )
    facts_sha256 = _facts_sha256(source_ir, base_tree, current)
    job_payload = {
        "schema_version": AGENT_STRUCTURE_PROPOSAL_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "command_id": payload.command_id,
        "source_sha256": source_ir.source_sha256,
        "facts_sha256": facts_sha256,
        "source_ir_id": source_ir.source_ir_id,
        "source_ir_canonical_sha256": _source_ir_canonical_sha256(source_ir),
        "base_structure_snapshot_id": base_tree.structure_snapshot_id,
        "base_structure_canonical_sha256": canonical_model_sha256(base_tree),
        "expected_revision_number": current.revision_number,
        "expected_effective_snapshot_id": current.effective_snapshot_id,
        "expected_effective_tree_sha256": current.effective_tree_sha256,
        "runtime_adapter_id": runtime.adapter_id,
        "runtime_adapter_version": runtime.adapter_version,
        "model_id": runtime.model_id,
        "skill_id": runtime.skill_id,
        "skill_version": runtime.skill_version,
        "reasoning_effort": "medium",
        "deadline_seconds": 180,
        "intent": payload.intent,
        "focus_structure_node_ids": list(payload.focus_structure_node_ids),
        "suggested_operations": [
            operation.model_dump(mode="json")
            for operation in payload.suggested_operations
        ],
        "suggested_operations_authority": payload.suggested_operations_authority,
        "supersedes_proposal_record_id": supersedes_proposal_record_id,
    }
    idempotency_key = _canonical_sha256(job_payload)
    job = _durable_jobs().enqueue(
        kind=AGENT_STRUCTURE_PROPOSAL_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=idempotency_key,
        payload=job_payload,
        priority=20,
        max_attempts=2,
    )
    response.status_code = 202
    return _job_projection(job)


def get_agent_structure_proposal_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_structure_proposals,
        _agent_structure_steers,
        _durable_jobs,
    )

    _require_project_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    job = _durable_jobs().get_latest(
        kind=AGENT_STRUCTURE_PROPOSAL_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    proposal = _agent_structure_proposals().get_latest(
        project_id=project_id,
        document_id=document_id,
    )
    steer = (
        _agent_structure_steers().get_latest_for_job(job.job_id)
        if job is not None
        else None
    )
    return {
        "schema_version": "agent-structure-proposal-status/v1",
        "project_id": project_id,
        "document_id": document_id,
        "job": _job_projection(job) if job is not None else None,
        "proposal": (
            serialize_agent_structure_proposal(proposal)
            if proposal is not None
            else None
        ),
        "steer": serialize_agent_structure_steer(steer),
    }


def steer_active_agent_structure_proposal(
    project_id: str,
    document_id: str,
    payload: AgentStructureProposalSteerRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        _agent_customer_data_enabled,
        _agent_runtime,
        _agent_structure_steers,
        _durable_jobs,
    )

    response.headers.update(_ASSET_RESPONSE_HEADERS)
    try:
        _agent_runtime()
    except RuntimeError as exc:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_runtime_not_configured",
                "message": "Agent Runtime is not configured for this deployment.",
            },
        ) from exc
    if not _agent_customer_data_enabled():
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "agent_customer_data_disabled",
                "message": (
                    "Agent Runtime customer-data execution is disabled for this "
                    "deployment."
                ),
            },
        )
    job = _durable_jobs().get(payload.job_id)
    if (
        job is None
        or job.kind != AGENT_STRUCTURE_PROPOSAL_JOB_KIND
        or job.aggregate_type != "sor-document"
        or job.aggregate_id != document_id
    ):
        raise ApplicationError(
            status_code=404,
            detail={
                "code": "agent_structure_job_not_found",
                "message": "The active Agent Structure job was not found.",
            },
        )
    if (
        job.status is not JobStatus.RUNNING
        or job.generation != payload.expected_job_generation
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_turn_not_active",
                "message": "The selected Agent turn is no longer active.",
                "current_job_status": job.status.value,
                "current_job_generation": job.generation,
            },
        )
    (
        _document,
        _extraction_job,
        _extraction_result,
        _agent,
        source_ir,
        base_tree,
        revisions,
        current,
    ) = _load_current_structure(project_id, document_id)
    if (
        payload.expected_source_sha256 != source_ir.source_sha256
        or payload.expected_revision_number != current.revision_number
        or payload.expected_effective_snapshot_id != current.effective_snapshot_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_steer_stale_base",
                "message": "Structure changed before the steer command was accepted.",
            },
        )
    current_node_ids = {node.structure_node_id for node in current.nodes}
    if any(
        node_id not in current_node_ids for node_id in payload.focus_structure_node_ids
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_focus_stale",
                "message": "One or more focused Structure nodes are no longer current.",
            },
        )
    if payload.suggested_operations:
        create_structure_patch_proposal(
            source_ir,
            base_tree,
            revisions,
            payload.suggested_operations,
        )
    try:
        command = create_agent_structure_steer(
            _agent_structure_steers(),
            project_id=project_id,
            document_id=document_id,
            payload=payload,
        )
    except AgentStructureSteerConflict as exc:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "agent_structure_steer_conflict",
                "message": str(exc),
            },
        ) from exc
    response.status_code = 202
    return serialize_agent_structure_steer(command)


def run_durable_agent_structure_proposal(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import (
        _agent_runtime,
        _agent_structure_proposals,
        _agent_structure_steers,
    )

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != AGENT_STRUCTURE_PROPOSAL_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="agent_structure_job_schema_invalid",
            message="Agent Structure proposal job schema is invalid.",
        )
    runtime = _agent_runtime()
    steer_repository = _agent_structure_steers()
    steer_repository.reject_other_generations(
        job_id=durable_job.job_id,
        current_job_generation=durable_job.generation,
        reason_code="job_generation_superseded",
    )
    if (
        payload.get("runtime_adapter_id") != runtime.adapter_id
        or payload.get("runtime_adapter_version") != runtime.adapter_version
        or payload.get("model_id") != runtime.model_id
        or payload.get("skill_id") != runtime.skill_id
        or payload.get("skill_version") != runtime.skill_version
    ):
        raise NonRetryableJobError(
            error_code="agent_structure_runtime_identity_changed",
            message="Agent Runtime identity changed after enqueue.",
        )
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    (
        _document,
        _extraction_job,
        _extraction_result,
        _agent,
        source_ir,
        base_tree,
        revisions,
        current,
    ) = _load_current_structure(project_id, document_id)
    if not _payload_matches_current(payload, source_ir, base_tree, current):
        raise NonRetryableJobError(
            error_code="agent_structure_proposal_stale_base",
            message="Structure changed before Agent proposal execution.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="agent_structure_proposal_cancelled",
            message="Agent Structure proposal was cancelled before inference.",
        )
    suggested_operations = TypeAdapter(
        tuple[StructurePatchOperationV1, ...]
    ).validate_python(payload.get("suggested_operations") or ())
    task = AgentStructureProposalTaskV1(
        run_id=durable_job.job_id,
        project_id=project_id,
        document_id=document_id,
        facts_sha256=str(payload["facts_sha256"]),
        source_ir=source_ir,
        base_structure=base_tree,
        structure_revisions=revisions,
        effective_structure=current,
        model_id=str(payload["model_id"]),
        reasoning_effort=str(payload["reasoning_effort"]),
        skill_id=str(payload["skill_id"]),
        skill_version=str(payload["skill_version"]),
        deadline_seconds=int(payload["deadline_seconds"]),
        intent=str(payload.get("intent") or "GENERAL"),
        focus_structure_node_ids=tuple(payload.get("focus_structure_node_ids") or ()),
        suggested_operations=suggested_operations,
        suggested_operations_authority=str(
            payload.get("suggested_operations_authority") or "NONE"
        ),
    )
    try:
        runtime_result = runtime.propose_structure(
            task,
            progress=lambda stage: context.heartbeat(progress={"stage": stage}),
            cancellation_requested=context.cancellation_requested,
            steering=_DurableAgentStructureSteering(
                steer_repository,
                job_id=durable_job.job_id,
                job_generation=durable_job.generation,
            ),
        )
    except AgentRuntimeUnavailableError as exc:
        raise RuntimeError(
            f"Agent Runtime is temporarily unavailable: {exc.reason_code}."
        ) from exc
    except AgentRuntimeInvocationError as exc:
        raise NonRetryableJobError(
            error_code=exc.reason_code,
            message="Agent Runtime rejected the Structure proposal run.",
        ) from exc
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="agent_structure_proposal_cancelled",
            message="Agent Structure proposal was cancelled before publication.",
        )
    missed_steer_count = steer_repository.reject_unfinished(
        job_id=durable_job.job_id,
        expected_job_generation=durable_job.generation,
        reason_code="turn_completed_before_steer_delivery",
    )
    if missed_steer_count:
        raise NonRetryableJobError(
            error_code="agent_structure_steer_missed_terminal",
            message="The active turn completed before a steer command was delivered.",
        )
    if runtime_result.proposal is None:
        return {
            "schema_version": "agent-structure-proposal-job-result/v1",
            "proposal_record_id": "",
            "proposal_revision": 0,
            "proposal_payload_sha256": "",
            "status": "NO_PROPOSAL",
            "reason_code": runtime_result.no_proposal_reason_code,
            "operation_count": 0,
            "source_sha256": source_ir.source_sha256,
            "facts_sha256": str(payload["facts_sha256"]),
        }
    supersedes_proposal_record_id = str(
        payload.get("supersedes_proposal_record_id") or ""
    )
    if supersedes_proposal_record_id:
        predecessor = _agent_structure_proposals().get(
            supersedes_proposal_record_id
        )
        if (
            predecessor is None
            or predecessor.project_id != project_id
            or predecessor.document_id != document_id
            or predecessor.status is not AgentStructureProposalStatus.READY
        ):
            raise NonRetryableJobError(
                error_code="agent_structure_revision_predecessor_changed",
                message="The proposal selected for revision is no longer READY.",
            )
    # Re-read current state immediately before publication.
    (
        _document,
        _extraction_job,
        _extraction_result,
        _agent,
        fresh_source_ir,
        fresh_base_tree,
        fresh_revisions,
        fresh_current,
    ) = _load_current_structure(project_id, document_id)
    if not _payload_matches_current(
        payload,
        fresh_source_ir,
        fresh_base_tree,
        fresh_current,
    ):
        raise NonRetryableJobError(
            error_code="agent_structure_proposal_stale_before_publication",
            message="Structure changed before proposal publication.",
        )
    validate_structure_patch_proposal(
        runtime_result.proposal,
        fresh_source_ir,
        fresh_base_tree,
        fresh_revisions,
    )
    context.begin_publication()
    proposal = create_ready_agent_structure_proposal(
        _agent_structure_proposals(),
        project_id=project_id,
        document_id=document_id,
        source_sha256=fresh_source_ir.source_sha256,
        facts_sha256=str(payload["facts_sha256"]),
        structure_patch_proposal=runtime_result.proposal,
        runtime_receipt=runtime_result.runtime_receipt,
        runtime_thread_id=runtime_result.runtime_thread_id,
        command_id=f"{durable_job.job_id}:{durable_job.generation}:publish",
    )
    return {
        "schema_version": "agent-structure-proposal-job-result/v1",
        "proposal_record_id": proposal.proposal_record_id,
        "proposal_revision": proposal.proposal_revision,
        "proposal_payload_sha256": proposal.proposal_payload_sha256,
        "status": proposal.status.value,
        "operation_count": len(proposal.structure_patch_proposal.operations),
        "source_sha256": proposal.source_sha256,
        "facts_sha256": proposal.facts_sha256,
    }


def _facts_sha256(source_ir: Any, base_tree: Any, current: Any) -> str:
    return _canonical_sha256(
        {
            "source_ir_id": source_ir.source_ir_id,
            "source_sha256": source_ir.source_sha256,
            "source_ir_canonical_sha256": _source_ir_canonical_sha256(source_ir),
            "base_structure_snapshot_id": base_tree.structure_snapshot_id,
            "base_structure_canonical_sha256": canonical_model_sha256(base_tree),
            "effective_snapshot_id": current.effective_snapshot_id,
            "effective_tree_sha256": current.effective_tree_sha256,
            "revision_chain_sha256": current.revision_chain_sha256,
        }
    )


def _payload_matches_current(
    payload: dict[str, Any],
    source_ir: Any,
    base_tree: Any,
    current: Any,
) -> bool:
    return bool(
        payload.get("source_sha256") == source_ir.source_sha256
        and payload.get("facts_sha256") == _facts_sha256(source_ir, base_tree, current)
        and payload.get("source_ir_id") == source_ir.source_ir_id
        and payload.get("source_ir_canonical_sha256")
        == _source_ir_canonical_sha256(source_ir)
        and payload.get("base_structure_snapshot_id") == base_tree.structure_snapshot_id
        and payload.get("base_structure_canonical_sha256")
        == canonical_model_sha256(base_tree)
        and payload.get("expected_revision_number") == current.revision_number
        and payload.get("expected_effective_snapshot_id")
        == current.effective_snapshot_id
        and payload.get("expected_effective_tree_sha256")
        == current.effective_tree_sha256
    )


def _job_projection(job: DurableJob) -> dict[str, Any]:
    return {
        "schema_version": "agent-structure-proposal-job-public/v1",
        "job_id": job.job_id,
        "status": job.status.value,
        "generation": job.generation,
        "created_at_utc": job.created_at.isoformat(),
        "updated_at_utc": job.updated_at.isoformat(),
        "progress": dict(job.progress or {}),
        "result": dict(job.result or {}) if job.result is not None else None,
        "error_code": job.error_code,
    }


def serialize_agent_structure_proposal(proposal: Any) -> dict[str, Any]:
    return {
        "schema_version": "agent-structure-proposal-public/v1",
        "proposal_record_id": proposal.proposal_record_id,
        "proposal_revision": proposal.proposal_revision,
        "status": proposal.status.value,
        "source_sha256": proposal.source_sha256,
        "facts_sha256": proposal.facts_sha256,
        "proposal_payload_sha256": proposal.proposal_payload_sha256,
        "created_at_utc": proposal.created_at_utc.isoformat(),
        "expires_at_utc": proposal.expires_at_utc.isoformat(),
        "operation_count": len(proposal.structure_patch_proposal.operations),
        "operations": [
            operation.model_dump(mode="json")
            for operation in proposal.structure_patch_proposal.operations
        ],
        "base": {
            "source_ir_id": proposal.structure_patch_proposal.source_ir_id,
            "base_structure_snapshot_id": (
                proposal.structure_patch_proposal.base_structure_snapshot_id
            ),
            "expected_revision_number": (
                proposal.structure_patch_proposal.expected_revision_number
            ),
            "expected_effective_snapshot_id": (
                proposal.structure_patch_proposal.expected_effective_snapshot_id
            ),
        },
    }


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_ir_canonical_sha256(source_ir: Any) -> str:
    return _canonical_sha256(
        source_ir.model_dump(mode="json", exclude={"created_at"})
    )


class _DurableAgentStructureSteering(AgentRuntimeSteeringPort):
    def __init__(
        self,
        repository: AgentStructureSteerRepository,
        *,
        job_id: str,
        job_generation: int,
    ) -> None:
        self._repository = repository
        self._job_id = job_id
        self._job_generation = job_generation

    def next_command(self) -> AgentStructureSteerV1 | None:
        command = self._repository.claim_next(
            job_id=self._job_id,
            expected_job_generation=self._job_generation,
        )
        return _runtime_steer(command) if command is not None else None

    def mark_delivered(
        self,
        command: AgentStructureSteerV1,
        *,
        runtime_thread_id: str,
        runtime_turn_id: str,
    ) -> None:
        self._repository.mark_delivered(
            steer_command_id=command.steer_command_id,
            expected_payload_sha256=command.payload_sha256,
            runtime_thread_id=runtime_thread_id,
            runtime_turn_id=runtime_turn_id,
        )

    def mark_failed(
        self,
        command: AgentStructureSteerV1,
        *,
        reason_code: str,
    ) -> None:
        self._repository.mark_rejected(
            steer_command_id=command.steer_command_id,
            expected_payload_sha256=command.payload_sha256,
            reason_code=reason_code[:120],
        )


def _runtime_steer(command: AgentStructureSteerCommand) -> AgentStructureSteerV1:
    return AgentStructureSteerV1(
        steer_command_id=command.steer_command_id,
        payload_sha256=command.payload_sha256,
        intent=command.intent,
        focus_structure_node_ids=command.focus_structure_node_ids,
        suggested_operations=command.suggested_operations,
        suggested_operations_authority=command.suggested_operations_authority,
    )


__all__ = [
    "AGENT_STRUCTURE_PROPOSAL_JOB_KIND",
    "AGENT_STRUCTURE_PROPOSAL_JOB_SCHEMA_VERSION",
    "get_agent_structure_proposal_status",
    "run_durable_agent_structure_proposal",
    "serialize_agent_structure_proposal",
    "start_agent_structure_proposal",
]
