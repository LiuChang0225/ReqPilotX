from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app import db
from app.adapters.persistence.postgres_jobs import JobConflictError
from app.application.errors import ApplicationError
from app.application.processing_profiles import (
    require_knowledge_upload_capability,
    selected_knowledge_profile_id,
)
from app.application.runtime import RUNTIME_STATE
from app.application.transport import (
    FileArtifact,
    JsonArtifact,
    UploadInput,
    form_value,
    upload_required,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.job_queue import DurableJob, JobExecutionContext, JobStatus
from app.runtime_readiness import (
    build_ai_profile_check,
)
from app.services.ai_copilot import (
    INDEX_PAYLOAD_DIR,
    get_ai_profile_missing_config,
    list_ai_copilot_profiles,
    load_ai_copilot_config,
    serialize_ai_profile,
    sync_knowledge_document_index,
)

logger = logging.getLogger(__name__)

def _attach_knowledge_syncs(documents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    attached: list[dict[str, Any]] = []
    for document in documents:
        current = dict(document)
        current["retrieval_syncs"] = db.list_knowledge_document_syncs(
            str(current.get("id") or "")
        )
        attached.append(current)
    return attached

def _sync_knowledge_document_targets(
    project_id: str,
    document: dict[str, Any],
    *,
    profile_ids: list[str] | None = None,
) -> dict[str, Any]:
    from app.application.ingestion import _hash_file, _publish_staged_object
    from app.application.runtime import RUNTIME_SETTINGS

    runtime_object = (
        RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL
    )
    sync = sync_knowledge_document_index(
        project_id,
        document,
        profile_ids=profile_ids,
        payload_dir=(
            RUNTIME_SETTINGS.scratch_root / "knowledge-index-staging"
            if runtime_object
            else None
        ),
    )
    payload_path = Path(str(sync.get("payload_path") or ""))
    payload_identity = str(payload_path)
    object_created = False
    if runtime_object:
        if not payload_path.is_file():
            raise RuntimeError("Knowledge index payload was not generated.")
        payload_sha256 = _hash_file(payload_path)
        payload_size = payload_path.stat().st_size
        payload_identity = (
            f"knowledge-index/{project_id}/{document.get('id', '')}/"
            f"{payload_sha256}.json"
        )
        object_created = _publish_staged_object(
            target=payload_path,
            storage_key=payload_identity,
            expected_sha256=payload_sha256,
            media_type="application/json",
        )
        db.register_stored_object(
            storage_key=payload_identity,
            sha256=payload_sha256,
            size_bytes=payload_size,
            media_type="application/json",
            object_kind="knowledge-index",
        )
    try:
        for target in (sync.get("targets") or {}).values():
            db.upsert_knowledge_document_sync(
                str(document.get("id") or ""),
                profile_id=str(target.get("profile_id") or "").strip(),
                provider=str(target.get("provider") or "").strip(),
                retrieval_backend=str(
                    target.get("retrieval_backend") or ""
                ).strip(),
                status=(
                    str(target.get("status") or "PENDING").strip().upper()
                    or "PENDING"
                ),
                error_message=str(target.get("error") or ""),
                synced_at=str(target.get("synced_at") or ""),
                chunk_count=int(target.get("chunk_count") or 0),
                payload_path=payload_identity,
            )
    except BaseException:
        if runtime_object and object_created and RUNTIME_STATE.container is not None:
            if db.release_stored_object_if_unreferenced(payload_identity):
                RUNTIME_STATE.container.object_store.delete(payload_identity)
        raise
    finally:
        if runtime_object:
            payload_path.unlink(missing_ok=True)
    updated = (
        db.update_knowledge_document_retrieval_state(
            str(document.get("id") or ""),
            retrieval_sync_status=str(sync.get("status") or "READY"),
            retrieval_sync_error=str(sync.get("error") or ""),
            retrieval_synced_at=str(sync.get("synced_at") or ""),
        )
        or document
    )
    updated["retrieval_syncs"] = db.list_knowledge_document_syncs(
        str(document.get("id") or "")
    )
    return updated

def _knowledge_sync_job_snapshot(job: DurableJob) -> dict[str, Any]:
    project_id = str(job.payload.get("project_id") or "")
    payload: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status.value,
        "poll_url": (
            f"/api/projects/{project_id}/knowledge-sync-jobs/{job.job_id}"
        ),
        "progress": dict(job.progress or {}),
        "error": (
            {
                "code": job.error_code,
                "message": job.error_message,
            }
            if job.status is JobStatus.FAILED
            else {}
        ),
    }
    if job.status is JobStatus.SUCCEEDED and isinstance(job.result, dict):
        payload["result"] = dict(job.result)
    return payload

def _enqueue_durable_knowledge_sync(
    *,
    project_id: str,
    document: dict[str, Any],
    profile_ids: list[str],
) -> dict[str, Any]:
    from app.application.runtime import _durable_jobs

    document_id = str(document.get("id") or "")
    normalized_profiles = sorted(
        {
            str(profile_id or "").strip()
            for profile_id in profile_ids
            if str(profile_id or "").strip()
        }
    )
    if not normalized_profiles:
        raise ApplicationError(
            status_code=409,
            detail="No retrieval-ready AI profile is available for knowledge sync.",
        )
    for profile_id in normalized_profiles:
        config = load_ai_copilot_config(profile_id)
        db.upsert_knowledge_document_sync(
            document_id,
            profile_id=config.profile_id,
            provider=config.provider,
            retrieval_backend=config.retrieval_backend,
            status="PENDING",
        )
    try:
        durable = _durable_jobs().enqueue(
            kind="knowledge.sync",
            aggregate_type="knowledge-document",
            aggregate_id=document_id,
            idempotency_key=uuid.uuid4().hex,
            payload={
                "project_id": project_id,
                "document_id": document_id,
                "source_sha256": str(document.get("source_sha256") or ""),
                "profile_ids": normalized_profiles,
            },
            priority=30,
            max_attempts=3,
        )
    except JobConflictError:
        durable = _durable_jobs().get_latest(
            kind="knowledge.sync",
            aggregate_type="knowledge-document",
            aggregate_id=document_id,
        )
        if durable is None:
            raise
    return _knowledge_sync_job_snapshot(durable)

def run_durable_knowledge_sync(
    durable_job: DurableJob,
    context: JobExecutionContext,
) -> dict[str, Any]:
    from app.application.knowledge_ingestion import (
        extract_knowledge_document_content,
    )
    from app.application.runtime import _runtime_record_path

    payload = dict(durable_job.payload or {})
    project_id = str(payload.get("project_id") or "")
    document_id = str(payload.get("document_id") or "")
    document = db.get_knowledge_document(document_id)
    if (
        not document
        or str(document.get("project_id") or "") != project_id
        or str(document.get("source_sha256") or "")
        != str(payload.get("source_sha256") or "")
    ):
        raise RuntimeError("Knowledge document identity changed before sync.")
    context.heartbeat(progress={"stage": "parsing"})
    if context.cancellation_requested():
        return {"cancelled": True}
    parsed = extract_knowledge_document_content(
        _runtime_record_path(document),
        expected_sha256=str(document.get("source_sha256") or ""),
    )
    if context.cancellation_requested():
        return {"cancelled": True}
    document = db.update_knowledge_document_content(
        document_id,
        file_type=parsed.file_type,
        extracted_text=parsed.extracted_text,
    )
    if not document:
        raise RuntimeError("Knowledge document disappeared before indexing.")
    context.heartbeat(progress={"stage": "indexing"})
    if context.cancellation_requested():
        return {"cancelled": True}
    refreshed = _sync_knowledge_document_targets(
        project_id,
        document,
        profile_ids=[
            str(item or "") for item in payload.get("profile_ids") or []
        ],
    )
    return _public_knowledge_document(refreshed)

def _attach_knowledge_payloads_for_profile(
    documents: list[dict[str, Any]],
    *,
    profile_id: str,
) -> list[dict[str, Any]]:
    from app.application.runtime import RUNTIME_SETTINGS

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
        return documents
    if RUNTIME_STATE.container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    hydrated: list[dict[str, Any]] = []
    for document in documents:
        current = dict(document)
        sync = next(
            (
                item
                for item in current.get("retrieval_syncs") or []
                if str(item.get("profile_id") or "") == profile_id
                and str(item.get("status") or "").upper() == "READY"
            ),
            None,
        )
        storage_key = str((sync or {}).get("payload_storage_key") or "").strip()
        if not storage_key:
            raise ApplicationError(
                status_code=409,
                detail="Knowledge index payload is not ready for this AI profile.",
            )
        stored = RUNTIME_STATE.container.object_store.stat(storage_key)
        if stored is None or not 0 <= int(stored.size) <= 64 * 1024 * 1024:
            raise ApplicationError(
                status_code=409,
                detail="Knowledge index payload is unavailable.",
            )
        with RUNTIME_STATE.container.object_store.open(storage_key) as source:
            encoded = source.read(int(stored.size) + 1)
        if (
            not isinstance(encoded, bytes)
            or len(encoded) != int(stored.size)
            or (
                stored.sha256
                and hashlib.sha256(encoded).hexdigest() != stored.sha256
            )
        ):
            raise ApplicationError(
                status_code=409,
                detail="Knowledge index payload verification failed.",
            )
        try:
            index_payload = json.loads(encoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApplicationError(
                status_code=409,
                detail="Knowledge index payload is invalid.",
            ) from exc
        if not isinstance(index_payload, dict):
            raise ApplicationError(
                status_code=409,
                detail="Knowledge index payload is invalid.",
            )
        current["_index_payload"] = index_payload
        hydrated.append(current)
    return hydrated

def _public_knowledge_document(document: dict[str, Any]) -> dict[str, Any]:
    from app.application.runtime import _public_storage_record

    payload = _public_storage_record(document)
    payload["retrieval_syncs"] = [
        _public_storage_record(dict(sync))
        for sync in document.get("retrieval_syncs") or []
    ]
    payload.pop("_index_payload", None)
    return payload

def _ensure_profile_knowledge_syncs(
    project_id: str,
    knowledge_docs: list[dict[str, Any]],
    ai_config: Any,
) -> list[dict[str, Any]]:
    from app.application.runtime import RUNTIME_SETTINGS

    refreshed_docs: list[dict[str, Any]] = []
    pending_files: list[str] = []
    for document in _attach_knowledge_syncs(knowledge_docs):
        sync_state = next(
            (
                item
                for item in (document.get("retrieval_syncs") or [])
                if str(item.get("profile_id") or "").strip() == ai_config.profile_id
            ),
            None,
        )
        if (
            not sync_state
            or str(sync_state.get("status") or "").strip().upper() != "READY"
        ):
            if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
                document = _sync_knowledge_document_targets(
                    project_id, document, profile_ids=[ai_config.profile_id]
                )
                sync_state = next(
                    (
                        item
                        for item in (document.get("retrieval_syncs") or [])
                        if str(item.get("profile_id") or "").strip()
                        == ai_config.profile_id
                    ),
                    None,
                )
        if (
            sync_state
            and str(sync_state.get("status") or "").strip().upper() == "READY"
        ):
            refreshed_docs.append(document)
            continue
        pending_files.append(
            str(document.get("file_name") or document.get("id") or "knowledge-document")
        )
        refreshed_docs.append(document)
    if pending_files:
        names = ", ".join(pending_files)
        raise ApplicationError(
            status_code=409,
            detail=f"Knowledge sync for {ai_config.display_name} is incomplete: {names}",
        )
    return refreshed_docs

def _build_ai_profile_catalog(
    knowledge_docs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    from app.application.runtime import _merge_ai_profile_runtime_state

    profiles = list_ai_copilot_profiles()
    default_profile_id = load_ai_copilot_config().profile_id
    runtime_checks = {
        config.profile_id: build_ai_profile_check(config) for config in profiles
    }
    sync_index: dict[tuple[str, str], dict[str, Any]] = {}
    for document in knowledge_docs:
        for sync in document.get("retrieval_syncs") or []:
            sync_index[
                (str(document.get("id") or ""), str(sync.get("profile_id") or ""))
            ] = sync

    profile_payloads: list[dict[str, Any]] = []
    for config in profiles:
        ready_count = 0
        error_count = 0
        for document in knowledge_docs:
            sync = sync_index.get((str(document.get("id") or ""), config.profile_id))
            state = str((sync or {}).get("status") or "").strip().upper()
            if state == "READY":
                ready_count += 1
            elif state == "ERROR":
                error_count += 1
        knowledge_total = len(knowledge_docs)
        if knowledge_total <= 0:
            sync_status = "NOT_REQUIRED"
        elif ready_count == knowledge_total:
            sync_status = "READY"
        elif error_count == knowledge_total:
            sync_status = "ERROR"
        elif ready_count == 0 and error_count == 0:
            sync_status = "PENDING"
        else:
            sync_status = "PARTIAL"
        runtime_check = runtime_checks.get(config.profile_id, {})
        profile_payloads.append(
            _merge_ai_profile_runtime_state(
                {
                    **serialize_ai_profile(config),
                    "default": config.profile_id == default_profile_id,
                    "missing": get_ai_profile_missing_config(config),
                    "knowledge_sync": {
                        "status": sync_status,
                        "ready_count": ready_count,
                        "total_count": knowledge_total,
                    },
                },
                runtime_check,
            )
        )
    return profile_payloads

def _project_knowledge_sync_profile_ids(project_id: str) -> list[str]:
    """Return exactly the profile selected by the project's knowledge backend."""

    return [selected_knowledge_profile_id(project_id)]


def reindex_project_knowledge_for_selected_backend(
    *,
    project_id: str,
    knowledge_backend: str,
) -> dict[str, Any]:
    """Create fresh sync work for every ACTIVE file without deleting old indexes."""

    if knowledge_backend == "none":
        return {
            "schema_version": "project-knowledge-reindex/v1",
            "knowledge_backend": "none",
            "status": "NOT_REQUIRED",
            "document_count": 0,
            "jobs": [],
        }
    profile_ids = _project_knowledge_sync_profile_ids(project_id)
    active_documents = [
        item
        for item in _list_project_knowledge_documents(project_id)
        if str(item.get("status") or "ACTIVE").strip().upper() == "ACTIVE"
    ]
    from app.application.runtime import RUNTIME_SETTINGS

    jobs: list[dict[str, Any]] = []
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        for document in active_documents:
            jobs.append(
                _enqueue_durable_knowledge_sync(
                    project_id=project_id,
                    document=document,
                    profile_ids=profile_ids,
                )
            )
        status = "PENDING" if jobs else "NOT_REQUIRED"
    else:
        for document in active_documents:
            refreshed = _sync_knowledge_document_targets(
                project_id,
                document,
                profile_ids=profile_ids,
            )
            jobs.append(
                {
                    "document_id": str(document.get("id") or ""),
                    "status": str(
                        refreshed.get("retrieval_sync_status") or "READY"
                    ),
                }
            )
        status = (
            "READY"
            if all(str(item.get("status") or "") == "READY" for item in jobs)
            else "PARTIAL"
        )
        if not jobs:
            status = "NOT_REQUIRED"
    return {
        "schema_version": "project-knowledge-reindex/v1",
        "knowledge_backend": knowledge_backend,
        "status": status,
        "document_count": len(active_documents),
        "jobs": jobs,
    }

def _normalize_knowledge_doc_type(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"REFERENCE", "STANDARD", "SPEC", "REGULATION", "CASE"}:
        return normalized
    return "REFERENCE"

def _normalize_knowledge_doc_status(value: str) -> str:
    normalized = str(value or "").strip().upper()
    if normalized in {"ACTIVE", "DRAFT", "SUPERSEDED", "ARCHIVED"}:
        return normalized
    return "ACTIVE"

def _normalize_knowledge_authority(value: str) -> str:
    if hasattr(value, "default"):
        value = value.default
    normalized = str(value or "").strip().upper()
    if normalized in {
        "UNKNOWN",
        "INTERNAL_CONTROLLED",
        "CUSTOMER_APPROVED",
        "REGULATORY_AUTHORITY",
        "CERTIFICATION_BODY",
        "TEST_LAB",
    }:
        return normalized
    raise ApplicationError(
        status_code=422,
        detail="Unsupported knowledge document authority.",
    )

def _normalize_knowledge_claim_role(value: str) -> str:
    if hasattr(value, "default"):
        value = value.default
    normalized = str(value or "").strip().upper()
    if normalized in {
        "UNKNOWN",
        "NORMATIVE_REQUIREMENT",
        "DESIGN_DECLARATION",
        "VALIDATION_RESULT",
        "TEST_RESULT",
        "CERTIFICATION",
        "COMPLIANCE_RECORD",
        "RELEASED_DESIGN_EVIDENCE",
        "REFERENCE_CONTEXT",
    }:
        return normalized
    raise ApplicationError(
        status_code=422,
        detail="Unsupported knowledge document claim role.",
    )

def _normalize_knowledge_applicability(value: str) -> str:
    if hasattr(value, "default"):
        value = value.default
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) > 500:
        raise ApplicationError(
            status_code=422,
            detail="Knowledge document applicability must be at most 500 characters.",
        )
    return normalized or "UNKNOWN"

def _normalize_optional_date(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ApplicationError(
            status_code=422,
            detail="Knowledge document dates must use YYYY-MM-DD format.",
        )
    return text

def _normalize_knowledge_document_file_name(file_name: str) -> str:
    base_name = Path(str(file_name or "")).name
    collapsed = re.sub(r"\s+", " ", base_name).strip()
    return collapsed.casefold()

def _dedupe_knowledge_documents_by_file_name(
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for document in documents or []:
        normalized_name = _normalize_knowledge_document_file_name(
            str(document.get("file_name") or "")
        )
        dedupe_key = normalized_name or str(document.get("id") or "")
        if dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)
        deduped.append(document)
    return deduped

def _list_project_knowledge_documents(project_id: str) -> list[dict[str, Any]]:
    return _dedupe_knowledge_documents_by_file_name(
        db.list_knowledge_documents(project_id)
    )

def _find_duplicate_knowledge_document(
    project_id: str, file_name: str
) -> dict[str, Any] | None:
    normalized_name = _normalize_knowledge_document_file_name(file_name)
    if not normalized_name:
        return None
    for document in db.list_knowledge_documents(project_id):
        if (
            _normalize_knowledge_document_file_name(
                str(document.get("file_name") or "")
            )
            == normalized_name
        ):
            return document
    return None

def _list_matching_knowledge_documents(
    project_id: str, file_name: str
) -> list[dict[str, Any]]:
    normalized_name = _normalize_knowledge_document_file_name(file_name)
    if not normalized_name:
        return []
    return [
        document
        for document in db.list_knowledge_documents(project_id)
        if _normalize_knowledge_document_file_name(str(document.get("file_name") or ""))
        == normalized_name
    ]

def _collect_knowledge_document_cleanup_paths(
    documents: list[dict[str, Any]],
) -> list[Path]:
    from app.application.runtime import _runtime_record_path

    paths: list[Path] = []
    seen: set[str] = set()
    for document in documents or []:
        raw_paths = [str(_runtime_record_path(document)).strip()]
        raw_paths.extend(
            str(sync.get("payload_path") or "").strip()
            for sync in document.get("retrieval_syncs") or []
        )
        fallback_payload = (
            INDEX_PAYLOAD_DIR / f"{str(document.get('id') or '').strip()}.json"
        )
        raw_paths.append(str(fallback_payload))
        for raw_path in raw_paths:
            if not raw_path:
                continue
            normalized_path = str(Path(raw_path))
            if normalized_path in seen:
                continue
            seen.add(normalized_path)
            paths.append(Path(raw_path))
    return paths

def _cleanup_knowledge_document_files(paths: list[Path]) -> tuple[list[str], list[str]]:
    deleted_paths: list[str] = []
    cleanup_errors: list[str] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            path.unlink()
            deleted_paths.append(str(path))
        except OSError as exc:
            logger.warning(
                "Failed to delete knowledge document artifact %s: %s", path, exc
            )
            cleanup_errors.append(f"{path}: {exc}")
    return deleted_paths, cleanup_errors

async def upload_knowledge_document(
    project_id: str,
    file: UploadInput = upload_required(),
    doc_type: str = form_value("REFERENCE"),
    doc_version: str = form_value(""),
    status: str = form_value("ACTIVE"),
    effective_from: str = form_value(""),
    effective_to: str = form_value(""),
    authority: str = form_value("UNKNOWN"),
    claim_role: str = form_value("UNKNOWN"),
    applicability: str = form_value("UNKNOWN"),
) -> dict:
    from app.application.ingestion import (
        _inspect_uploaded_document,
        _object_key_for_upload,
        _prepare_uploaded_document,
        _publish_staged_object,
        _safe_upload_storage_name,
        _store_uploaded_document,
        _upload_staging_dir,
    )
    from app.application.projects import _require_project
    from app.application.runtime import RUNTIME_SETTINGS

    _require_project(project_id)
    selected_profile_ids: list[str] = []
    deferred_sync_reason = ""
    try:
        selected_profile_ids = _project_knowledge_sync_profile_ids(project_id)
    except ApplicationError as exc:
        # Durable source storage is a core local capability. Embedding and
        # retrieval are optional enrichments and must never block saving the
        # user's knowledge file.
        detail = exc.detail
        deferred_sync_reason = (
            str(detail.get("message") or detail.get("reason_code") or "")
            if isinstance(detail, dict)
            else str(detail or "")
        ) or "检索服务尚未连接，可稍后同步。"
    file_name = file.filename or f"knowledge_{uuid.uuid4().hex}.txt"
    duplicate = _find_duplicate_knowledge_document(project_id, file_name)
    if duplicate is not None:
        raise ApplicationError(
            status_code=409,
            detail=f"Knowledge document '{file_name}' already exists in this project.",
        )
    target = _upload_staging_dir("knowledge") / (
        f"{uuid.uuid4().hex}_{_safe_upload_storage_name(file_name)}"
    )
    await _store_uploaded_document(file, target)

    runtime_object = RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL
    if runtime_object:
        report = _inspect_uploaded_document(target)
        parsed_file_type = report.identity.detected_format.value.upper()
        parsed_text = ""
    else:
        prepared = _prepare_uploaded_document(target)
        report = prepared.preflight
        parsed_file_type = prepared.parsed.file_type
        parsed_text = prepared.parsed.text
    storage_identity = str(target)
    object_created = False
    media_type = (
        str(file.content_type or "").strip()
        or mimetypes.guess_type(file_name)[0]
        or "application/octet-stream"
    )
    if runtime_object:
        storage_identity = _object_key_for_upload(
            kind="knowledge-documents",
            project_id=project_id,
            source_sha256=report.source_sha256,
            file_name=file_name,
        )
        object_created = _publish_staged_object(
            target=target,
            storage_key=storage_identity,
            expected_sha256=report.source_sha256,
            media_type=media_type,
        )
    try:
        document = db.add_knowledge_document(
            project_id=project_id,
            file_name=file_name,
            file_type=parsed_file_type,
            file_path=storage_identity,
            extracted_text=parsed_text,
            doc_type=_normalize_knowledge_doc_type(doc_type),
            doc_version=str(doc_version or "").strip(),
            status=_normalize_knowledge_doc_status(status),
            effective_from=_normalize_optional_date(effective_from),
            effective_to=_normalize_optional_date(effective_to),
            authority=_normalize_knowledge_authority(authority),
            claim_role=_normalize_knowledge_claim_role(claim_role),
            applicability=_normalize_knowledge_applicability(applicability),
            source_sha256=report.source_sha256,
            source_size=report.source_size,
            declared_extension=report.identity.declared_extension,
            detected_format=report.identity.detected_format.value,
            preflight_schema_version=report.schema_version,
            media_type=media_type,
        )
    except BaseException:
        if runtime_object and object_created and RUNTIME_STATE.container is not None:
            RUNTIME_STATE.container.object_store.delete(storage_identity)
        if not runtime_object:
            target.unlink(missing_ok=True)
        raise
    finally:
        if runtime_object:
            target.unlink(missing_ok=True)
    sync_job: dict[str, Any] | None = None
    if runtime_object and selected_profile_ids:
        sync_job = _enqueue_durable_knowledge_sync(
            project_id=project_id,
            document=document,
            profile_ids=selected_profile_ids,
        )
        document["retrieval_syncs"] = db.list_knowledge_document_syncs(
            str(document.get("id") or "")
        )
    elif selected_profile_ids:
        document = _sync_knowledge_document_targets(
            project_id,
            document,
            profile_ids=selected_profile_ids,
        )
    else:
        document = (
            db.update_knowledge_document_retrieval_state(
                str(document.get("id") or ""),
                retrieval_sync_status="PENDING",
                retrieval_sync_error="",
                retrieval_synced_at="",
            )
            or document
        )
        document["retrieval_syncs"] = []
    project = db.bump_project_knowledge_base_version(project_id)
    response_payload = {
        **_public_knowledge_document(document),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "retrieval_chunk_count": max(
            [
                int(item.get("chunk_count") or 0)
                for item in document.get("retrieval_syncs") or []
            ]
            or [0]
        ),
    }
    if sync_job is not None:
        response_payload["sync_job"] = sync_job
    if deferred_sync_reason:
        response_payload["retrieval_sync_deferred"] = True
        response_payload["retrieval_sync_message"] = deferred_sync_reason
    return response_payload

def list_knowledge_documents(project_id: str) -> list[dict]:
    from app.application.projects import _require_project

    _require_project(project_id)
    return [
        _public_knowledge_document(item)
        for item in _attach_knowledge_syncs(
            _list_project_knowledge_documents(project_id)
        )
    ]

def get_knowledge_document_file(
    project_id: str,
    document_id: str,
    expected_source_sha256: str | None = None,
) -> FileArtifact:
    """Read the registered original independently of indexing/analysis state."""
    from app.application.projects import _require_project
    from app.application.source_runtime import resolve_source_record_path

    _require_project(project_id)
    document = db.get_knowledge_document(document_id)
    if not document or document.get('project_id') != project_id:
        raise ApplicationError(status_code=404, detail='Knowledge document not found for this project.')
    source_sha = str(document.get('source_sha256') or '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', source_sha):
        raise ApplicationError(status_code=409, detail={'code':'KNOWLEDGE_SOURCE_IDENTITY_MISSING','message':'知识原件缺少有效的来源指纹，不能读取为已校验原件。'})
    if expected_source_sha256 is not None:
        expected = expected_source_sha256.strip().lower()
        if not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ApplicationError(status_code=422, detail={'code':'INVALID_SOURCE_SHA256','message':'来源指纹须为64位SHA-256。'})
        if expected != source_sha:
            raise ApplicationError(status_code=409, detail={'code':'KNOWLEDGE_SOURCE_VERSION_CHANGED','message':'知识文档来源已改变，请刷新文档后重试。'})
    media_type = str(document.get('media_type') or '').strip()
    if document.get('storage_key') and not media_type:
        with db.get_conn() as conn:
            metadata = conn.execute('SELECT sha256, media_type FROM stored_objects WHERE storage_key = ?', (document['storage_key'],)).fetchone()
        if metadata:
            if str(metadata['sha256']).lower() != source_sha:
                raise ApplicationError(status_code=409, detail={'code':'KNOWLEDGE_SOURCE_IDENTITY_MISMATCH','message':'知识原件的登记指纹不一致。'})
            media_type = str(metadata['media_type'] or '').strip()
    try:
        path = resolve_source_record_path(document)
        if not path.is_file():
            raise FileNotFoundError
        # Object-backed records are already byte-verified by RuntimeObjectFiles,
        # including cached files. Legacy SQLite paths need that check here.
        if not document.get('storage_key'):
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(chunk)
            if digest.hexdigest() != source_sha:
                raise ValueError('Source bytes differ')
    except FileNotFoundError as exc:
        raise ApplicationError(status_code=404, detail={'code':'KNOWLEDGE_SOURCE_NOT_FOUND','message':'已登记的知识原件文件不可用。'}) from exc
    except ValueError as exc:
        raise ApplicationError(status_code=409, detail={'code':'KNOWLEDGE_SOURCE_IDENTITY_MISMATCH','message':'知识原件与登记来源指纹不一致。'}) from exc
    except (OSError, RuntimeError) as exc:
        raise ApplicationError(status_code=503, detail={'code':'KNOWLEDGE_SOURCE_UNAVAILABLE','message':'知识原件存储暂不可用，请稍后重试。'}) from exc
    # The client receives a download name, never a stored path or object key.
    name = str(document.get('file_name') or 'knowledge-source').replace('\\','/').rsplit('/',1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip() or 'knowledge-source'
    media_type = media_type or mimetypes.guess_type(name)[0] or 'application/octet-stream'
    if '\r' in media_type or '\n' in media_type:
        media_type = 'application/octet-stream'
    disposition = 'inline' if media_type.split(';',1)[0].strip().lower() == 'application/pdf' else 'attachment'
    return FileArtifact(path,media_type=media_type,headers={
        'Content-Disposition':f"{disposition}; filename*=utf-8''{quote(name,safe='')}",
        'X-ReqPilot-Source-SHA256':source_sha,'ETag':f'"{source_sha}"','Cache-Control':'no-store',
        'X-Content-Type-Options':'nosniff',
    })

def delete_knowledge_document(project_id: str, document_id: str) -> dict:
    from app.application.ingestion import (
        _delete_runtime_object_if_unreferenced,
        _delete_runtime_object_key_if_unreferenced,
    )
    from app.application.projects import _require_project

    _require_project(project_id)
    target = next(
        (
            item
            for item in db.list_knowledge_documents(project_id)
            if str(item.get("id") or "").strip() == document_id
        ),
        None,
    )
    if not target:
        raise ApplicationError(
            status_code=404, detail="Knowledge document not found for this project."
        )

    related_documents = _list_matching_knowledge_documents(
        project_id, str(target.get("file_name") or "")
    ) or [target]
    related_documents = _attach_knowledge_syncs(related_documents)
    sync_storage_keys = {
        str(sync.get("payload_storage_key") or "").strip()
        for document in related_documents
        for sync in document.get("retrieval_syncs") or []
        if str(sync.get("payload_storage_key") or "").strip()
    }
    cleanup_paths = _collect_knowledge_document_cleanup_paths(related_documents)

    removed_document_ids: list[str] = []
    removed_documents: list[dict[str, Any]] = []
    for document in related_documents:
        removed = db.delete_knowledge_document(str(document.get("id") or ""))
        if removed is not None:
            removed_document_ids.append(str(removed.get("id") or ""))
            removed_documents.append(removed)

    project = db.bump_project_knowledge_base_version(project_id)
    for removed in removed_documents:
        _delete_runtime_object_if_unreferenced(removed)
    deleted_sync_objects = sum(
        1
        for storage_key in sync_storage_keys
        if _delete_runtime_object_key_if_unreferenced(storage_key)
    )
    deleted_paths, cleanup_errors = _cleanup_knowledge_document_files(cleanup_paths)
    return {
        "file_name": str(target.get("file_name") or ""),
        "removed_document_id": document_id,
        "removed_document_ids": removed_document_ids,
        "removed_count": len(removed_document_ids),
        "knowledge_base_version": int(project.get("knowledge_base_version") or 0),
        "deleted_paths": deleted_paths,
        "cleanup_errors": cleanup_errors,
        "deleted_sync_object_count": deleted_sync_objects,
    }

def resync_knowledge_document(
    project_id: str,
    document_id: str,
) -> Any:
    from app.application.projects import _require_project
    from app.application.runtime import RUNTIME_SETTINGS

    _require_project(project_id)
    require_knowledge_upload_capability(project_id)
    selected_profile_ids = _project_knowledge_sync_profile_ids(project_id)
    document = next(
        (
            item
            for item in _list_project_knowledge_documents(project_id)
            if str(item.get("id") or "").strip() == document_id
        ),
        None,
    )
    if not document:
        raise ApplicationError(
            status_code=404, detail="Knowledge document not found for this project."
        )
    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.POSTGRESQL:
        return JsonArtifact(
            status_code=202,
            content=_enqueue_durable_knowledge_sync(
                project_id=project_id,
                document=document,
                profile_ids=selected_profile_ids,
            ),
        )
    return _sync_knowledge_document_targets(
        project_id,
        document,
        profile_ids=selected_profile_ids,
    )

def get_knowledge_sync_job(project_id: str, job_id: str) -> dict[str, Any]:
    from app.application.projects import _require_project
    from app.application.runtime import _durable_jobs

    _require_project(project_id)
    durable = _durable_jobs().get(job_id)
    if (
        durable is None
        or durable.kind != "knowledge.sync"
        or str(durable.payload.get("project_id") or "") != project_id
    ):
        raise ApplicationError(status_code=404, detail="Knowledge sync job not found.")
    return _knowledge_sync_job_snapshot(durable)
