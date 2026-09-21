from __future__ import annotations

from typing import Any

from app.application.agent_workflow.native_structure_bridge import (
    NativeStructureHandoffAckRequestV1,
    _canonical_sha256,
    _document_source_sha256,
    _latest_handoff_events,
    _read_all_handoff_attempts,
    _read_handoff_events,
    _read_handoffs,
    _require_project_document,
    acknowledge_native_structure_handoff,
    record_native_structure_handoff_outcome,
)
from reqpilotx.ports.agent_structure_handoff_executor import (
    CanonicalStructureHandoffBindingV1,
)
from app.application.source_runtime import resolve_source_record_path
from app.ports.agent_structure_handoff_executor import AgentStructureHandoffTaskV1
from app.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    NonRetryableJobError,
)


AGENT_STRUCTURE_HANDOFF_JOB_KIND = "agent.structure.handoff"
AGENT_STRUCTURE_HANDOFF_JOB_SCHEMA_VERSION = "agent-structure-handoff-job/v1"


def enqueue_native_structure_handoff_job(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff: dict[str, Any],
) -> dict[str, Any] | None:
    from app.application.runtime import (
        RUNTIME_STATE,
        _agent_customer_data_enabled,
        _durable_jobs,
    )

    container = RUNTIME_STATE.container
    if (
        container is None
        or container.agent_structure_handoff_executor is None
        or not _agent_customer_data_enabled()
    ):
        return None
    executor = container.agent_structure_handoff_executor
    binding = _build_binding(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
        handoff=handoff,
    )
    payload = {
        "schema_version": AGENT_STRUCTURE_HANDOFF_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "handoff_id": str(handoff["handoff_id"]),
        "input_fingerprint": str(handoff["input_fingerprint"]),
        "binding": binding.model_dump(mode="json"),
        "executor_id": executor.adapter_id,
        "executor_version": executor.adapter_version,
        "deadline_seconds": 300,
    }
    job = _durable_jobs().enqueue(
        kind=AGENT_STRUCTURE_HANDOFF_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(payload),
        payload=payload,
        priority=25,
        max_attempts=2,
    )
    return _job_projection(job)


def run_durable_native_structure_handoff(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from app.application.runtime import _agent_structure_handoff_executor

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != AGENT_STRUCTURE_HANDOFF_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_job_schema_invalid",
            message="Agent Structure handoff job schema is invalid.",
        )
    executor = _agent_structure_handoff_executor()
    if (
        payload.get("executor_id") != executor.adapter_id
        or payload.get("executor_version") != executor.adapter_version
    ):
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_executor_identity_changed",
            message="Agent Structure handoff executor identity changed after enqueue.",
        )
    try:
        binding = CanonicalStructureHandoffBindingV1.model_validate(
            payload.get("binding")
        )
    except Exception as exc:
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_binding_invalid",
            message="Agent Structure handoff binding is invalid.",
        ) from exc
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    if source_sha256 != binding.source_sha256:
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_source_changed",
            message="Document source changed before Agent handoff execution.",
        )
    handoff = next(
        (
            item
            for item in _read_handoffs(
                source_sha256=source_sha256,
                artifact_id=binding.artifact_id,
            )
            if str(item.get("handoff_id") or "") == binding.handoff_id
        ),
        None,
    )
    if (
        handoff is None
        or str(handoff.get("input_fingerprint") or "") != binding.input_fingerprint
        or tuple(sorted(handoff.get("affected_node_ids", [])))
        != binding.affected_scope_ids
    ):
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_changed",
            message="Agent Structure handoff changed before execution.",
        )
    latest_event = _latest_handoff_events(
        _read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=binding.artifact_id,
        )
    ).get(binding.handoff_id)
    if latest_event is not None and str(latest_event.get("status") or "") in {
        "NO_CHANGE",
        "NEEDS_GUIDANCE",
        "UNRESOLVED_CONTINUED",
    }:
        visible_status = str(latest_event.get("status") or "")
        recovered_status = str(latest_event.get("execution_status") or "")
        if recovered_status != (
            "NO_CHANGE" if visible_status == "NO_CHANGE" else "UNRESOLVED"
        ):
            raise NonRetryableJobError(
                error_code="agent_structure_handoff_outcome_invalid",
                message="Stored Agent Structure handoff outcome is invalid.",
            )
        return {
            "schema_version": "agent-structure-handoff-job-result/v1",
            "status": recovered_status,
            "reason_code": str(
                latest_event.get("reason_code") or "durable_outcome_recovered"
            ),
            "evidence_state": str(
                latest_event.get("evidence_state") or "INSUFFICIENT"
            ),
            "decision_policy": str(
                latest_event.get("decision_policy") or "KEEP_CURRENT_STRUCTURE"
            ),
            "attempt_id": "",
            "attempt_status": "",
            "runtime_thread_id_sha256": "",
            "runtime_turn_id_sha256": "",
            "tool_activity_sha256": _canonical_sha256({}),
        }
    existing_attempts = [
        item
        for item in _read_all_handoff_attempts(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=binding.artifact_id,
        )
        if str(item.get("handoff_id") or "") == binding.handoff_id
    ]
    if existing_attempts:
        latest = existing_attempts[-1]
        validation_report = latest.get("validation_report")
        validation_report = (
            validation_report if isinstance(validation_report, dict) else {}
        )
        working_decision = validation_report.get("working_decision")
        working_decision = (
            working_decision if isinstance(working_decision, dict) else {}
        )
        return {
            "schema_version": "agent-structure-handoff-job-result/v1",
            "status": "PUBLISHED",
            "reason_code": "durable_attempt_recovered",
            "evidence_state": str(
                working_decision.get("evidence_state") or "SUFFICIENT"
            ),
            "decision_policy": str(
                working_decision.get("decision_policy")
                or "FIRST_HOST_VALIDATED_PROPOSAL"
            ),
            "attempt_id": str(latest.get("attempt_id") or ""),
            "attempt_status": str(latest.get("status") or ""),
            "runtime_thread_id_sha256": "",
            "runtime_turn_id_sha256": "",
            "tool_activity_sha256": _canonical_sha256({}),
        }
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_cancelled",
            message="Agent Structure handoff was cancelled before execution.",
        )
    acknowledge_native_structure_handoff(
        project_id,
        document_id,
        NativeStructureHandoffAckRequestV1(
            command_id=f"job-ack:{durable_job.job_id}:{durable_job.generation}",
            expected_source_sha256=source_sha256,
            expected_artifact_id=binding.artifact_id,
            handoff_id=binding.handoff_id,
            current_action="Canonical Agent 正在读取本轮范围与证据。",
        ),
        _ResponseSink(),
    )
    source_path = resolve_source_record_path(document).resolve(strict=True)
    task = AgentStructureHandoffTaskV1(
        project_id=project_id,
        document_id=document_id,
        source_path=source_path,
        authorized_corpus_root=source_path.parent,
        document_role="CUSTOMER_SOR",
        binding=binding,
        deadline_seconds=int(payload.get("deadline_seconds") or 300),
    )
    execution = executor.execute(
        task,
        progress=lambda stage: context.heartbeat(progress={"stage": stage}),
        cancellation_requested=context.cancellation_requested,
        begin_publication=context.begin_publication,
    )
    expected_policy = {
        "PUBLISHED": "FIRST_HOST_VALIDATED_PROPOSAL",
        "NO_CHANGE": "KEEP_CURRENT_STRUCTURE",
    }.get(execution.status)
    allowed_reasons = {
        "PUBLISHED": {"revision_published"},
        "NO_CHANGE": {
            "source_already_satisfies_handoff",
            "no_grounded_change",
            "current_structure_selected",
        },
    }.get(execution.status, set())
    if (
        expected_policy is None
        or execution.reason_code not in allowed_reasons
        or execution.evidence_state
        not in {"SUFFICIENT", "CONFLICTING", "INSUFFICIENT"}
        or execution.decision_policy != expected_policy
    ):
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_outcome_invalid",
            message="Agent Structure handoff returned an invalid working decision.",
        )
    if execution.status == "PUBLISHED":
        publication = dict(execution.publication or {})
        if publication.get("status") != "VERIFICATION_REQUIRED":
            raise NonRetryableJobError(
                error_code="agent_structure_handoff_publication_invalid",
                message="Agent Structure handoff publication is invalid.",
            )
        attempt_id = str(publication.get("attempt_id") or "")
        attempt_status = str(publication.get("status") or "")
    elif execution.status == "NO_CHANGE":
        record_native_structure_handoff_outcome(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_id=binding.artifact_id,
            handoff_id=binding.handoff_id,
            execution_status=execution.status,
            reason_code=execution.reason_code,
            evidence_state=execution.evidence_state,
            decision_policy=execution.decision_policy,
            public_summary=execution.public_summary,
        )
        attempt_id = ""
        attempt_status = ""
    else:
        raise NonRetryableJobError(
            error_code="agent_structure_handoff_outcome_invalid",
            message="Agent Structure handoff returned an invalid outcome.",
        )
    return {
        "schema_version": "agent-structure-handoff-job-result/v1",
        "status": execution.status,
        "reason_code": execution.reason_code,
        "evidence_state": execution.evidence_state,
        "decision_policy": execution.decision_policy,
        "attempt_id": attempt_id,
        "attempt_status": attempt_status,
        "runtime_thread_id_sha256": execution.runtime_thread_id_sha256,
        "runtime_turn_id_sha256": execution.runtime_turn_id_sha256,
        "tool_activity_sha256": _canonical_sha256(execution.tool_activity_summary),
    }


def _build_binding(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    artifact_id: str,
    handoff: dict[str, Any],
) -> CanonicalStructureHandoffBindingV1:
    affected = tuple(sorted(str(item) for item in handoff["affected_node_ids"]))
    run_digest = _canonical_sha256(
        {
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "artifact_id": artifact_id,
            "handoff_id": handoff["handoff_id"],
            "input_fingerprint": handoff["input_fingerprint"],
        }
    )
    values = {
        "run_id": f"agent-run-{run_digest[:32]}",
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "handoff_id": str(handoff["handoff_id"]),
        "input_fingerprint": str(handoff["input_fingerprint"]),
        "status": "ACKNOWLEDGED",
        "affected_scope_ids": affected,
        "work_item_count": len(handoff.get("feedback_ids", []))
        + len(handoff.get("proposal_decision_ids", [])),
    }
    return CanonicalStructureHandoffBindingV1(
        **values,
        binding_sha256=_canonical_sha256(
            {
                "schema_version": "canonical-structure-handoff-binding/v1",
                "content_free": True,
                **values,
            }
        ),
    )


def _job_projection(job: DurableJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "generation": job.generation,
        "progress": dict(job.progress or {}),
        "error_code": job.error_code,
    }


class _ResponseSink:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.status_code = 200


__all__ = [
    "AGENT_STRUCTURE_HANDOFF_JOB_KIND",
    "AGENT_STRUCTURE_HANDOFF_JOB_SCHEMA_VERSION",
    "enqueue_native_structure_handoff_job",
    "run_durable_native_structure_handoff",
]
