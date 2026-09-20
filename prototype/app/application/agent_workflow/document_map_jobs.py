"""Durable first-document entry point for the existing Codex Document Map drivers.

The queue stores operational state; source evidence and driver checkpoints use
the existing ObjectStore.  This module does not classify or rewrite source items.
"""

from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Mapping
from uuid import uuid4
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field

from app.application.agent_workflow.native_structure_bridge import (
    _document_source_sha256,
    _no_store_headers,
    _require_project_document,
)
from app.application.errors import ApplicationError
from app.application.source_runtime import resolve_source_record_path
from app.application.transport import ResponseSink
from app.ports.job_queue import DurableJob, JobExecutionContext, NonRetryableJobError

DOCUMENT_MAP_JOB_KIND = "agent.document-map"
ROOT = Path(__file__).resolve().parents[4]


class DocumentMapStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["document-map-start-request/v1"] = (
        "document-map-start-request/v1"
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    resume_job_id: str | None = Field(default=None, pattern=r"^job-[0-9a-f]{32}$")
    timeout_seconds: int = Field(default=1200, ge=60, le=7200)
    auto_source_items: bool = False


class SourceItemDocumentStartRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(min_length=1, max_length=200)


def start_document_source_items(
    project_id: str,
    document_id: str,
    payload: SourceItemDocumentStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from app.application.agent_workflow.native_structure_bridge import (
        get_native_structure_reference,
    )
    from app.application.agent_workflow.source_requirement_reference_bridge import (
        _load_effective_reference,
    )
    from app.application.agent_workflow.source_requirement_jobs import (
        SourceItemTextBaselineStartRequestV1,
        SourceRequirementBaselineStartRequestV1,
        start_source_item_text_baseline,
        start_source_requirement_baseline,
    )
    from app.application.agent_workflow.source_item_image_jobs import SourceItemImagesStartRequestV1, start_source_item_images

    reference = get_native_structure_reference(project_id, document_id, response)
    if (
        reference["source_sha256"] != payload.expected_source_sha256
        or reference["artifact_id"] != payload.expected_artifact_id
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "document_source_items_map_changed",
                "message": "The source or Document Map changed before extraction.",
            },
        )
    bundle = _load_effective_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=payload.expected_source_sha256,
    )
    document_map = bundle["document_map"]
    covered = set(bundle["baseline"].covered_scope_ids)
    nodes = [
        node
        for node in document_map.nodes
        if any(
            content.text.strip()
            for content in node.source_content
        )
    ]
    tables = list(document_map.logical_tables)
    image_nodes = [node for node in document_map.nodes if any(anchor.anchor_kind in {"IMAGE", "FIGURE"} for anchor in node.source_anchors)]
    expected = [
        *(f"text:{node.node_id}" for node in nodes),
        *(table.scope_id for table in tables),
        *(f"image:{node.node_id}" for node in image_nodes),
    ]
    missing_nodes = [
        node.node_id for node in nodes if f"text:{node.node_id}" not in covered
    ]
    jobs = []
    for index in range(0, len(missing_nodes), 32):
        jobs.append(
            start_source_item_text_baseline(
                project_id,
                document_id,
                SourceItemTextBaselineStartRequestV1(
                    command_id=f"{payload.command_id}:text:{index // 32 + 1}",
                    expected_source_sha256=payload.expected_source_sha256,
                    expected_artifact_id=payload.expected_artifact_id,
                    node_ids=tuple(missing_nodes[index : index + 32]),
                ),
                response,
            )
        )
    for table in tables:
        if table.scope_id not in covered:
            jobs.append(
                start_source_requirement_baseline(
                    project_id,
                    document_id,
                    SourceRequirementBaselineStartRequestV1(
                        command_id=f"{payload.command_id}:table:{_digest(table.scope_id)[:16]}",
                        expected_source_sha256=payload.expected_source_sha256,
                        expected_artifact_id=payload.expected_artifact_id,
                        logical_table_scope_id=table.scope_id,
                    ),
                    response,
                )
            )
    missing_images = [node.node_id for node in image_nodes if f"image:{node.node_id}" not in covered]
    for index in range(0, len(missing_images), 32):
        jobs.append(start_source_item_images(project_id, document_id,
            SourceItemImagesStartRequestV1(command_id=f"{payload.command_id}:image:{index // 32 + 1}",
                expected_source_sha256=payload.expected_source_sha256, expected_artifact_id=payload.expected_artifact_id,
                node_ids=tuple(missing_images[index:index + 32])), response))
    return {
        "schema_version": "document-source-items-start/v1",
        "status": "QUEUED" if jobs else "CURRENT_SCOPES_PROCESSED",
        "source_sha256": payload.expected_source_sha256,
        "document_map_artifact_id": document_map.artifact_id,
        "expected_scope_ids": expected,
        "already_covered_scope_ids": sorted(set(expected) & covered),
        "jobs": jobs,
    }


def _container():
    from app.application.runtime import RUNTIME_STATE

    value = RUNTIME_STATE.container
    if (
        value is None
        or value.jobs is None
        or value.agent_structure_loop_repository is None
    ):
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "document_map_runtime_unavailable",
                "message": "Document Map requires the durable PostgreSQL runtime.",
            },
        )
    return value


def _projection(job: DurableJob | None) -> dict[str, Any]:
    if job is None:
        return {"schema_version": "document-map-job-status/v1", "status": "NOT_STARTED"}
    return {
        "schema_version": "document-map-job-status/v1"
        if job.kind == DOCUMENT_MAP_JOB_KIND
        else "agent-workflow-job-status/v1",
        "kind": job.kind,
        "job_id": job.job_id,
        "status": job.status.value,
        "attempts": job.attempts,
        "source_sha256": job.payload.get("source_sha256"),
        "run_id": job.payload.get("run_id"),
        "progress": job.progress,
        "result": job.result,
        "error_code": job.error_code,
        "error_message": job.error_message,
    }


def start_document_map(
    project_id: str,
    document_id: str,
    payload: DocumentMapStartRequestV1,
    response: ResponseSink,
) -> dict[str, Any]:
    from app.adapters.agents.codex_app_server.lifecycle import (
        load_protocol_lock,
        verify_protocol_binary_identity,
    )
    from app.adapters.agents.portable_kit import load_portable_agent_kit

    response.headers.update(_no_store_headers())
    document = _require_project_document(project_id, document_id)
    source_sha = _document_source_sha256(document)
    if source_sha != payload.expected_source_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "document_map_source_changed",
                "message": "The uploaded source changed before start.",
            },
        )
    container = _container()
    settings = container.settings
    if settings.codex_bin is None or settings.codex_home is None:
        raise ApplicationError(
            status_code=503,
            detail={
                "code": "document_map_codex_not_configured",
                "message": "Configure the Codex App Server runtime before starting.",
            },
        )
    verify_protocol_binary_identity(settings.codex_bin, lock=load_protocol_lock())
    source_path = resolve_source_record_path(document).resolve(strict=True)
    if source_path.suffix.lower() != ".pdf":
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "document_map_pdf_required",
                "message": "The current App Server visual Document Map adapter requires PDF input.",
            },
        )
    kit = load_portable_agent_kit(ROOT / "agent-kit")
    resume = (
        container.jobs.get(payload.resume_job_id) if payload.resume_job_id else None
    )
    if payload.resume_job_id and (
        resume is None
        or resume.kind != DOCUMENT_MAP_JOB_KIND
        or resume.aggregate_id != document_id
        or resume.payload.get("project_id") != project_id
        or resume.payload.get("source_sha256") != source_sha
        or resume.payload.get("kit_bundle_sha256") != kit.identity.bundle_sha256
        or resume.status.value not in {"FAILED", "CANCELLED", "SUCCEEDED"}
    ):
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "document_map_resume_identity_mismatch",
                "message": "Resume must bind a terminal job for the same source and Agent Kit.",
            },
        )
    values = {
        "schema_version": "document-map-job/v1",
        "application_entrypoint": "source-item-workbench-v1",
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha,
        "command_id": payload.command_id,
        "timeout_seconds": payload.timeout_seconds,
        "auto_source_items": payload.auto_source_items,
        "kit_bundle_sha256": kit.identity.bundle_sha256,
        "run_id": resume.payload["run_id"] if resume else "agent-run-" + uuid4().hex,
        "resume_checkpoint": (resume.progress.get("checkpoint") if resume else None),
    }
    # command_id is the request identity, while the generated run_id is not part
    # of idempotency. Repeating start cannot enqueue a second model run.
    idempotency = _digest(
        {
            "project_id": project_id,
            "document_id": document_id,
            **payload.model_dump(mode="json"),
        }
    )
    existing = container.jobs.get_by_idempotency(
        kind=DOCUMENT_MAP_JOB_KIND, idempotency_key=idempotency
    )
    if existing is not None:
        return _projection(existing)
    job = container.jobs.enqueue(
        kind=DOCUMENT_MAP_JOB_KIND,
        aggregate_type="sor-document",
        aggregate_id=document_id,
        idempotency_key=idempotency,
        payload=values,
        priority=25,
        max_attempts=3,
    )
    return _projection(job)


def get_document_map_status(
    project_id: str, document_id: str, response: ResponseSink
) -> dict[str, Any]:
    response.headers.update(_no_store_headers())
    _require_project_document(project_id, document_id)
    return _projection(
        _container().jobs.get_latest(
            kind=DOCUMENT_MAP_JOB_KIND,
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _save_checkpoint(
    container: Any,
    job: DurableJob,
    context: JobExecutionContext,
    value: Mapping[str, Any],
    stage: str,
) -> dict[str, Any]:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    digest = hashlib.sha256(raw).hexdigest()
    key = f"artifacts/agent-document-map/{job.payload['project_id']}/{job.aggregate_id}/{job.payload['run_id']}/{digest}.json"
    stored = container.object_store.put(
        key=key,
        source=BytesIO(raw),
        expected_sha256=digest,
        media_type="application/json",
    )
    pointer = {"object_key": stored.key, "sha256": stored.sha256, "size": stored.size}
    context.heartbeat(
        progress={
            "stage": stage,
            "checkpoint": pointer,
            "completed_scope_count": sum(
                "replacement" in item for item in value.get("scopes", {}).values()
            ),
            "verified_heading_scope_count": sum(
                "headings" in item for item in value.get("scopes", {}).values()
            ),
            "message": "正在核对原文结构；已完成的范围可从持久化检查点恢复。",
        }
    )
    return pointer


def run_durable_document_map(
    job: DurableJob, context: JobExecutionContext
) -> dict[str, Any]:
    from app.adapters.agents.codex_app_server.document_map_executor import (
        execute_document_map,
    )

    container = _container()
    document = _require_project_document(job.payload["project_id"], job.aggregate_id)
    if _document_source_sha256(document) != job.payload["source_sha256"]:
        raise NonRetryableJobError(
            error_code="document_map_source_changed",
            message="The source changed after this job was queued.",
        )
    checkpoint = None
    pointer = job.progress.get("checkpoint") or job.payload.get("resume_checkpoint")
    if pointer:
        expected_prefix = f"artifacts/agent-document-map/{job.payload['project_id']}/{job.aggregate_id}/{job.payload['run_id']}/"
        if not str(pointer.get("object_key", "")).startswith(expected_prefix):
            raise NonRetryableJobError(
                error_code="document_map_checkpoint_scope_mismatch",
                message="Checkpoint belongs to another document run.",
            )
        with container.object_store.open(pointer["object_key"]) as stream:
            raw = stream.read(32 * 1024 * 1024 + 1)
        if (
            len(raw) != pointer["size"]
            or hashlib.sha256(raw).hexdigest() != pointer["sha256"]
        ):
            raise NonRetryableJobError(
                error_code="document_map_checkpoint_corrupt",
                message="Document Map checkpoint failed integrity validation.",
            )
        checkpoint = json.loads(raw)
    result = execute_document_map(
        source_path=resolve_source_record_path(document).resolve(strict=True),
        project_id=job.payload["project_id"],
        document_id=job.aggregate_id,
        source_sha256=job.payload["source_sha256"],
        run_id=job.payload["run_id"],
        expected_kit_sha256=job.payload["kit_bundle_sha256"],
        codex_bin=container.settings.codex_bin,
        codex_home=container.settings.codex_home,
        repository_root=ROOT,
        timeout_seconds=job.payload["timeout_seconds"],
        object_store=container.object_store,
        repository=container.agent_structure_loop_repository,
        checkpoint=checkpoint,
        checkpoint_sink=lambda value, stage: _save_checkpoint(
            container, job, context, value, stage
        ),
        cancellation_requested=context.cancellation_requested,
        before_publication=context.begin_publication,
    )
    if not job.payload.get("auto_source_items", False):
        return result
    result = dict(result)
    result["source_item_job_ids"] = []
    if not result.get("artifact_id") or not result.get("working_revision"):
        result["source_items_status"] = "NOT_STARTED_NO_PUBLISHABLE_MAP"
        return result
    from app.application.agent_workflow.native_structure_bridge import get_native_structure_reference
    response = SimpleNamespace(headers={})
    reference = get_native_structure_reference(job.payload["project_id"], job.aggregate_id, response)
    head = reference.get("working_revision_head") or {}
    if (reference.get("source_sha256") != job.payload["source_sha256"]
            or reference.get("artifact_id") != result["artifact_id"]
            or head.get("revision_sha256") != result.get("revision_sha256")):
        result["source_items_status"] = "NOT_STARTED_MAP_NO_LONGER_CURRENT"
        return result
    # The stable parent-job command survives retries. Queueing is a server action,
    # not a browser lifecycle callback, and the Map's PARTIAL status is retained.
    started = start_document_source_items(job.payload["project_id"], job.aggregate_id,
        SourceItemDocumentStartRequestV1(command_id=f"{job.job_id}:source-items",
            expected_source_sha256=job.payload["source_sha256"], expected_artifact_id=result["artifact_id"]), response)
    result["source_item_job_ids"] = [item["job_id"] for item in started.get("jobs", []) if item.get("job_id")]
    result["source_items_status"] = started["status"]
    result["source_item_expected_scope_ids"] = started.get("expected_scope_ids", [])
    fresh_job = container.jobs.get(job.job_id)
    context.heartbeat(progress={**(fresh_job.progress if fresh_job else job.progress),
        "stage": "SOURCE_ITEMS_QUEUED", "source_item_job_ids": result["source_item_job_ids"],
        "message": "原文结构已可核对，后续文字、表格和图像原文条目任务已持久入队。"})
    return result
