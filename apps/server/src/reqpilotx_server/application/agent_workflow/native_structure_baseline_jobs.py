"""Durable acceptance of the verified native structure as canonical D3 input."""

from __future__ import annotations

from reqpilotx_server.resource_paths import development_path

from datetime import UTC, datetime
from io import BytesIO
import hashlib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from reqpilotx_server.adapters.agents.document_evidence_host import prepare_document_evidence
from reqpilotx_server.adapters.agents.portable_kit import load_portable_agent_kit
from reqpilotx_server.application.agent_workflow.native_structure_baseline import (
    NativeStructureAcceptedBaselineV1,
    NativeStructureBaselineError,
    build_native_structure_baseline_artifact,
    compile_native_structure_accepted_baseline,
    native_structure_baseline_artifact_bytes,
    native_structure_baseline_object_key,
    native_structure_projection_sha256,
)
from reqpilotx_server.application.agent_workflow.native_structure_bridge import (
    _canonical_sha256,
    _document_source_sha256,
    _load_latest_reference,
    _load_current_reference,
    _no_store_headers,
    _read_all_handoff_attempts,
    _read_feedbacks,
    _read_handoff_events,
    _read_handoffs,
    _read_proposal_decisions,
    _require_project_document,
    _serialize_public_reference,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.source_runtime import resolve_source_record_path
from reqpilotx_server.application.transport import ResponseSink
from reqpilotx_server.ports.job_queue import (
    DurableJob,
    JobExecutionContext,
    JobStatus,
    NonRetryableJobError,
)


NATIVE_STRUCTURE_BASELINE_JOB_KIND = "agent.native-baseline.accept"
NATIVE_STRUCTURE_BASELINE_JOB_SCHEMA_VERSION = "native-structure-baseline-job/v1"
NATIVE_STRUCTURE_BASELINE_RESULT_SCHEMA_VERSION = (
    "native-structure-baseline-job-result/v1"
)

_REPOSITORY_ROOT = development_path()
_AGENT_KIT_ROOT = _REPOSITORY_ROOT / "agent-kit"


class NativeStructureBaselineStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["native-structure-baseline-start-request/v1"] = (
        "native-structure-baseline-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def start_native_structure_baseline_acceptance(
    project_id: str,
    document_id: str,
    payload: NativeStructureBaselineStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.jobs is None:
        raise _runtime_unavailable()
    context = _current_reference_context(project_id, document_id)
    if (
        payload.expected_source_sha256 != context["source_sha256"]
        or payload.expected_artifact_id != context["artifact_id"]
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "native_structure_baseline_reference_changed",
                "message": "The native structure changed before baseline acceptance.",
            },
        )
    kit = load_portable_agent_kit(_AGENT_KIT_ROOT)
    projection_sha256 = native_structure_projection_sha256(
        public_reference=context["public_reference"],
        table_previews=context["table_previews"],
    )
    jobs = _durable_jobs()
    latest_succeeded = jobs.get_latest_succeeded(
        kind=NATIVE_STRUCTURE_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    if _job_matches_current_baseline(
        latest_succeeded,
        source_sha256=context["source_sha256"],
        artifact_id=context["artifact_id"],
        native_projection_sha256=projection_sha256,
        kit_bundle_sha256=kit.identity.bundle_sha256,
    ):
        return _job_projection(latest_succeeded)
    job_payload = {
        "schema_version": NATIVE_STRUCTURE_BASELINE_JOB_SCHEMA_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": context["source_sha256"],
        "native_artifact_id": context["artifact_id"],
        "native_projection_sha256": projection_sha256,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "command_id": payload.command_id,
    }
    job = jobs.enqueue(
        kind=NATIVE_STRUCTURE_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=_canonical_sha256(job_payload),
        payload=job_payload,
        priority=24,
        max_attempts=2,
    )
    return _job_projection(job)


def get_native_structure_baseline_status(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _durable_jobs

    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    if RUNTIME_STATE.container is None or RUNTIME_STATE.container.jobs is None:
        return {
            "schema_version": "native-structure-baseline-status/v1",
            "status": "UNAVAILABLE",
            "baseline_ready": False,
            "job": None,
            "message": "Durable native baseline processing is unavailable.",
        }
    job = _durable_jobs().get_latest(
        kind=NATIVE_STRUCTURE_BASELINE_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
    )
    return {
        "schema_version": "native-structure-baseline-status/v1",
        "status": "IDLE" if job is None else job.status.value,
        "baseline_ready": bool(job and job.status is JobStatus.SUCCEEDED),
        "job": None if job is None else _job_projection(job),
        "message": _status_message(job),
    }


def run_durable_native_structure_baseline_acceptance(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    payload = dict(durable_job.payload or {})
    if payload.get("schema_version") != NATIVE_STRUCTURE_BASELINE_JOB_SCHEMA_VERSION:
        raise NonRetryableJobError(
            error_code="native_structure_baseline_job_schema_invalid",
            message="Native structure baseline job schema is invalid.",
        )
    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Native baseline worker runtime is not initialized.")
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    current = _current_reference_context(project_id, document_id)
    kit = load_portable_agent_kit(_AGENT_KIT_ROOT)
    observed_projection_sha256 = native_structure_projection_sha256(
        public_reference=current["public_reference"],
        table_previews=current["table_previews"],
    )
    if (
        current["source_sha256"] != payload.get("source_sha256")
        or current["artifact_id"] != payload.get("native_artifact_id")
        or observed_projection_sha256 != payload.get("native_projection_sha256")
        or kit.identity.bundle_sha256 != payload.get("kit_bundle_sha256")
    ):
        raise NonRetryableJobError(
            error_code="native_structure_baseline_identity_changed",
            message="Native structure, source, or Agent Kit changed before acceptance.",
        )
    if context.cancellation_requested():
        raise NonRetryableJobError(
            error_code="native_structure_baseline_cancelled",
            message="Native structure baseline acceptance was cancelled.",
        )
    document = current["document"]
    source_path = resolve_source_record_path(document).resolve(strict=True)
    run_digest = _canonical_sha256(
        {
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": current["source_sha256"],
            "native_projection_sha256": observed_projection_sha256,
            "kit_bundle_sha256": kit.identity.bundle_sha256,
        }
    )
    context.heartbeat(
        progress={
            "stage": "REBUILDING_SOURCE_EVIDENCE",
            "message": "正在重建原文坐标、表格单元格和图片证据。",
        }
    )
    try:
        prepared = prepare_document_evidence(
            source_path,
            expected_source_sha256=current["source_sha256"],
            document_role="CUSTOMER_SOR",
            kit_identity=kit.identity,
            stage_context_sha256=observed_projection_sha256,
            run_id=f"agent-run-{run_digest[:32]}",
            opaque_source_handle=f"source-handle-{run_digest[32:64]}",
            clock=lambda: datetime.now(UTC),
        )
        context.heartbeat(
            progress={
                "stage": "VALIDATING_NATIVE_BASELINE",
                "message": "正在验证标题树、原文范围和表格区域的 exact-once 覆盖。",
            }
        )
        compilation = compile_native_structure_accepted_baseline(
            project_id=project_id,
            document_id=document_id,
            source_ir=prepared.source_ir,
            public_reference=current["public_reference"],
            table_previews=current["table_previews"],
            kit_identity=kit.identity,
        )
    except NativeStructureBaselineError as exc:
        raise NonRetryableJobError(
            error_code=str(exc),
            message="Native structure could not be accepted as a complete D3 input.",
        ) from exc
    except ValueError as exc:
        raise NonRetryableJobError(
            error_code="native_structure_baseline_validation_failed",
            message="Native structure baseline Host validation failed.",
        ) from exc
    identity = compilation.identity
    if (
        identity.native_projection_sha256 != observed_projection_sha256
        or identity.source_sha256 != current["source_sha256"]
        or identity.native_artifact_id != current["artifact_id"]
        or identity.kit_identity != kit.identity
    ):
        raise NonRetryableJobError(
            error_code="native_structure_baseline_compilation_drift",
            message="Compiled native structure baseline drifted from its enqueue pin.",
        )
    artifact = build_native_structure_baseline_artifact(compilation)
    encoded = native_structure_baseline_artifact_bytes(artifact)
    object_sha256 = hashlib.sha256(encoded).hexdigest()
    object_key = native_structure_baseline_object_key(identity)
    context.heartbeat(
        progress={
            "stage": "PERSISTING_ACCEPTED_BASELINE",
            "message": "正在保存可恢复的 accepted baseline 身份。",
        }
    )
    context.begin_publication()
    stored = container.object_store.put(
        key=object_key,
        source=BytesIO(encoded),
        expected_sha256=object_sha256,
        media_type="application/vnd.reqpilotx.native-baseline+json",
    )
    from reqpilotx_server.application.agent_workflow.native_parent_provision_jobs import (
        enqueue_native_parent_provision_recognition,
    )

    parent_job = enqueue_native_parent_provision_recognition(
        project_id=project_id,
        document_id=document_id,
        baseline=identity,
        baseline_object_key=stored.key,
        baseline_object_sha256=stored.sha256,
        baseline_artifact_sha256=artifact.artifact_sha256,
    )
    return {
        "schema_version": NATIVE_STRUCTURE_BASELINE_RESULT_SCHEMA_VERSION,
        "status": "ACCEPTED",
        "source_sha256": identity.source_sha256,
        "native_artifact_id": identity.native_artifact_id,
        "native_projection_sha256": identity.native_projection_sha256,
        "kit_bundle_sha256": identity.kit_identity.bundle_sha256,
        "baseline": identity.model_dump(mode="json"),
        "private_artifact": {
            "object_key": stored.key,
            "object_sha256": stored.sha256,
            "artifact_sha256": artifact.artifact_sha256,
            "size": stored.size,
            "media_type": stored.media_type,
        },
        "parent_job": parent_job,
        "next_action": "WAIT_FOR_PARENT_PROVISION_RECOGNITION",
    }


def _current_reference_context(
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    document = _require_project_document(project_id, document_id)
    source_sha256 = _document_source_sha256(document)
    bundle = _load_current_reference(
        project_id, document_id, document=document, source_sha256=source_sha256,
    )
    artifact_id = str(bundle["artifact"]["artifact_id"])
    attempts = _read_all_handoff_attempts(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_id=artifact_id,
    )
    public_reference = _serialize_public_reference(
        bundle,
        feedbacks=_read_feedbacks(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        proposal_decisions=_read_proposal_decisions(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoffs=_read_handoffs(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoff_events=_read_handoff_events(
            source_sha256=source_sha256,
            artifact_id=artifact_id,
        ),
        handoff_attempts=attempts,
    )
    evidence = bundle.get("document_tree_evidence")
    table_previews = (
        tuple(evidence.get("table_previews", ())) if isinstance(evidence, dict) else ()
    )
    return {
        "document": document,
        "source_sha256": source_sha256,
        "artifact_id": artifact_id,
        "public_reference": public_reference,
        "table_previews": table_previews,
        "bundle": bundle,
        "handoff_attempts": attempts,
    }


def _job_matches_current_baseline(
    job: DurableJob | None,
    *,
    source_sha256: str,
    artifact_id: str,
    native_projection_sha256: str,
    kit_bundle_sha256: str,
) -> bool:
    if job is None or job.status is not JobStatus.SUCCEEDED:
        return False
    result = dict(job.result or {})
    return (
        result.get("schema_version") == NATIVE_STRUCTURE_BASELINE_RESULT_SCHEMA_VERSION
        and result.get("status") == "ACCEPTED"
        and result.get("source_sha256") == source_sha256
        and result.get("native_artifact_id") == artifact_id
        and result.get("native_projection_sha256") == native_projection_sha256
        and result.get("kit_bundle_sha256") == kit_bundle_sha256
    )


def _job_projection(job: DurableJob) -> dict[str, Any]:
    result = dict(job.result or {})
    baseline = result.get("baseline")
    validated_baseline = None
    if isinstance(baseline, dict):
        validated_baseline = NativeStructureAcceptedBaselineV1.model_validate(
            baseline
        ).model_dump(mode="json")
    return {
        "schema_version": "native-structure-baseline-job-public/v1",
        "job_id": job.job_id,
        "status": job.status.value,
        "generation": job.generation,
        "progress": dict(job.progress or {}),
        "error_code": job.error_code,
        "baseline": validated_baseline,
        "next_action": str(result.get("next_action") or ""),
    }


def _status_message(job: DurableJob | None) -> str:
    if job is None:
        return "原文结构已可继续；accepted baseline 尚未生成。"
    if job.status is JobStatus.SUCCEEDED:
        return "原生结构已成为下一步可恢复、可追溯的 accepted baseline。"
    if job.status in {JobStatus.PENDING, JobStatus.RUNNING}:
        return str(job.progress.get("message") or "正在把原生结构验证为下一步输入。")
    if job.status is JobStatus.FAILED:
        return "accepted baseline 验证失败；标题树仍可继续浏览和修订。"
    if job.status is JobStatus.CANCELLED:
        return "accepted baseline 处理已取消；标题树未受影响。"
    return "accepted baseline 正在处理。"


def _runtime_unavailable() -> ApplicationError:
    return ApplicationError(
        status_code=503,
        detail={
            "code": "native_structure_baseline_runtime_unavailable",
            "message": "Durable native baseline processing is unavailable.",
        },
    )


__all__ = [
    "NATIVE_STRUCTURE_BASELINE_JOB_KIND",
    "NativeStructureBaselineStartRequestV1",
    "get_native_structure_baseline_status",
    "run_durable_native_structure_baseline_acceptance",
    "start_native_structure_baseline_acceptance",
]
