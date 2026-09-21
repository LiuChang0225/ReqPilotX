from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.pdf_source_crop_renderer import (
    PdfSourceCropRejected,
    render_pdf_source_crop,
)
from reqpilotx_server.adapters.persistence.postgres_jobs import JobConflictError
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction import (
    build_extraction_shadow_preview,
)
from reqpilotx_server.application.extraction.contracts import (
    RestorationRunRequest,
)
from reqpilotx_server.application.runtime import RUNTIME_STATE
from reqpilotx_server.application.transport import (
    BinaryArtifact,
    FileArtifact,
    ResponseSink,
)
from reqpilotx_server.infrastructure.runtime_settings import DatabaseBackend
from reqpilotx_server.ports.job_queue import DurableJob, JobExecutionContext, JobStatus
from reqpilotx_server.services.extraction_shadow_runs import (
    SHADOW_RUN_CACHE_VERSION,
    SHADOW_RUN_SCHEMA_VERSION,
    ExtractionShadowRunRejected,
    build_extraction_shadow_artifacts,
    get_extraction_shadow_details_path,
    get_latest_extraction_shadow_run,
    get_extraction_shadow_page_path,
    get_extraction_shadow_run,
    get_extraction_shadow_visual_capabilities,
    start_extraction_shadow_run,
)
from reqpilotx_server.services.extraction_visual_routes import (
    visual_runtime_audit,
)

logger = logging.getLogger(__name__)

def _raise_restoration_run_error(exc: ExtractionShadowRunRejected) -> None:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    raise ApplicationError(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
        headers=_ASSET_RESPONSE_HEADERS,
    ) from exc

def _restoration_snapshot_from_durable(job: DurableJob) -> dict[str, Any]:
    if job.status is JobStatus.SUCCEEDED and isinstance(job.result, dict):
        return dict(job.result)
    payload = dict(job.payload or {})
    progress = dict(job.progress or {})
    run_status = {
        JobStatus.PENDING: "QUEUED",
        JobStatus.RUNNING: "BUILDING",
        JobStatus.CANCEL_REQUESTED: "BUILDING",
        JobStatus.SUCCEEDED: "READY",
        JobStatus.FAILED: "FAILED",
        JobStatus.CANCELLED: "CANCELLED",
    }[job.status]
    return {
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "cache_version": SHADOW_RUN_CACHE_VERSION,
        "run_id": job.job_id,
        "project_id": str(payload.get("project_id") or ""),
        "document_id": str(payload.get("document_id") or ""),
        "run_status": run_status,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "completed_at": "",
        "progress": {
            "stage": str(progress.get("stage") or run_status),
            "page_count": int(progress.get("page_count") or 0),
            "pages_ready": int(progress.get("pages_ready") or 0),
        },
        "preview": {},
        "details_counts": {},
        "metrics": {},
        "error": (
            {
                "code": job.error_code,
                "message": job.error_message,
            }
            if job.status is JobStatus.FAILED
            else {}
        ),
    }

def _enqueue_durable_document_restoration(
    *,
    project_id: str,
    document: dict[str, Any],
    initial_page: int,
    visual_provider: str,
    force: bool,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.document_jobs import (
        _prepare_llm_document_visual_runtime,
    )
    from reqpilotx_server.application.runtime import _durable_jobs, _runtime_record_path

    document_id = str(document.get("id") or "")
    source_sha256 = str(document.get("source_sha256") or "")
    path = _runtime_record_path(document)
    _authorized, visual_strategy = _prepare_llm_document_visual_runtime(
        source=path,
        source_sha256=source_sha256,
        provider_name=visual_provider,
    )
    strategy_sha256 = hashlib.sha256(
        json.dumps(
            visual_strategy,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    idempotency_key = f"{source_sha256}:{strategy_sha256[:32]}"
    if force:
        idempotency_key = f"{idempotency_key}:{uuid.uuid4().hex}"
    try:
        durable = _durable_jobs().enqueue(
            kind="document.restore",
            aggregate_type="sor-document",
            aggregate_id=document_id,
            idempotency_key=idempotency_key,
            payload={
                "project_id": project_id,
                "document_id": document_id,
                "source_sha256": source_sha256,
                "initial_page": int(initial_page),
                "visual_provider": str(visual_provider or "off"),
                "visual_strategy": visual_strategy,
            },
            priority=20,
            max_attempts=3,
        )
    except JobConflictError:
        durable = _durable_jobs().get_latest(
            kind="document.restore",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        if durable is None:
            raise
    return _restoration_snapshot_from_durable(durable)

def _require_durable_document_restoration(
    *,
    project_id: str,
    document_id: str,
    run_id: str,
) -> DurableJob:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, _durable_jobs

    durable = _durable_jobs().get(run_id)
    if (
        durable is None
        or durable.kind != "document.restore"
        or durable.aggregate_type != "sor-document"
        or durable.aggregate_id != document_id
        or str(durable.payload.get("project_id") or "") != project_id
    ):
        raise ApplicationError(
            status_code=404,
            detail="Document restoration run not found.",
            headers=_ASSET_RESPONSE_HEADERS,
        )
    return durable

def _read_runtime_json_artifact(
    descriptor: dict[str, Any] | None,
    *,
    not_found_message: str,
) -> bytes:
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    if RUNTIME_STATE.container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    artifact = dict(descriptor or {})
    storage_key = str(artifact.get("storage_key") or "").strip()
    expected_sha256 = str(artifact.get("sha256") or "").strip().lower()
    expected_size = int(artifact.get("size") or 0)
    if (
        not storage_key
        or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)
        or expected_size < 0
        or expected_size > 64 * 1024 * 1024
    ):
        raise ApplicationError(
            status_code=409,
            detail="Document restoration artifact metadata is invalid.",
            headers=_ASSET_RESPONSE_HEADERS,
        )
    stored = RUNTIME_STATE.container.object_store.stat(storage_key)
    if stored is None:
        raise ApplicationError(
            status_code=404,
            detail=not_found_message,
            headers=_ASSET_RESPONSE_HEADERS,
        )
    if int(stored.size) != expected_size or stored.sha256 != expected_sha256:
        raise ApplicationError(
            status_code=409,
            detail="Document restoration artifact identity does not match.",
            headers=_ASSET_RESPONSE_HEADERS,
        )
    with RUNTIME_STATE.container.object_store.open(storage_key) as source:
        payload = source.read(expected_size + 1)
    if (
        not isinstance(payload, bytes)
        or len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ApplicationError(
            status_code=409,
            detail="Document restoration artifact verification failed.",
            headers=_ASSET_RESPONSE_HEADERS,
        )
    return payload

def run_durable_document_restoration(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.document_jobs import (
        _prepare_llm_document_visual_runtime,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.ingestion import _put_runtime_json
    from reqpilotx_server.application.runtime import _runtime_record_path

    payload = dict(durable_job.payload or {})
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    document = _require_project_document(project_id, document_id)
    source_sha256 = str(document.get("source_sha256") or "")
    if source_sha256 != str(payload.get("source_sha256") or ""):
        raise RuntimeError("Document source identity changed before restoration.")
    path = _runtime_record_path(document)
    authorized, visual_strategy = _prepare_llm_document_visual_runtime(
        source=path,
        source_sha256=source_sha256,
        provider_name=str(payload.get("visual_provider") or "off"),
    )
    if visual_strategy != payload.get("visual_strategy"):
        raise RuntimeError("Visual strategy identity changed before restoration.")
    context.heartbeat(progress={"stage": "BUILDING", "page_count": 0, "pages_ready": 0})
    started = time.perf_counter()
    preview = build_extraction_shadow_preview(
        path,
        expected_source_sha256=source_sha256,
        visual_provider=str(payload.get("visual_provider") or "off"),
        visual_runtime=(authorized.runtime if authorized is not None else None),
        visual_authorization=(
            visual_runtime_audit(authorized) if authorized is not None else None
        ),
        collection="document_outline",
        offset=0,
        limit=5_000,
    )
    metadata, pages, details = build_extraction_shadow_artifacts(
        preview,
        run_id=durable_job.job_id,
        project_id=project_id,
        document_id=document_id,
    )
    initial_page = int(payload.get("initial_page") or 1)
    ordered_pages = sorted(
        pages,
        key=lambda page: (page != initial_page, page),
    )
    page_artifacts: dict[str, dict[str, Any]] = {}
    for ready_count, page_number in enumerate(ordered_pages, start=1):
        if context.cancellation_requested():
            return {"cancelled": True}
        artifact = _put_runtime_json(
            storage_key=(
                f"runtime/extraction-runs/{durable_job.job_id}/"
                f"pages/{page_number}.json"
            ),
            payload=pages[page_number],
        )
        page_artifacts[str(page_number)] = artifact
        context.heartbeat(
            progress={
                "stage": "SERIALIZING",
                "page_count": len(pages),
                "pages_ready": ready_count,
            }
        )
    details_artifact = _put_runtime_json(
        storage_key=f"runtime/extraction-runs/{durable_job.job_id}/details.json",
        payload=details,
    )
    completed_at = datetime.now(timezone.utc).isoformat()
    return {
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "cache_version": SHADOW_RUN_CACHE_VERSION,
        "run_id": durable_job.job_id,
        "project_id": project_id,
        "document_id": document_id,
        "run_status": "READY",
        "created_at": durable_job.created_at.isoformat(),
        "updated_at": completed_at,
        "completed_at": completed_at,
        "progress": {
            "stage": "READY",
            "page_count": len(pages),
            "pages_ready": len(pages),
        },
        "preview": metadata,
        "details_counts": dict(metadata.get("details_counts") or {}),
        "metrics": {
            "build_and_serialize_ms": round(
                (time.perf_counter() - started) * 1000,
                1,
            ),
            "page_count": len(pages),
            "artifact_bytes": (
                sum(int(item["size"]) for item in page_artifacts.values())
                + int(details_artifact["size"])
            ),
        },
        "artifacts": {
            "pages": page_artifacts,
            "details": details_artifact,
        },
        "error": {},
    }

def start_document_restoration_run(
    project_id: str,
    document_id: str,
    payload: RestorationRunRequest,
    response: ResponseSink,
) -> dict[str, Any]:
    """Start or reuse one version-bound full parse with page-addressable output."""
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, RUNTIME_SETTINGS


    document = _require_restoration_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        run = _enqueue_durable_document_restoration(
            project_id=project_id,
            document=document,
            initial_page=payload.initial_page,
            force=payload.force,
            visual_provider=payload.visual_provider,
        )
    else:
        try:
            run = start_extraction_shadow_run(
                project_id=project_id,
                document=document,
                initial_page=payload.initial_page,
                force=payload.force,
                visual_provider=payload.visual_provider,
            )
        except ExtractionShadowRunRejected as exc:
            _raise_restoration_run_error(exc)
    if str(run.get("run_status") or "").upper() not in {
        "READY",
        "FAILED",
        "CANCELLED",
        "STALE",
    }:
        response.status_code = 202
    return run

def get_document_restoration_capabilities(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    """Return source-bound remote choices without reading provider credentials."""
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS


    document = _require_restoration_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    try:
        return get_extraction_shadow_visual_capabilities(
            project_id=project_id,
            document=document,
        )
    except ExtractionShadowRunRejected as exc:
        _raise_restoration_run_error(exc)


def get_latest_document_restoration_run(
    project_id: str,
    document_id: str,
    response: ResponseSink,
) -> dict[str, Any] | None:
    """Resume the newest source-current run without creating background work."""
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import (
        _ASSET_RESPONSE_HEADERS,
        RUNTIME_SETTINGS,
        _durable_jobs,
    )

    document = _require_restoration_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        durable = _durable_jobs().get_latest(
            kind="document.restore",
            aggregate_type="sor-document",
            aggregate_id=document_id,
        )
        if (
            durable is None
            or str(durable.payload.get("project_id") or "") != project_id
        ):
            return None
        snapshot = _restoration_snapshot_from_durable(durable)
        snapshot["visual_provider"] = str(
            durable.payload.get("visual_provider") or "off"
        )
        if str(durable.payload.get("source_sha256") or "") != str(
            document.get("source_sha256") or ""
        ):
            snapshot["run_status"] = "STALE"
            snapshot["progress"] = {
                **dict(snapshot.get("progress") or {}),
                "stage": "STALE",
            }
        return snapshot
    try:
        return get_latest_extraction_shadow_run(
            project_id=project_id,
            document=document,
        )
    except ExtractionShadowRunRejected as exc:
        _raise_restoration_run_error(exc)


def get_document_restoration_run(
    project_id: str,
    document_id: str,
    run_id: str,
    response: ResponseSink,
) -> dict[str, Any]:
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, RUNTIME_SETTINGS

    document = _require_restoration_document(project_id, document_id)
    response.headers.update(_ASSET_RESPONSE_HEADERS)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        durable = _require_durable_document_restoration(
            project_id=project_id,
            document_id=document_id,
            run_id=run_id,
        )
        return _restoration_snapshot_from_durable(durable)
    try:
        return get_extraction_shadow_run(
            project_id=project_id,
            document=document,
            run_id=run_id,
        )
    except ExtractionShadowRunRejected as exc:
        _raise_restoration_run_error(exc)

def get_document_restoration_page(
    project_id: str,
    document_id: str,
    run_id: str,
    page_number: int,
) -> ResponseSink:
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, RUNTIME_SETTINGS

    document = _require_restoration_document(project_id, document_id)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        durable = _require_durable_document_restoration(
            project_id=project_id,
            document_id=document_id,
            run_id=run_id,
        )
        if durable.status is not JobStatus.SUCCEEDED or not durable.result:
            raise ApplicationError(
                status_code=409,
                detail="Document restoration run is not ready.",
                headers=_ASSET_RESPONSE_HEADERS,
            )
        pages = dict((durable.result.get("artifacts") or {}).get("pages") or {})
        payload = _read_runtime_json_artifact(
            pages.get(str(page_number)),
            not_found_message="Document restoration page not found.",
        )
        return BinaryArtifact(
            content=payload,
            media_type="application/json",
            headers={
                **_ASSET_RESPONSE_HEADERS,
                "X-Content-Type-Options": "nosniff",
            },
        )
    try:
        path = get_extraction_shadow_page_path(
            project_id=project_id,
            document=document,
            run_id=run_id,
            page_number=page_number,
        )
    except ExtractionShadowRunRejected as exc:
        _raise_restoration_run_error(exc)
    return FileArtifact(
        path,
        media_type="application/json",
        headers={
            **_ASSET_RESPONSE_HEADERS,
            "X-Content-Type-Options": "nosniff",
        },
    )

def get_document_restoration_details(
    project_id: str,
    document_id: str,
    run_id: str,
) -> ResponseSink:
    from reqpilotx_server.application.extraction.provider_support import (
        _require_restoration_document,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, RUNTIME_SETTINGS

    document = _require_restoration_document(project_id, document_id)
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        durable = _require_durable_document_restoration(
            project_id=project_id,
            document_id=document_id,
            run_id=run_id,
        )
        if durable.status is not JobStatus.SUCCEEDED or not durable.result:
            raise ApplicationError(
                status_code=409,
                detail="Document restoration run is not ready.",
                headers=_ASSET_RESPONSE_HEADERS,
            )
        descriptor = dict(durable.result.get("artifacts") or {}).get("details")
        payload = _read_runtime_json_artifact(
            descriptor,
            not_found_message="Document restoration details not found.",
        )
        return BinaryArtifact(
            content=payload,
            media_type="application/json",
            headers={
                **_ASSET_RESPONSE_HEADERS,
                "X-Content-Type-Options": "nosniff",
            },
        )
    try:
        path = get_extraction_shadow_details_path(
            project_id=project_id,
            document=document,
            run_id=run_id,
        )
    except ExtractionShadowRunRejected as exc:
        _raise_restoration_run_error(exc)
    return FileArtifact(
        path,
        media_type="application/json",
        headers={
            **_ASSET_RESPONSE_HEADERS,
            "X-Content-Type-Options": "nosniff",
        },
    )

def get_document_source_crop(
    project_id: str,
    document_id: str,
    source_sha256: str,
    page: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
) -> ResponseSink:
    """Render one bounded PDF region; the UI calls it from grounded placements."""
    from reqpilotx_server.application.projects import _require_project
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS, _runtime_record_path


    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document.get("project_id") != project_id:
        raise ApplicationError(
            status_code=404,
            detail="SOR document not found for this project.",
        )
    path = _runtime_record_path(document)
    if not path.exists() or not path.is_file():
        raise ApplicationError(status_code=404, detail="SOR source file not found.")
    if path.suffix.lower() != ".pdf":
        raise ApplicationError(
            status_code=422,
            detail="Source crops are available only for PDF documents.",
        )
    requested_source_sha256 = str(source_sha256 or "").strip().lower()
    stored_source_sha256 = str(document.get("source_sha256") or "").strip().lower()
    if stored_source_sha256 and requested_source_sha256 != stored_source_sha256:
        raise ApplicationError(
            status_code=409,
            detail={
                "code": "pdf_source_crop_source_unavailable",
                "message": "The requested preview identity does not match the registered source.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    try:
        payload = render_pdf_source_crop(
            path,
            page_number=page,
            bbox=(x0, y0, x1, y1),
            expected_source_sha256=requested_source_sha256,
        )
    except PdfSourceCropRejected as exc:
        status_code = (
            409 if exc.code.value == "pdf_source_crop_source_unavailable" else 422
        )
        raise ApplicationError(
            status_code=status_code,
            detail={"code": exc.code.value, "message": str(exc)},
            headers=_ASSET_RESPONSE_HEADERS,
        ) from exc
    return BinaryArtifact(
        content=payload,
        media_type="image/png",
        headers={
            **_ASSET_RESPONSE_HEADERS,
            "Content-Disposition": "inline",
            "X-Content-Type-Options": "nosniff",
        },
    )
