from __future__ import annotations

import hashlib
import io
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from reqpilotx_server.adapters.documents.pdf_source_crop_renderer import (
    inspect_pdf_page_geometry,
)
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
    PageStagingAuthorityLostError,
)
from reqpilotx_server.application.errors import ApplicationError
from reqpilotx_server.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    _serialize_finding,
    build_document_view,
    build_page_comparison_view,
)
from reqpilotx_server.application.extraction.geometry_evidence import (
    PDF_PAGE_GEOMETRY_PREVIEW_KEY,
    build_pdf_page_geometry_preview,
    persist_pdf_page_geometry_preview,
    public_pdf_page_geometry_descriptor,
)
from reqpilotx_server.application.extraction.structure_snapshot import StructureSnapshot
from reqpilotx_server.application.transport import BinaryArtifact, ResponseSink
from reqpilotx_server.domain.extraction.pdf_page_furniture import (
    PdfPageFurnitureKind,
    pdf_image_furniture_kind_from_diagnostic_codes,
)
from reqpilotx_server.domain.extraction.pdf_page_raster import (
    collapse_pdf_page_raster_blocks,
    is_proven_pdf_page_raster_fact,
)
from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
)


SOURCE_PAGE_PREVIEW_KEY = "_source_page_preview"
SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION = "source-page-fact-snapshot/v1"
DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION = (
    "document-extraction-page-staging/v1"
)
DOCUMENT_EXTRACTION_PAGE_STAGING_SUMMARY_SCHEMA_VERSION = (
    "document-extraction-page-staging-summary/v1"
)
_SOURCE_PAGE_PREVIEW_SCHEMA_VERSION = "source-page-fact-preview/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MAX_PAGE_ARTIFACT_BYTES = 64 * 1024 * 1024
_STAGING_NATIVE_BLOCK_TYPES = {
    "heading": "TEXT",
    "paragraph": "TEXT",
    "list": "TEXT",
    "list_item": "TEXT",
    "note": "TEXT",
    "toc_entry": "TEXT",
    "table": "TABLE",
    "image": "FIGURE",
    "figure": "FIGURE",
    "header": "HEADER",
    "footer": "FOOTER",
    "watermark": "WATERMARK",
}


def build_source_page_preview(
    source_path: Path,
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
) -> dict[str, Any] | None:
    """Build page-addressable facts from the SourceIR already used by extraction.

    This presenter never reparses source content.  It only reads PDF page bounds
    so SourceIR coordinates can be projected into the existing overlay contract.
    Non-PDF inputs keep their native SourceIR contract and return no page preview.
    """

    if not _is_pdf_source(source_ir, source_path=source_path):
        return None
    page_geometries = inspect_pdf_page_geometry(
        source_path,
        expected_source_sha256=source_ir.source_sha256,
    )
    page_bounds = tuple(page.bounds for page in page_geometries)
    document_view = build_document_view(source_ir, structure)
    page_comparison = build_page_comparison_view(
        source_ir,
        structure,
        page_bounds=page_bounds,
    )
    source_catalog = document_view["source_catalog"]
    findings = [
        _serialize_finding(finding, source_catalog)
        for finding in structure.findings
    ]
    diagnostics = [
        {
            "code": diagnostic.code,
            "severity": diagnostic.severity.value,
            "message": diagnostic.message,
            "retryable": diagnostic.retryable,
            "location": (
                diagnostic.native_locator.model_dump(mode="json")
                if diagnostic.native_locator is not None
                else None
            ),
        }
        for diagnostic in source_ir.diagnostics
    ]
    structure_snapshot_id = str(
        getattr(structure, "effective_snapshot_id", "")
        or getattr(structure, "structure_snapshot_id", "")
    )
    return {
        "schema_version": _SOURCE_PAGE_PREVIEW_SCHEMA_VERSION,
        "mode": "FORMAL_SOURCE_FACTS",
        "status": "READY",
        "serving_impact": "FORMAL_READ_MODEL",
        "source_identity": {
            "status": "MATCHED",
            "match": True,
            "source_sha256": source_ir.source_sha256,
            "source_ir_id": source_ir.source_ir_id,
            "adapter_id": source_ir.adapter_id,
            "adapter_version": source_ir.adapter_version,
        },
        "structure_identity": {
            "snapshot_id": structure_snapshot_id,
            "resolver_id": str(getattr(structure, "resolver_id", "") or ""),
            "resolver_version": str(
                getattr(structure, "resolver_version", "") or ""
            ),
        },
        "document": {
            "format": "pdf",
            "page_count": len(page_bounds),
            "max_page_number": len(page_bounds),
        },
        "presentation": {
            "page_comparison_schema": PAGE_COMPARISON_SCHEMA_VERSION,
            "response_collection": "source_page_facts",
        },
        "page_comparison": page_comparison,
        "tables": document_view["tables"],
        "findings": findings,
        "diagnostics": diagnostics,
        PDF_PAGE_GEOMETRY_PREVIEW_KEY: build_pdf_page_geometry_preview(
            source_ir,
            page_geometries=page_geometries,
        ),
    }


def persist_source_page_preview(
    preview: dict[str, Any] | None,
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any] | None:
    """Persist immutable per-page JSON and return a small result descriptor."""

    if preview is None:
        return None
    from reqpilotx_server.application.ingestion import _put_runtime_json
    from reqpilotx_server.services.extraction_shadow_runs import (
        SHADOW_PAGE_SCHEMA_VERSION,
        build_extraction_shadow_artifacts,
    )

    persisted_preview = dict(preview)
    geometry_preview = persisted_preview.pop(PDF_PAGE_GEOMETRY_PREVIEW_KEY, None)
    source_identity = _record(persisted_preview.get("source_identity"))
    structure_identity = _record(persisted_preview.get("structure_identity"))
    source_sha256 = str(source_identity.get("source_sha256") or "").lower()
    source_ir_id = str(source_identity.get("source_ir_id") or "")
    if not _SHA256_PATTERN.fullmatch(source_sha256) or not source_ir_id:
        raise ValueError("source page preview has invalid source identity")
    snapshot_id = _source_page_snapshot_id(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        source_ir_id=source_ir_id,
        structure_snapshot_id=str(structure_identity.get("snapshot_id") or ""),
        adapter_version=str(source_identity.get("adapter_version") or ""),
        page_comparison_schema=str(
            _record(persisted_preview.get("page_comparison")).get("schema_version")
            or ""
        ),
        geometry_schema_version=str(
            _record(geometry_preview).get("schema_version") or ""
        ),
    )
    metadata, pages, details = build_extraction_shadow_artifacts(
        persisted_preview,
        run_id=snapshot_id,
        project_id=project_id,
        document_id=document_id,
    )
    prefix = f"runtime/source-page-facts/{source_sha256}/{snapshot_id}"
    page_artifacts = {
        str(page_number): _put_runtime_json(
            storage_key=f"{prefix}/pages/{page_number}.json",
            payload=page,
        )
        for page_number, page in sorted(pages.items())
    }
    details_artifact = _put_runtime_json(
        storage_key=f"{prefix}/details.json",
        payload=details,
    )
    geometry_descriptor = persist_pdf_page_geometry_preview(
        geometry_preview,
        project_id=project_id,
        document_id=document_id,
    )
    manifest_payload = {
        "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "source_ir_id": source_ir_id,
        "structure_snapshot_id": str(
            structure_identity.get("snapshot_id") or ""
        ),
        "page_schema_version": SHADOW_PAGE_SCHEMA_VERSION,
        "page_count": len(page_artifacts),
        "metadata": metadata,
        "pages": page_artifacts,
        "details": details_artifact,
    }
    if geometry_descriptor is not None:
        manifest_payload["geometry_index"] = geometry_descriptor
    manifest_artifact = _put_runtime_json(
        storage_key=f"{prefix}/manifest.json",
        payload=manifest_payload,
    )
    return {
        **manifest_payload,
        "state": "READY",
        "manifest": manifest_artifact,
    }


def build_document_extraction_staged_page(
    candidate: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    processing_plan_sha256: str,
    result_runtime_sha256: str,
    completed_at_utc: str | None = None,
) -> dict[str, Any]:
    """Bind an adapter page slice to one live extraction attempt.

    This is a read-only diagnostic artifact. It deliberately contains no
    Structure identity and can never satisfy the formal SourceIR/Structure
    endpoints.
    """

    if not isinstance(candidate, dict) or candidate.get("schema_version") != (
        "pdf-source-page-staging-candidate/v1"
    ):
        raise ValueError("page staging requires a PDF source-page candidate")
    normalized_source_sha256 = str(source_sha256 or "").strip().lower()
    if (
        not _SHA256_PATTERN.fullmatch(normalized_source_sha256)
    ):
        raise ValueError("page staging source identity is invalid")
    if str(candidate.get("source_sha256") or "") != normalized_source_sha256:
        raise PageStagingAuthorityLostError(
            "Page staging candidate source identity does not match the delivery."
        )
    if not _SHA256_PATTERN.fullmatch(str(processing_plan_sha256 or "")):
        raise ValueError("page staging processing plan identity is invalid")
    if not _SHA256_PATTERN.fullmatch(str(result_runtime_sha256 or "")):
        raise ValueError("page staging result runtime identity is invalid")
    if not str(project_id or "").strip() or not str(document_id or "").strip():
        raise ValueError("page staging requires project and document identity")
    if not str(job_id or "").strip() or int(attempt_generation) < 1:
        raise ValueError("page staging requires a positive job generation")
    page_number = _positive_int(candidate.get("page_number"), "page_number")
    total_pages = _positive_int(candidate.get("total_pages"), "total_pages")
    if page_number > total_pages:
        raise ValueError("page staging page_number exceeds total_pages")
    adapter = _record(candidate.get("adapter"))
    adapter_id = str(adapter.get("id") or "").strip()
    adapter_version = str(adapter.get("version") or "").strip()
    if not adapter_id or not adapter_version:
        raise ValueError("page staging adapter identity is incomplete")
    completed_at = str(completed_at_utc or "").strip() or datetime.now(
        timezone.utc
    ).isoformat()
    try:
        parsed_completed_at = datetime.fromisoformat(completed_at)
    except ValueError as exc:
        raise ValueError("page staging completed_at_utc is invalid") from exc
    if parsed_completed_at.tzinfo is None:
        raise ValueError("page staging completed_at_utc must be timezone-aware")

    bounds = _normalized_page_bounds(candidate.get("bounds"))
    diagnostics = _staging_page_diagnostics(
        candidate.get("diagnostics"),
        page_number=page_number,
    )
    native_blocks, native_tables = _staging_native_blocks(
        candidate.get("nodes"),
        assets=candidate.get("assets"),
        source_sha256=normalized_source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        page_number=page_number,
        page_bounds=bounds,
        page_diagnostic_codes={
            str(diagnostic.get("code") or "") for diagnostic in diagnostics
        },
    )
    remote_blocks, remote_tables = _staging_remote_blocks(
        candidate.get("visual_text_snapshots"),
        source_sha256=normalized_source_sha256,
        adapter_id=adapter_id,
        adapter_version=adapter_version,
        page_number=page_number,
    )
    blocks = collapse_pdf_page_raster_blocks(
        [*native_blocks, *remote_blocks],
        page_bbox=bounds,
    )
    by_type = Counter(str(block.get("type") or "UNKNOWN") for block in blocks)
    by_channel = Counter(
        str(_record(block.get("provenance")).get("channel") or "UNKNOWN")
        for block in blocks
    )
    physical_tables = [*native_tables, *remote_tables]
    page = {
        "page_number": page_number,
        "bounds": bounds,
        "blocks": blocks,
        "issues": [],
        "diagnostics": diagnostics,
        "physical_tables": physical_tables,
        "counts": {
            "blocks": len(blocks),
            "content_blocks": sum(
                block.get("type") != "PAGE_RASTER" for block in blocks
            ),
            "support_blocks": sum(
                block.get("type") == "PAGE_RASTER" for block in blocks
            ),
            "locatable": sum(block.get("locatable") is True for block in blocks),
            "unlocatable": sum(block.get("locatable") is not True for block in blocks),
            "diagnostics": len(diagnostics),
            "physical_tables": len(physical_tables),
            "by_type": dict(sorted(by_type.items())),
            "by_channel": dict(sorted(by_channel.items())),
        },
    }
    return {
        "schema_version": DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION,
        "formal": False,
        "editable": False,
        "serving_impact": "NONE",
        "state": "STAGED_READ_ONLY",
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": normalized_source_sha256,
        "job_id": job_id,
        "attempt_generation": int(attempt_generation),
        "processing_plan_sha256": processing_plan_sha256,
        "result_runtime_sha256": result_runtime_sha256,
        "adapter": {"id": adapter_id, "version": adapter_version},
        "page_number": page_number,
        "total_pages": total_pages,
        "completed_at_utc": parsed_completed_at.isoformat(),
        "structure_state": "NOT_BUILT",
        "page_comparison": {
            "schema_version": PAGE_COMPARISON_SCHEMA_VERSION,
            "coordinate_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "interaction": "READ_ONLY_STAGED_PAGE",
            "downstream_identity": "NONE",
            "source_only": True,
            "pages": [page],
            "counts": {
                "pages": 1,
                "blocks": len(blocks),
                "diagnostics": len(diagnostics),
                "physical_tables": len(physical_tables),
            },
        },
        "tables": physical_tables,
        "diagnostics": diagnostics,
    }


def persist_document_extraction_staged_page(
    candidate: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
    attempt_generation: int,
    worker_id: str,
    processing_plan_sha256: str,
    result_runtime_sha256: str,
) -> dict[str, Any]:
    """Persist and confirm one exact live-attempt page artifact."""

    from reqpilotx_server import db
    from reqpilotx_server.application.runtime import RUNTIME_STATE

    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    payload = build_document_extraction_staged_page(
        candidate,
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        job_id=job_id,
        attempt_generation=attempt_generation,
        processing_plan_sha256=processing_plan_sha256,
        result_runtime_sha256=result_runtime_sha256,
    )
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if not encoded or len(encoded) > _MAX_PAGE_ARTIFACT_BYTES:
        raise RuntimeError("Document extraction staged page exceeds the size limit.")
    digest = hashlib.sha256(encoded).hexdigest()
    page_number = int(payload["page_number"])
    storage_key = (
        "runtime/extraction-page-staging/"
        f"{_artifact_token(project_id)}/{_artifact_token(document_id)}/"
        f"{source_sha256}/{_artifact_token(job_id)}/{attempt_generation}/"
        f"pages/{page_number}-{digest}.json"
    )
    try:
        db.stage_document_extraction_page_reference(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            worker_id=worker_id,
            processing_plan_sha256=processing_plan_sha256,
            result_runtime_sha256=result_runtime_sha256,
            adapter_id=str(payload["adapter"]["id"]),
            adapter_version=str(payload["adapter"]["version"]),
            page_number=page_number,
            total_pages=int(payload["total_pages"]),
            completed_at_utc=str(payload["completed_at_utc"]),
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(encoded),
            media_type="application/json",
        )
    except db.DocumentExtractionPageStagingFenceError as exc:
        raise PageStagingAuthorityLostError(str(exc)) from exc
    try:
        stored = container.object_store.stat(storage_key)
        if stored is None:
            stored = container.object_store.put(
                key=storage_key,
                source=io.BytesIO(encoded),
                expected_sha256=digest,
                media_type="application/json",
            )
        if (
            stored.key != storage_key
            or stored.sha256 != digest
            or int(stored.size) != len(encoded)
        ):
            raise RuntimeError("Staged page object identity changed during put.")
        confirmed = db.confirm_document_extraction_page_reference(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
            attempt_generation=attempt_generation,
            worker_id=worker_id,
            processing_plan_sha256=processing_plan_sha256,
            result_runtime_sha256=result_runtime_sha256,
            page_number=page_number,
            storage_key=storage_key,
            artifact_sha256=digest,
            size_bytes=len(encoded),
        )
    except db.DocumentExtractionPageStagingFenceError as exc:
        _abort_staged_page_best_effort(
            job_id=job_id,
            attempt_generation=attempt_generation,
            page_number=page_number,
        )
        raise PageStagingAuthorityLostError(str(exc)) from exc
    except Exception:
        _abort_staged_page_best_effort(
            job_id=job_id,
            attempt_generation=attempt_generation,
            page_number=page_number,
        )
        raise
    if not confirmed:
        _abort_staged_page_best_effort(
            job_id=job_id,
            attempt_generation=attempt_generation,
            page_number=page_number,
        )
        raise RuntimeError("Staged page publication lost its job lease fence.")
    return {
        "schema_version": DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION,
        "state": "STAGED_READ_ONLY",
        "page_number": page_number,
        "total_pages": int(payload["total_pages"]),
        "storage_key": storage_key,
        "sha256": digest,
        "size": len(encoded),
    }


def get_document_extraction_staged_source_page(
    project_id: str,
    document_id: str,
    job_id: str,
    generation: int,
    page_number: int,
) -> ResponseSink:
    """Serve only a confirmed page from the exact live attempt in the URL."""

    from reqpilotx_server import db
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 1
        or isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ApplicationError(
            status_code=422,
            code="invalid_extraction_page_staging_identity",
            detail={
                "code": "invalid_extraction_page_staging_identity",
                "message": "Attempt generation and page number must be positive integers.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    descriptor = db.get_document_extraction_page_reference(
        project_id=project_id,
        document_id=document_id,
        source_sha256=str(document.get("source_sha256") or ""),
        job_id=job_id,
        attempt_generation=generation,
        page_number=page_number,
    )
    if not descriptor:
        raise ApplicationError(
            status_code=404,
            code="extraction_page_staging_not_found",
            detail={
                "code": "extraction_page_staging_not_found",
                "message": "The exact live extraction attempt has no confirmed staged page.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    payload_bytes = _read_verified_json_artifact(
        {
            "storage_key": descriptor["storage_key"],
            "sha256": descriptor["artifact_sha256"],
            "size": descriptor["size_bytes"],
        },
        not_found_message="The staged page artifact is missing.",
    )
    return BinaryArtifact(
        content=payload_bytes,
        media_type="application/json",
        headers={
            **_ASSET_RESPONSE_HEADERS,
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{descriptor["artifact_sha256"]}"',
        },
    )


def public_document_extraction_page_staging(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    job_id: str,
) -> dict[str, Any]:
    """Return a content-free summary for the current live attempt."""

    from reqpilotx_server import db

    try:
        summary = db.get_document_extraction_page_staging_summary(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            job_id=job_id,
        )
    except Exception:
        return {
            "schema_version": (
                DOCUMENT_EXTRACTION_PAGE_STAGING_SUMMARY_SCHEMA_VERSION
            ),
            "state": "DEGRADED",
            "error_code": "page_staging_status_unavailable",
            "job_id": job_id,
            "attempt_generation": 0,
            "total_pages": 0,
            "ready_page_count": 0,
            "ready_page_ranges": [],
            "page_url_template": "",
        }
    if not summary:
        return {
            "schema_version": (
                DOCUMENT_EXTRACTION_PAGE_STAGING_SUMMARY_SCHEMA_VERSION
            ),
            "state": "UNAVAILABLE",
            "job_id": job_id,
            "attempt_generation": 0,
            "total_pages": 0,
            "ready_page_count": 0,
            "ready_page_ranges": [],
            "page_url_template": "",
        }
    generation = int(summary["attempt_generation"])
    ready_page_count = int(summary["ready_page_count"])
    return {
        "schema_version": DOCUMENT_EXTRACTION_PAGE_STAGING_SUMMARY_SCHEMA_VERSION,
        "state": (
            "STAGED_READ_ONLY" if ready_page_count else "WAITING_FOR_FIRST_PAGE"
        ),
        "job_id": job_id,
        "attempt_generation": generation,
        "source_sha256": source_sha256,
        "processing_plan_sha256": str(summary["processing_plan_sha256"]),
        "result_runtime_sha256": str(summary["result_runtime_sha256"]),
        "total_pages": int(summary["total_pages"]),
        "ready_page_count": ready_page_count,
        "ready_page_ranges": _page_ranges(summary["page_numbers"]),
        "page_url_template": (
            f"/api/projects/{project_id}/sor-documents/{document_id}/extraction/"
            f"attempts/{job_id}/generations/{generation}/source-pages/"
            "{page_number}"
        ),
    }


def public_source_page_snapshot(
    payload: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    snapshot = _validated_snapshot_descriptor(
        payload.get("source_page_snapshot"),
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=str(payload.get("source_sha256") or ""),
    )
    if snapshot is None:
        return {
            "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
            "state": "UNAVAILABLE",
            "page_count": 0,
        }
    return {
        "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
        "state": "READY",
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "source_sha256": str(snapshot.get("source_sha256") or ""),
        "source_ir_id": str(snapshot.get("source_ir_id") or ""),
        "page_schema_version": str(snapshot.get("page_schema_version") or ""),
        "page_count": int(snapshot.get("page_count") or 0),
        "geometry_index": public_pdf_page_geometry_descriptor(
            snapshot.get("geometry_index"),
            project_id=project_id,
            document_id=document_id,
            expected_source_sha256=str(snapshot.get("source_sha256") or ""),
        ),
    }


def get_document_source_page(
    project_id: str,
    document_id: str,
    page_number: int,
) -> ResponseSink:
    """Return a formal page fact artifact from the latest extraction result."""

    _document, snapshot = _current_document_source_page_snapshot(
        project_id=project_id,
        document_id=document_id,
    )
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ApplicationError(
            status_code=422,
            code="invalid_source_page_number",
            detail={
                "code": "invalid_source_page_number",
                "message": "The source page number must be a positive integer.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    pages = _record(snapshot.get("pages"))
    descriptor = _record(pages.get(str(page_number)))
    if not descriptor:
        raise ApplicationError(
            status_code=404,
            code="source_page_not_found",
            detail={
                "code": "source_page_not_found",
                "message": "The requested source page snapshot does not exist.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    payload_bytes = _read_verified_json_artifact(
        descriptor,
        not_found_message="The requested source page artifact is missing.",
    )
    return BinaryArtifact(
        content=payload_bytes,
        media_type="application/json",
        headers={
            **_ASSET_RESPONSE_HEADERS,
            "X-Content-Type-Options": "nosniff",
        },
    )


def _current_document_source_page_snapshot(
    *,
    project_id: str,
    document_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Resolve the current verified page snapshot without starting any work."""

    from reqpilotx_server.application.extraction.document_jobs import (
        _document_extraction_result_artifact_identity,
    )
    from reqpilotx_server.application.extraction.runtime_read_models import (
        cache_document_extraction_runtime_read_model,
        get_document_extraction_runtime_read_model,
    )
    from reqpilotx_server.application.extraction.structure_workspace import (
        _structure_agent_job_payload,
    )
    from reqpilotx_server.application.extraction.review_support import _require_project_document
    from reqpilotx_server.application.runtime import _ASSET_RESPONSE_HEADERS

    document = _require_project_document(project_id, document_id)
    source_sha256 = str(document.get("source_sha256") or "")
    artifact_identity = _document_extraction_result_artifact_identity(
        project_id,
        document_id,
        source_sha256=source_sha256,
    )
    cached = get_document_extraction_runtime_read_model(
        project_id=project_id,
        document_id=document_id,
        source_sha256=source_sha256,
        artifact_identity=artifact_identity,
    )
    if cached is None or not cached.source_page_snapshot:
        _job, payload, _agent = _structure_agent_job_payload(
            project_id=project_id,
            document_id=document_id,
            document=document,
            require_final=False,
        )
        cached = cache_document_extraction_runtime_read_model(
            project_id=project_id,
            document_id=document_id,
            source_sha256=source_sha256,
            artifact_identity=artifact_identity,
            payload=payload,
        )
        raw_snapshot = payload.get("source_page_snapshot")
    else:
        raw_snapshot = cached.source_page_snapshot
    snapshot = _validated_snapshot_descriptor(
        raw_snapshot,
        project_id=project_id,
        document_id=document_id,
        expected_source_sha256=source_sha256,
    )
    if snapshot is None:
        raise ApplicationError(
            status_code=404,
            code="source_page_snapshot_unavailable",
            detail={
                "code": "source_page_snapshot_unavailable",
                "message": "The current extraction result has no formal page snapshot.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    return document, snapshot


def _validated_snapshot_descriptor(
    value: Any,
    *,
    project_id: str,
    document_id: str,
    expected_source_sha256: str,
) -> dict[str, Any] | None:
    snapshot = _record(value)
    if not snapshot:
        return None
    if (
        str(snapshot.get("schema_version") or "")
        != SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION
        or str(snapshot.get("state") or "").upper() != "READY"
        or str(snapshot.get("project_id") or "") != project_id
        or str(snapshot.get("document_id") or "") != document_id
        or str(snapshot.get("source_sha256") or "")
        != str(expected_source_sha256 or "")
        or not _SHA256_PATTERN.fullmatch(
            str(snapshot.get("snapshot_id") or "")
        )
        or int(snapshot.get("page_count") or 0) < 1
    ):
        return None
    pages = _record(snapshot.get("pages"))
    if len(pages) != int(snapshot.get("page_count") or 0):
        return None
    return snapshot


def _read_verified_json_artifact(
    descriptor: dict[str, Any],
    *,
    not_found_message: str,
) -> bytes:
    from reqpilotx_server.application.runtime import RUNTIME_STATE, _ASSET_RESPONSE_HEADERS

    container = RUNTIME_STATE.container
    if container is None:
        raise RuntimeError("Runtime object storage is not initialized.")
    storage_key = str(descriptor.get("storage_key") or "").strip()
    expected_sha256 = str(descriptor.get("sha256") or "").strip().lower()
    expected_size = int(descriptor.get("size") or 0)
    if (
        not storage_key
        or not _SHA256_PATTERN.fullmatch(expected_sha256)
        or expected_size < 1
        or expected_size > _MAX_PAGE_ARTIFACT_BYTES
    ):
        raise ApplicationError(
            status_code=409,
            code="source_page_artifact_invalid",
            detail={
                "code": "source_page_artifact_invalid",
                "message": "Source page artifact metadata is invalid.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    stored = container.object_store.stat(storage_key)
    if stored is None:
        raise ApplicationError(
            status_code=404,
            code="source_page_artifact_missing",
            detail={
                "code": "source_page_artifact_missing",
                "message": not_found_message,
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    if stored.sha256 != expected_sha256 or int(stored.size) != expected_size:
        raise ApplicationError(
            status_code=409,
            code="source_page_artifact_identity_mismatch",
            detail={
                "code": "source_page_artifact_identity_mismatch",
                "message": "Source page artifact identity does not match.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    with container.object_store.open(storage_key) as source:
        payload = source.read(expected_size + 1)
    if (
        len(payload) != expected_size
        or hashlib.sha256(payload).hexdigest() != expected_sha256
    ):
        raise ApplicationError(
            status_code=409,
            code="source_page_artifact_verification_failed",
            detail={
                "code": "source_page_artifact_verification_failed",
                "message": "Source page artifact verification failed.",
            },
            headers=_ASSET_RESPONSE_HEADERS,
        )
    return payload


def _source_page_snapshot_id(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    source_ir_id: str,
    structure_snapshot_id: str,
    adapter_version: str,
    page_comparison_schema: str,
    geometry_schema_version: str,
) -> str:
    canonical = json.dumps(
        {
            "schema_version": SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION,
            "project_id": project_id,
            "document_id": document_id,
            "source_sha256": source_sha256,
            "source_ir_id": source_ir_id,
            "structure_snapshot_id": structure_snapshot_id,
            "adapter_version": adapter_version,
            "page_comparison_schema": page_comparison_schema,
            "geometry_schema_version": geometry_schema_version,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _is_pdf_source(source_ir: SourceIRSnapshot, *, source_path: Path) -> bool:
    if any(
        node.native_locator.format is NativeDocumentFormat.PDF
        for node in source_ir.nodes
    ):
        return True
    return source_path.suffix.casefold() == ".pdf"


def _staging_native_blocks(
    value: Any,
    *,
    assets: Any,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    page_number: int,
    page_bounds: list[float] | None,
    page_diagnostic_codes: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    nodes = value if isinstance(value, list) else []
    assets_by_id: dict[str, dict[str, Any]] = {}
    for asset in assets if isinstance(assets, list) else []:
        if not isinstance(asset, dict):
            continue
        asset_id = str(asset.get("asset_id") or "").strip()
        if not asset_id:
            raise ValueError("staged source asset identity is missing")
        if asset_id in assets_by_id and assets_by_id[asset_id] != asset:
            raise ValueError("staged source asset identity is duplicated")
        assets_by_id[asset_id] = asset
    for node in nodes:
        if not isinstance(node, dict):
            continue
        location = _record(node.get("visual_locator")) or _record(
            node.get("native_locator")
        )
        if int(location.get("page_number") or 0) != page_number:
            raise ValueError("staged native node belongs to another page")
        node_id = str(node.get("source_node_id") or "").strip()
        source_kind = str(node.get("kind") or "unknown").strip().lower()
        if not node_id:
            raise ValueError("staged native node identity is missing")
        bbox = _normalized_block_bbox(location.get("bbox"))
        table_payload = _record(node.get("table_payload"))
        table_id = str(table_payload.get("table_id") or "") or None
        node_diagnostics = list(node.get("extraction_diagnostics") or [])
        furniture_kind = pdf_image_furniture_kind_from_diagnostic_codes(
            str(_record(diagnostic).get("code") or "")
            for diagnostic in node_diagnostics
        )
        asset_ref = str(node.get("asset_ref") or "") or None
        asset = assets_by_id.get(asset_ref or "")
        if asset_ref is not None and asset is None:
            raise ValueError("staged native node references a missing source asset")
        block_type = _STAGING_NATIVE_BLOCK_TYPES.get(source_kind, "UNKNOWN")
        role = "TOC" if source_kind == "toc_entry" else None
        classification_basis = f"source_ir:{source_kind}"
        if furniture_kind is PdfPageFurnitureKind.HEADER:
            block_type = "HEADER"
            classification_basis = "source_ir:repeated_image_furniture"
        elif furniture_kind is PdfPageFurnitureKind.FOOTER:
            block_type = "FOOTER"
            classification_basis = "source_ir:repeated_image_furniture"
        asset_kind = str(_record(asset).get("kind") or "")
        if is_proven_pdf_page_raster_fact(
            node_kind=source_kind,
            asset_kind=asset_kind,
            image_bbox=bbox,
            page_bbox=page_bounds,
            diagnostic_codes={
                *page_diagnostic_codes,
                *(
                    str(_record(diagnostic).get("code") or "")
                    for diagnostic in node_diagnostics
                ),
            },
        ):
            block_type = "PAGE_RASTER"
            role = "SCAN_LAYER"
            classification_basis = "presenter:full_page_scan_layer"
        block = {
            "id": node_id,
            "source_ref": {"kind": "source_node", "source_id": node_id},
            "type": block_type,
            "role": role,
            "default_visible": block_type != "PAGE_RASTER",
            "overlay_default": block_type != "PAGE_RASTER",
            "source_kind": source_kind,
            "source_order": int(node.get("source_order") or 0),
            "text": str(node.get("raw_text") or ""),
            "page_number": page_number,
            "bbox": bbox,
            "locatable": bbox is not None,
            "location_status": "LOCATABLE" if bbox is not None else "UNLOCATABLE",
            "source_location": location,
            "classification_basis": classification_basis,
            "provenance": {
                "channel": "NATIVE",
                "fact_kind": "SOURCE_NODE",
                "source_sha256": source_sha256,
                "page_image_sha256": None,
                "adapter": {"id": adapter_id, "version": adapter_version},
                "provider": None,
                "confidence": None,
            },
            "table_id": table_id,
            "asset_ref": asset_ref,
            "asset": (
                {
                    "id": str(asset.get("asset_id") or ""),
                    "kind": str(asset.get("kind") or ""),
                    "media_type": str(asset.get("media_type") or ""),
                    "content_sha256": str(asset.get("content_sha256") or ""),
                }
                if asset is not None
                else None
            ),
            "diagnostics": node_diagnostics,
        }
        blocks.append(block)
        if table_id:
            tables.append(
                {
                    "id": table_id,
                    "channel": "NATIVE",
                    "page_number": page_number,
                    "source_node_id": node_id,
                    "table": table_payload,
                }
            )
    blocks.sort(key=lambda item: (int(item["source_order"]), str(item["id"])))
    return blocks, tables


def _staging_remote_blocks(
    value: Any,
    *,
    source_sha256: str,
    adapter_id: str,
    adapter_version: str,
    page_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[dict[str, Any]] = []
    tables: list[dict[str, Any]] = []
    snapshots = value if isinstance(value, list) else []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        if (
            str(snapshot.get("source_sha256") or "") != source_sha256
            or int(snapshot.get("page_number") or 0) != page_number
        ):
            raise ValueError("staged visual snapshot belongs to another source page")
        for observation in snapshot.get("observations") or []:
            if not isinstance(observation, dict):
                continue
            observation_id = str(observation.get("observation_id") or "").strip()
            if not observation_id:
                raise ValueError("staged visual observation identity is missing")
            content_type = str(observation.get("content_type") or "TEXT").upper()
            bbox = _normalized_block_bbox(observation.get("bbox"))
            layout_table = _record(observation.get("layout_table"))
            table_id = (
                _staging_visual_table_id(observation_id)
                if content_type == "TABLE" and layout_table
                else None
            )
            blocks.append(
                {
                    "id": observation_id,
                    "source_ref": {
                        "kind": "visual_text_observation",
                        "source_id": observation_id,
                    },
                    "type": content_type,
                    "role": observation.get("role"),
                    "default_visible": True,
                    "overlay_default": True,
                    "source_kind": content_type.lower(),
                    "source_order": None,
                    "text": str(observation.get("raw_text") or ""),
                    "page_number": page_number,
                    "bbox": bbox,
                    "locatable": bbox is not None,
                    "location_status": (
                        "LOCATABLE" if bbox is not None else "UNLOCATABLE"
                    ),
                    "source_location": {
                        "format": "pdf",
                        "page_number": page_number,
                        "bbox": bbox,
                    },
                    "classification_basis": f"provider:page_layout:{content_type}",
                    "provenance": {
                        "channel": "REMOTE_VISUAL",
                        "fact_kind": "VISUAL_PAGE_OBSERVATION",
                        "source_sha256": source_sha256,
                        "page_image_sha256": str(
                            observation.get("page_image_sha256")
                            or snapshot.get("page_image_sha256")
                            or ""
                        ),
                        "adapter": {
                            "id": adapter_id,
                            "version": adapter_version,
                        },
                        "provider": {
                            "id": str(observation.get("provider_id") or ""),
                            "version": str(
                                observation.get("provider_version") or ""
                            ),
                        },
                        "confidence": observation.get("confidence"),
                    },
                    "table_id": table_id,
                    "asset_ref": None,
                    "diagnostics": [],
                }
            )
            if table_id:
                tables.append(
                    {
                        "id": table_id,
                        "channel": "REMOTE_VISUAL",
                        "page_number": page_number,
                        "observation_id": observation_id,
                        "table": layout_table,
                    }
                )
    blocks.sort(
        key=lambda item: (
            int(_record(item.get("source_ref")).get("source_id") is None),
            str(item["id"]),
        )
    )
    return blocks, tables


def _staging_page_diagnostics(value: Any, *, page_number: int) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for diagnostic in value if isinstance(value, list) else []:
        if not isinstance(diagnostic, dict):
            continue
        location = _record(diagnostic.get("native_locator"))
        located_page = int(location.get("page_number") or page_number)
        if located_page != page_number:
            raise ValueError("staged diagnostic belongs to another page")
        diagnostics.append(
            {
                "code": str(diagnostic.get("code") or ""),
                "severity": str(diagnostic.get("severity") or "warning"),
                "message": str(diagnostic.get("message") or ""),
                "retryable": bool(diagnostic.get("retryable")),
                "source_node_id": str(diagnostic.get("source_node_id") or "")
                or None,
                "location": location or None,
                "evidence_sha256": str(
                    diagnostic.get("evidence_sha256") or ""
                )
                or None,
            }
        )
    return diagnostics


def _staging_visual_table_id(observation_id: str) -> str:
    suffix = observation_id[4:] if observation_id.startswith("vto-") else ""
    if not re.fullmatch(r"[0-9a-f]{32}", suffix):
        raise ValueError("staged visual table observation identity is invalid")
    return f"table-{suffix}"


def _abort_staged_page_best_effort(
    *,
    job_id: str,
    attempt_generation: int,
    page_number: int,
) -> None:
    from reqpilotx_server import db

    try:
        db.abort_document_extraction_page_reference(
            job_id=job_id,
            attempt_generation=attempt_generation,
            page_number=page_number,
        )
    except Exception:
        # Managed zero-ref inventory makes deferred cleanup recoverable.
        return


def _normalized_page_bounds(value: Any) -> list[float] | None:
    bbox = _normalized_block_bbox(value)
    if bbox is None:
        return None
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        raise ValueError("page staging bounds must have positive area")
    return bbox


def _normalized_block_bbox(value: Any) -> list[float] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("page staging bbox must contain four numbers")
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise ValueError("page staging bbox must contain four numbers") from exc
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        raise ValueError("page staging bbox is not ordered")
    return bbox


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"page staging {field} must be a positive integer")
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"page staging {field} must be a positive integer"
        ) from exc
    if normalized < 1:
        raise ValueError(f"page staging {field} must be a positive integer")
    return normalized


def _artifact_token(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "").strip())
    if not normalized:
        raise ValueError("page staging artifact identity is blank")
    return normalized[:160]


def _page_ranges(value: Any) -> list[dict[str, int]]:
    page_numbers = sorted(
        {
            int(item)
            for item in value if not isinstance(item, bool) and int(item) >= 1
        }
    ) if isinstance(value, (list, tuple, set)) else []
    ranges: list[dict[str, int]] = []
    for page_number in page_numbers:
        if not ranges or page_number > ranges[-1]["end_page"] + 1:
            ranges.append(
                {"start_page": page_number, "end_page": page_number}
            )
        else:
            ranges[-1]["end_page"] = page_number
    return ranges


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = [
    "DOCUMENT_EXTRACTION_PAGE_STAGING_SCHEMA_VERSION",
    "DOCUMENT_EXTRACTION_PAGE_STAGING_SUMMARY_SCHEMA_VERSION",
    "SOURCE_PAGE_PREVIEW_KEY",
    "SOURCE_PAGE_SNAPSHOT_SCHEMA_VERSION",
    "build_source_page_preview",
    "build_document_extraction_staged_page",
    "get_document_extraction_staged_source_page",
    "get_document_source_page",
    "persist_source_page_preview",
    "persist_document_extraction_staged_page",
    "public_document_extraction_page_staging",
    "public_source_page_snapshot",
]
