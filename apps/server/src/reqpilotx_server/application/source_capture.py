"""Neutral, capture-only access to immutable document source facts."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.docx_source_ir_adapter import DocxSourceIRAdapter
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from reqpilotx_server.adapters.documents.xlsb_source_ir_adapter import XlsbSourceIRAdapter
from reqpilotx_server.adapters.documents.xlsx_source_ir_adapter import XlsxSourceIRAdapter
from reqpilotx_server.adapters.persistence.sql_source_capture import SqlSourceCaptureRepository
from reqpilotx_server.adapters.persistence.sql_reading_catalog import SqlReadingCatalogRepository
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.build_source_ir import (
    SourceIRBuildRejected,
    build_source_ir,
)
from reqpilotx_server.application.source_asset_publication import SourceAssetPublicationAttempt
from reqpilotx_server.application.source_runtime import (
    get_source_object_store,
    resolve_source_record_path,
)
from reqpilotx_server.ports.reading_catalog_repository import (
    ReadingSourceIdentityRepositoryPort,
)
from reqpilotx_server.ports.source_capture_repository import (
    SourceCaptureRecord,
    SourceCaptureRepositoryPort,
)
from reqpilotx_server.ports.object_store import ObjectStore


def capture_current_document_source(
    *,
    project_id: str,
    sor_document_id: str,
    expected_source_sha256: str | None = None,
    repository: SourceCaptureRepositoryPort | None = None,
    object_store: ObjectStore | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SourceCaptureRecord:
    """Return or append one current-version SourceIR capture.

    The use case deliberately stops at SourceIR.  It never starts extraction,
    builds Structure, invokes semantic models, or writes requirement data.
    """

    document = require_current_source_document(
        project_id=project_id,
        sor_document_id=sor_document_id,
        expected_source_sha256=expected_source_sha256,
    )
    source_sha256 = str(document["source_sha256"])
    store = repository or SqlSourceCaptureRepository()
    existing = store.get_capture(
        project_id=project_id,
        sor_document_id=sor_document_id,
        source_sha256=source_sha256,
    )
    if existing is not None:
        return existing
    created_at = _aware_now(clock)
    asset_attempt = SourceAssetPublicationAttempt(
        project_id=project_id,
        sor_document_id=sor_document_id,
        source_sha256=source_sha256,
        object_store=object_store or get_source_object_store(),
        created_at_utc=created_at,
    )
    try:
        source_path = resolve_source_record_path(document)
        publisher = asset_attempt.publish
        source_ir = build_source_ir(
            source_path,
            adapters=(
                PdfSourceIRAdapter(source_asset_publisher=publisher),
                DocxSourceIRAdapter(source_asset_publisher=publisher),
                XlsxSourceIRAdapter(source_asset_publisher=publisher),
                XlsbSourceIRAdapter(source_asset_publisher=publisher),
            ),
            clock=lambda: created_at,
        )
    except SourceIRBuildRejected as exc:
        asset_attempt.abort()
        raise ApplicationError(
            status_code=409,
            code="reading_source_capture_rejected",
            detail={
                "reason_code": "reading_source_capture_rejected",
                "message": "The current document could not produce source-bound reading facts.",
                "capture_code": exc.code.value,
            },
        ) from exc
    except (OSError, RuntimeError, ValueError) as exc:
        asset_attempt.abort()
        raise ApplicationError(
            status_code=409,
            code="reading_source_unavailable",
            detail={
                "reason_code": "reading_source_unavailable",
                "message": "The current document source is unavailable for reading preparation.",
            },
        ) from exc
    if source_ir.source_sha256 != source_sha256:
        asset_attempt.abort()
        raise ApplicationError(
            status_code=409,
            code="reading_source_identity_changed",
            detail={
                "reason_code": "reading_source_identity_changed",
                "message": "The source bytes changed while reading preparation was running.",
            },
        )
    try:
        asset_attempt.verify(source_ir.assets)
        require_current_source_document(
            project_id=project_id,
            sor_document_id=sor_document_id,
            expected_source_sha256=source_sha256,
        )
        capture_id = _stable_id("source-capture", source_ir.source_ir_id)
        stored = store.append_capture(
            SourceCaptureRecord(
                capture_id=capture_id,
                project_id=project_id,
                sor_document_id=sor_document_id,
                source_sha256=source_sha256,
                source_ir=source_ir,
                created_at_utc=created_at,
            ),
            asset_publication_id=asset_attempt.identity.publication_id,
        )
        asset_attempt.seal()
        return stored
    except Exception:
        asset_attempt.abort()
        raise


def require_current_source_document(
    *,
    project_id: str,
    sor_document_id: str,
    expected_source_sha256: str | None = None,
    identity_repository: ReadingSourceIdentityRepositoryPort | None = None,
) -> dict:
    store = identity_repository or SqlReadingCatalogRepository()
    document = store.get_source_document(
        project_id=project_id,
        sor_document_id=sor_document_id,
    )
    if document is None:
        raise ApplicationError(
            status_code=404,
            code="reading_document_not_found",
            detail="SOR document not found for this project.",
        )
    source_sha256 = str(document.source_sha256 or "").strip().lower()
    if len(source_sha256) != 64 or any(
        character not in "0123456789abcdef" for character in source_sha256
    ):
        raise ApplicationError(
            status_code=409,
            code="reading_source_identity_missing",
            detail={
                "reason_code": "reading_source_identity_missing",
                "message": "The document has no verified source identity.",
            },
        )
    expected = str(expected_source_sha256 or "").strip().lower()
    if expected and expected != source_sha256:
        raise ApplicationError(
            status_code=409,
            code="reading_source_identity_changed",
            detail={
                "reason_code": "reading_source_identity_changed",
                "message": "The requested reading source version is no longer current.",
                "current_source_sha256": source_sha256,
            },
        )
    locator_key = "storage_key" if db.is_postgresql_runtime() else "file_path"
    return {
        "id": document.id,
        "project_id": document.project_id,
        "source_sha256": source_sha256,
        "source_size": document.source_size,
        locator_key: document.source_locator,
    }


def _aware_now(clock: Callable[[], datetime] | None) -> datetime:
    value = (clock or (lambda: datetime.now(timezone.utc)))()
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source capture clock must return a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"{prefix}\0{value}".encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


__all__ = ["capture_current_document_source", "require_current_source_document"]
