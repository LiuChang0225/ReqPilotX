from __future__ import annotations

import hashlib
import io
import json
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app import db
from app.application.errors import ApplicationError
from app.application.extraction import (
    DocumentIngestionRejected,
    PreparedDocument,
    inspect_document_for_ingestion,
    prepare_document_for_ingestion,
)
from app.application.runtime import RUNTIME_STATE
from app.application.transport import (
    FileArtifact,
    UploadInput,
    upload_required,
)
from app.infrastructure.runtime_settings import DatabaseBackend
from app.services.extraction_shadow_runs import (
    cancel_extraction_shadow_runs,
    purge_extraction_shadow_runs,
)
from app.services.llm_extraction_result_cache import (
    LlmExtractionResultCache,
)
from app.services.preview_cache import (
    cancel_preview_refresh,
    load_cached_preview,
    purge_preview_artifacts,
)
from app.services.table_assets import (
    prepare_table_asset_urls,
)
from app.services.visual_text_snapshot_cache import (
    VisualTextSnapshotCache,
)

logger = logging.getLogger(__name__)

def _normalize_preview_generation_id(value: object) -> str:
    return str(value or "").strip()

def _resolve_active_preview_generation_id(
    *,
    preview: dict | None = None,
    document: dict | None = None,
) -> str:
    preview_generation_id = _normalize_preview_generation_id(
        (preview or {}).get("preview_generation_id")
    )
    if preview_generation_id:
        return preview_generation_id
    return _normalize_preview_generation_id(
        (document or {}).get("preview_generation_id")
    )

def _visual_assets_match_active_generation(
    asset_generation_id: object,
    active_generation_id: str,
) -> bool:
    if not active_generation_id:
        return True
    return _normalize_preview_generation_id(asset_generation_id) == active_generation_id

def _strip_preview_visual_assets(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    copied["table_refs"] = []
    copied["content_blocks"] = []
    copied["has_table_asset"] = False
    copied["table_render_mode"] = ""
    return copied

def _row_visual_assets_match_active_generation(
    row: dict | None, active_generation_id: str
) -> bool:
    if not isinstance(row, dict):
        return False
    return _visual_assets_match_active_generation(
        row.get("preview_generation_id"), active_generation_id
    )

def _filter_table_assets_by_generation(
    assets: list[dict[str, Any]] | None,
    *,
    active_generation_id: str,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for asset in assets or []:
        if not isinstance(asset, dict):
            continue
        if not _visual_assets_match_active_generation(
            asset.get("preview_generation_id"), active_generation_id
        ):
            continue
        filtered.append(asset)
    return filtered

def _asset_file_ready(path_value: object) -> bool:
    path = Path(str(path_value or "").strip())
    if not path.exists():
        return False
    try:
        return path.stat().st_size > 0
    except OSError:
        return False

def _table_asset_variant_is_ready(
    asset: dict | None,
    *,
    require_snapshot: bool = False,
    require_source: bool = False,
) -> bool:
    if not isinstance(asset, dict):
        return False
    if require_snapshot and not _asset_file_ready(asset.get("snapshot_path")):
        return False
    if require_source and not _asset_file_ready(asset.get("source_snapshot_path")):
        return False
    return True

def _resolve_document_table_asset(
    project_id: str,
    document_id: str,
    table_id: str,
    *,
    requirement_set_id: str = "",
    require_snapshot: bool = False,
    require_source: bool = False,
) -> dict | None:
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )

    set_id = str(requirement_set_id or "").strip()
    if not set_id:
        active_set_id = str(document.get("active_requirement_set_id") or "").strip()
        active_set = db.get_requirement_set(active_set_id) if active_set_id else None
        active_set_generation_id = _normalize_preview_generation_id(
            (active_set or {}).get("preview_generation_id")
        )
        document_generation_id = _normalize_preview_generation_id(
            document.get("preview_generation_id")
        )
        if (
            active_set_id
            and active_set_generation_id
            and document_generation_id
            and active_set_generation_id != document_generation_id
        ):
            set_id = active_set_id
    target_set = None
    if set_id:
        target_set = db.get_requirement_set(set_id)
        if (
            not target_set
            or target_set.get("project_id") != project_id
            or target_set.get("sor_document_id") != document_id
        ):
            raise ApplicationError(
                status_code=404,
                detail="Requirement set not found for this SOR document.",
            )
    active_generation_id = _normalize_preview_generation_id(
        target_set.get("preview_generation_id")
        if target_set
        else document.get("preview_generation_id")
    )
    preview_asset = None
    active_set_id = str(document.get("active_requirement_set_id") or "").strip()
    if not set_id or set_id == active_set_id:
        preview, _ = load_cached_preview(document)
        preview_assets = (
            (preview or {}).get("table_assets") if isinstance(preview, dict) else []
        )
        for item in preview_assets or []:
            if (
                isinstance(item, dict)
                and str(item.get("table_id") or "").strip() == table_id
            ):
                preview_asset = item
                break
        if preview_asset and not _visual_assets_match_active_generation(
            preview_asset.get("preview_generation_id")
            or (preview or {}).get("preview_generation_id"),
            active_generation_id,
        ):
            preview_asset = None

    db_asset = db.get_document_table_asset(
        project_id, document_id, table_id, requirement_set_id=set_id
    )
    if (
        db_asset
        and not set_id
        and not _visual_assets_match_active_generation(
            db_asset.get("preview_generation_id"), active_generation_id
        )
    ):
        db_asset = None
    if _table_asset_variant_is_ready(
        preview_asset,
        require_snapshot=require_snapshot,
        require_source=require_source,
    ):
        return preview_asset
    if _table_asset_variant_is_ready(
        db_asset,
        require_snapshot=require_snapshot,
        require_source=require_source,
    ):
        return db_asset
    return preview_asset or db_asset

def _prepare_uploaded_document(target: Path) -> PreparedDocument:
    try:
        return prepare_document_for_ingestion(target)
    except DocumentIngestionRejected as exc:
        target.unlink(missing_ok=True)
        raise ApplicationError(status_code=422, detail=exc.safe_detail()) from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise

def _inspect_uploaded_document(target: Path):
    try:
        return inspect_document_for_ingestion(target)
    except DocumentIngestionRejected as exc:
        target.unlink(missing_ok=True)
        raise ApplicationError(status_code=422, detail=exc.safe_detail()) from exc
    except Exception:
        target.unlink(missing_ok=True)
        raise

async def _store_uploaded_document(file: UploadInput, target: Path) -> int:
    """Stream one upload to a private part file with a hard byte budget."""
    from app.application.runtime import (
        MAX_DOCUMENT_UPLOAD_BYTES,
        UPLOAD_READ_CHUNK_BYTES,
    )


    target.parent.mkdir(parents=True, exist_ok=True)
    part_path = target.with_name(f"{target.name}.part")
    total = 0
    try:
        with part_path.open("xb") as handle:
            while chunk := await file.read(UPLOAD_READ_CHUNK_BYTES):
                total += len(chunk)
                if total > MAX_DOCUMENT_UPLOAD_BYTES:
                    raise ApplicationError(
                        status_code=413,
                        detail={
                            "code": "upload_too_large",
                            "message": "The document exceeds the configured upload limit.",
                            "max_bytes": MAX_DOCUMENT_UPLOAD_BYTES,
                        },
                    )
                handle.write(chunk)
        part_path.replace(target)
        return total
    except BaseException:
        part_path.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        raise

def _safe_upload_storage_name(file_name: str) -> str:
    """Sanitize a client name without changing its declared extension."""
    from app.application.requirements import _safe_name


    declared_extension = Path(file_name).suffix.lower()
    safe_name = _safe_name(file_name)
    if not declared_extension or Path(safe_name).suffix.lower() != declared_extension:
        raise ApplicationError(
            status_code=422,
            detail={
                "code": "invalid_declared_extension",
                "message": (
                    "The upload filename must contain an extension that remains exact "
                    "after safe storage-name normalization."
                ),
            },
        )
    return safe_name

def _upload_staging_dir(kind: str) -> Path:
    from app.application.runtime import KNOWLEDGE_DIR, RUNTIME_SETTINGS, UPLOADS_DIR

    if RUNTIME_SETTINGS.database_backend is DatabaseBackend.SQLITE_TEST:
        return UPLOADS_DIR if kind == "sor" else KNOWLEDGE_DIR
    return RUNTIME_SETTINGS.scratch_root / "ingest" / kind

def _publish_staged_object(
    *,
    target: Path,
    storage_key: str,
    expected_sha256: str,
    media_type: str,
) -> bool:
    if RUNTIME_STATE.container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    existing = RUNTIME_STATE.container.object_store.stat(storage_key)
    if existing is not None:
        if (
            existing.sha256 != expected_sha256
            or int(existing.size) != target.stat().st_size
        ):
            raise RuntimeError("storage_key is already bound to different bytes.")
        return False
    with target.open("rb") as source:
        RUNTIME_STATE.container.object_store.put(
            key=storage_key,
            source=source,
            expected_sha256=expected_sha256,
            media_type=media_type,
        )
    return True

def _delete_runtime_object_if_unreferenced(record: dict[str, Any] | None) -> bool:
    from app.application.runtime import RUNTIME_SETTINGS

    if (
        not record
        or RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
        or RUNTIME_STATE.container is None
    ):
        return False
    storage_key = str(record.get("storage_key") or "").strip()
    return _delete_runtime_object_key_if_unreferenced(storage_key)

def _delete_runtime_object_key_if_unreferenced(storage_key: str) -> bool:
    from app.application.runtime import RUNTIME_SETTINGS

    if (
        RUNTIME_SETTINGS.database_backend is not DatabaseBackend.POSTGRESQL
        or RUNTIME_STATE.container is None
    ):
        return False
    normalized = str(storage_key or "").strip()
    if not normalized or not db.release_stored_object_if_unreferenced(normalized):
        return False
    return RUNTIME_STATE.container.object_store.delete(normalized)

def _put_runtime_json(*, storage_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    if RUNTIME_STATE.container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    existing = RUNTIME_STATE.container.object_store.stat(storage_key)
    created = existing is None
    if existing is None:
        existing = RUNTIME_STATE.container.object_store.put(
            key=storage_key,
            source=io.BytesIO(encoded),
            expected_sha256=sha256,
            media_type="application/json",
        )
    if existing.sha256 != sha256 or int(existing.size) != len(encoded):
        raise RuntimeError("Runtime artifact key is bound to different bytes.")
    try:
        db.register_stored_object(
            storage_key=storage_key,
            sha256=sha256,
            size_bytes=len(encoded),
            media_type="application/json",
            object_kind="runtime-extraction-artifact",
        )
    except BaseException:
        if created:
            RUNTIME_STATE.container.object_store.delete(storage_key)
        raise
    return {
        "storage_key": storage_key,
        "sha256": sha256,
        "size": len(encoded),
        "media_type": "application/json",
    }

def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()

def _object_key_for_upload(
    *,
    kind: str,
    project_id: str,
    source_sha256: str,
    file_name: str,
) -> str:
    safe_name = _safe_upload_storage_name(file_name)
    return f"{kind}/{project_id}/{source_sha256}/{safe_name}"

async def upload_sor_document(project_id: str, file: UploadInput = upload_required()) -> dict:
    from app.application.projects import _require_project
    from app.application.runtime import RUNTIME_SETTINGS, _public_storage_record

    _require_project(project_id)
    file_name = file.filename or f"sor_{uuid.uuid4().hex}.txt"
    target = _upload_staging_dir("sor") / (
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
            kind="sor-documents",
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
        document = db.add_sor_document(
            project_id=project_id,
            file_name=file_name,
            file_type=parsed_file_type,
            file_path=storage_identity,
            extracted_text=parsed_text,
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
    return _public_storage_record(document)

def list_sor_documents(project_id: str) -> list[dict]:
    from app.application.projects import _require_project
    from app.application.runtime import _public_storage_record

    _require_project(project_id)
    return [_public_storage_record(item) for item in db.list_sor_documents(project_id)]

def delete_sor_document(project_id: str, document_id: str) -> dict:
    from app.application.extraction.document_jobs import (
        _LLM_EXTRACTION_JOB_LOCK,
        _LLM_EXTRACTION_JOBS,
        _purge_llm_extraction_job_checkpoints,
    )
    from app.application.extraction.review_support import (
        _llm_requirement_review_repository,
    )
    from app.application.projects import _require_project
    from app.application.requirements import _purge_table_snapshot_root
    from app.application.runtime import STORAGE_DIR

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )
    cancel_preview_refresh(document_id)
    cancel_extraction_shadow_runs(document_id)
    with _LLM_EXTRACTION_JOB_LOCK:
        _LLM_EXTRACTION_JOBS.pop(document_id, None)
        # Visual snapshots contain recognized customer text. Their purge is a
        # deletion precondition: if storage cleanup fails, keep the DB binding
        # so the same document can be retried instead of orphaning private data.
        VisualTextSnapshotCache(STORAGE_DIR).purge_document(
            project_id=project_id,
            document_id=document_id,
        )
        _llm_requirement_review_repository().purge_document(
            project_id=project_id,
            document_id=document_id,
        )
        deleted = db.delete_sor_document(document_id)
        if not deleted:
            raise ApplicationError(status_code=404, detail="SOR document not found.")
        released_source_asset_keys = tuple(
            str(item)
            for item in deleted.pop(
                "_released_source_asset_storage_keys",
                (),
            )
            if str(item or "").strip()
        )
        if released_source_asset_keys:
            from app.application.source_asset_publication import (
                release_source_asset_candidates,
            )
            from app.application.source_runtime import get_source_object_store

            release_source_asset_candidates(
                released_source_asset_keys,
                object_store=get_source_object_store(),
            )
        released_result_artifact_keys = tuple(
            str(item)
            for item in deleted.pop(
                "_released_extraction_result_artifact_storage_keys",
                (),
            )
            if str(item or "").strip()
        )
        if released_result_artifact_keys:
            from app.application.extraction.source_asset_objects import (
                delete_unreferenced_extraction_result_artifacts,
            )

            delete_unreferenced_extraction_result_artifacts(
                released_result_artifact_keys
            )
        _delete_runtime_object_if_unreferenced(deleted)
        purge_preview_artifacts(document_id, recreate_snapshot_root=False)
        purge_extraction_shadow_runs(document_id)
        LlmExtractionResultCache(STORAGE_DIR).purge_document(
            project_id=project_id,
            document_id=document_id,
        )
        _purge_llm_extraction_job_checkpoints(
            project_id=project_id,
            document_id=document_id,
        )
        _purge_table_snapshot_root(document_id)
    return {"ok": True, "file_name": deleted["file_name"]}

def get_document_table_asset(
    project_id: str,
    document_id: str,
    table_id: str,
    requirement_set_id: str = "",
) -> dict:
    from app.application.projects import _require_project

    _require_project(project_id)
    asset = _resolve_document_table_asset(
        project_id, document_id, table_id, requirement_set_id=requirement_set_id
    )
    if asset is None:
        raise ApplicationError(status_code=404, detail="Table asset not found.")
    return prepare_table_asset_urls(
        asset, project_id=project_id, document_id=document_id
    )

def get_document_table_asset_snapshot(
    project_id: str,
    document_id: str,
    table_id: str,
    requirement_set_id: str = "",
) -> FileArtifact:
    from app.application.projects import _require_project
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    _require_project(project_id)
    asset = _resolve_document_table_asset(
        project_id,
        document_id,
        table_id,
        requirement_set_id=requirement_set_id,
        require_snapshot=True,
    )
    if asset is None:
        raise ApplicationError(status_code=404, detail="Table asset not found.")
    snapshot_path = Path(str(asset.get("snapshot_path") or "").strip())
    if not snapshot_path.exists():
        raise ApplicationError(status_code=404, detail="Table snapshot not found.")
    return FileArtifact(
        snapshot_path,
        media_type="image/png",
        filename=f"{table_id}.png",
        headers=_ASSET_RESPONSE_HEADERS,
    )

def get_document_table_asset_source_snapshot(
    project_id: str,
    document_id: str,
    table_id: str,
    requirement_set_id: str = "",
) -> FileArtifact:
    from app.application.projects import _require_project
    from app.application.runtime import _ASSET_RESPONSE_HEADERS

    _require_project(project_id)
    asset = _resolve_document_table_asset(
        project_id,
        document_id,
        table_id,
        requirement_set_id=requirement_set_id,
        require_source=True,
    )
    if asset is None:
        raise ApplicationError(status_code=404, detail="Table asset not found.")
    snapshot_path = Path(str(asset.get("source_snapshot_path") or "").strip())
    if not snapshot_path.exists():
        raise ApplicationError(status_code=404, detail="Table source snapshot not found.")
    return FileArtifact(
        snapshot_path,
        media_type="image/png",
        filename=f"{table_id}_source.png",
        headers=_ASSET_RESPONSE_HEADERS,
    )

def get_sor_document_file(project_id: str, document_id: str) -> FileArtifact:
    from app.application.projects import _require_project
    from app.application.runtime import _runtime_record_path

    _require_project(project_id)
    document = db.get_sor_document(document_id)
    if not document or document["project_id"] != project_id:
        raise ApplicationError(
            status_code=404, detail="SOR document not found for this project."
        )

    path = _runtime_record_path(document)
    if not path.exists():
        raise ApplicationError(status_code=404, detail="SOR source file not found.")

    file_name = str(document.get("file_name") or path.name or "sor_source")
    suffix = path.suffix.lower()
    media_type = "application/pdf" if suffix == ".pdf" else "application/octet-stream"
    if media_type == "application/pdf":
        inline_name = quote(file_name)
        return FileArtifact(
            path,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename*=utf-8''{inline_name}"},
        )
    return FileArtifact(path, filename=file_name, media_type=media_type)
