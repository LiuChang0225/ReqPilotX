from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from reqpilotx_server import db
from reqpilotx_server.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_ID,
    PDF_SOURCE_IR_ADAPTER_VERSION,
)
from reqpilotx_server.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    SHADOW_PREVIEW_SCHEMA_VERSION,
    ShadowPreviewRejected,
    build_extraction_shadow_preview,
)
from reqpilotx_server.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
)
from reqpilotx_server.services.document_parse_budget import document_parse_slot
from reqpilotx_server.services.extraction_visual_routes import (
    AuthorizedPdfVisualRuntime,
    ExtractionVisualRouteRejected,
    build_authorized_pdf_visual_runtime,
    resolve_authorized_pdf_visual_route,
    visual_route_capabilities,
    visual_runtime_audit,
)

logger = logging.getLogger(__name__)

SHADOW_RUN_SCHEMA_VERSION = "extraction-run/v2"
SHADOW_PAGE_SCHEMA_VERSION = "extraction-page/v4"
SHADOW_DETAILS_SCHEMA_VERSION = "extraction-details/v2"
SHADOW_RUN_CACHE_VERSION = "extraction-run-cache/v2"

_RUN_ID_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHADOW_EXECUTOR = ThreadPoolExecutor(
    max_workers=1,
    thread_name_prefix="extraction",
)
_SHADOW_LOCK = threading.RLock()
_ACTIVE_RUNS: dict[str, str] = {}
_CANCELLED_RUNS: set[str] = set()

_CACHE_TTL = timedelta(hours=24)
_CACHE_MAX_RUNS = 12
_CACHE_MAX_BYTES = 512 * 1024 * 1024
_HASH_READ_CHUNK_BYTES = 1024 * 1024

_HEAVY_PREVIEW_KEYS = frozenset(
    {
        "review_groups",
        "document_outline",
        "page_comparison",
        "document_regions",
        "requirement_groups",
        "tables",
        "items",
        "findings",
        "diagnostics",
    }
)


class ExtractionShadowRunRejected(RuntimeError):
    """Safe failure at the asynchronous page-run boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
    ) -> None:
        self.code = str(code or "extraction_shadow_run_rejected")
        self.status_code = int(status_code)
        super().__init__(message)


def extraction_shadow_cache_root(*, create: bool = True) -> Path:
    root = db.STORAGE_DIR / "extraction_shadow_runs"
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def start_extraction_shadow_run(
    *,
    project_id: str,
    document: dict[str, Any],
    initial_page: int = 1,
    force: bool = False,
    visual_provider: str = "off",
) -> dict[str, Any]:
    """Create or reuse one version-bound, single-flight Shadow run."""

    project_token = str(project_id or "").strip()
    document_id = str(document.get("id") or "").strip()
    document_project_id = str(document.get("project_id") or "").strip()
    if not project_token or not document_id or document_project_id != project_token:
        raise ExtractionShadowRunRejected(
            "shadow_run_document_mismatch",
            "The document is not registered for this project.",
            status_code=404,
        )
    if (
        isinstance(initial_page, bool)
        or not isinstance(initial_page, int)
        or initial_page < 1
    ):
        raise ExtractionShadowRunRejected(
            "invalid_initial_page",
            "The initial page must be a positive integer.",
        )

    source = _inspect_document_source(document)
    selected_visual_provider = str(visual_provider or "").strip().lower()
    authorized_visual_runtime = _prepare_authorized_visual_runtime(
        source=source,
        provider_name=selected_visual_provider,
    )
    visual_identity = (
        authorized_visual_runtime.cache_identity()
        if authorized_visual_runtime is not None
        else {
            "mode": "OFF",
            "provider_name": "off",
            "provider_id": None,
            "provider_version": None,
            "route_id": None,
            "route_sha256": None,
            "authorized_page_count": 0,
        }
    )
    cache_identity = _cache_identity(
        project_id=project_token,
        document_id=document_id,
        source_sha256=source["source_sha256"],
        source_adapter_version=(
            authorized_visual_runtime.runtime.pdf_adapter.adapter_version
            if authorized_visual_runtime is not None
            else PDF_SOURCE_IR_ADAPTER_VERSION
        ),
        visual_provider=selected_visual_provider,
        visual_identity=visual_identity,
    )
    run_id = _run_id_for_identity(cache_identity)
    generation_id = str(uuid.uuid4())

    with _SHADOW_LOCK:
        _prune_shadow_cache_locked()
        manifest = _load_manifest(run_id)
        if (
            not force
            and manifest is not None
            and str(manifest.get("run_status") or "").upper() == "READY"
            and _ready_artifacts_exist(manifest)
            and _manifest_source_is_current(document, manifest)
        ):
            return _public_manifest(manifest, cache_hit=True)

        if run_id in _ACTIVE_RUNS:
            active_manifest = _load_manifest(run_id) or manifest
            if active_manifest is None:
                active_manifest = _new_manifest(
                    run_id=run_id,
                    generation_id=_ACTIVE_RUNS[run_id],
                    project_id=project_token,
                    document_id=document_id,
                    cache_identity=cache_identity,
                    source=source,
                    initial_page=initial_page,
                )
            return _public_manifest(active_manifest, cache_hit=False)

        _CANCELLED_RUNS.discard(run_id)
        _ACTIVE_RUNS[run_id] = generation_id
        run_dir = _run_dir(run_id)
        _remove_run_dir(run_dir)
        (run_dir / "pages").mkdir(parents=True, exist_ok=True)
        manifest = _new_manifest(
            run_id=run_id,
            generation_id=generation_id,
            project_id=project_token,
            document_id=document_id,
            cache_identity=cache_identity,
            source=source,
            initial_page=initial_page,
        )
        _write_json_atomic(_manifest_path(run_id), manifest)
        _SHADOW_EXECUTOR.submit(
            _run_shadow_build,
            run_id,
            generation_id,
            project_token,
            document_id,
            str(source["path"]),
            str(document.get("source_sha256") or "").strip().lower(),
            initial_page,
            selected_visual_provider,
            authorized_visual_runtime,
        )
        return _public_manifest(manifest, cache_hit=False)


def get_extraction_shadow_visual_capabilities(
    *,
    project_id: str,
    document: dict[str, Any],
) -> dict[str, Any]:
    """Describe the exact remote route without preparing credentials or OCR."""

    project_token = str(project_id or "").strip()
    document_id = str(document.get("id") or "").strip()
    if (
        not project_token
        or not document_id
        or str(document.get("project_id") or "").strip() != project_token
    ):
        raise ExtractionShadowRunRejected(
            "shadow_run_document_mismatch",
            "The document is not registered for this project.",
            status_code=404,
        )
    try:
        source = _registered_visual_source_identity(document)
    except ExtractionShadowRunRejected as exc:
        if exc.code != "visual_route_source_identity_unavailable":
            raise
        return {
            **visual_route_capabilities(None),
            "authorization_status": "SOURCE_IDENTITY_UNAVAILABLE",
            "reason_code": exc.code,
            "project_id": project_token,
            "document_id": document_id,
            "source_sha256": None,
            "identity_basis": "REGISTERED_UPLOAD_SHA256",
            "runtime_source_revalidation": "REQUIRED_BEFORE_PROVIDER",
            "provider_credentials_checked": False,
        }
    try:
        route = resolve_authorized_pdf_visual_route(source["source_sha256"])
        capabilities = visual_route_capabilities(route)
    except ExtractionVisualRouteRejected as exc:
        raise ExtractionShadowRunRejected(
            exc.code,
            str(exc),
            status_code=503,
        ) from exc
    return {
        **capabilities,
        "project_id": project_token,
        "document_id": document_id,
        "source_sha256": source["source_sha256"],
        "identity_basis": "REGISTERED_UPLOAD_SHA256",
        "runtime_source_revalidation": "REQUIRED_BEFORE_PROVIDER",
        "provider_credentials_checked": False,
    }


def _registered_visual_source_identity(document: dict[str, Any]) -> dict[str, Any]:
    """Resolve capability routing without rereading a potentially large PDF.

    This is advisory only: ``start_extraction_shadow_run`` always calls the
    byte-level inspector before it constructs a remote provider or transfers a
    page.  Size validation here catches the common stale-file case while
    preserving P1's no-extra-full-scan behavior on document selection.
    """

    path = Path(str(document.get("file_path") or "").strip())
    try:
        stat = path.stat()
    except OSError as exc:
        raise ExtractionShadowRunRejected(
            "shadow_source_not_found",
            "The registered source file is unavailable.",
            status_code=404,
        ) from exc
    if not path.is_file():
        raise ExtractionShadowRunRejected(
            "shadow_source_not_found",
            "The registered source file is unavailable.",
            status_code=404,
        )
    source_sha256 = str(document.get("source_sha256") or "").strip().lower()
    if _SHA256_PATTERN.fullmatch(source_sha256) is None:
        raise ExtractionShadowRunRejected(
            "visual_route_source_identity_unavailable",
            "The document has no trusted upload identity for visual routing.",
            status_code=409,
        )
    stored_size = document.get("source_size")
    if (
        isinstance(stored_size, int)
        and not isinstance(stored_size, bool)
        and stored_size >= 0
        and stat.st_size != stored_size
    ):
        raise ExtractionShadowRunRejected(
            "source_hash_mismatch",
            "The source file no longer matches its registered size.",
            status_code=409,
        )
    return {
        "source_sha256": source_sha256,
        "source_size": stat.st_size,
    }


def get_extraction_shadow_run(
    *,
    project_id: str,
    document: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    manifest = _authorized_manifest(
        project_id=project_id,
        document=document,
        run_id=run_id,
    )
    if not _manifest_source_is_current(document, manifest):
        stale = dict(manifest)
        stale["run_status"] = "STALE"
        stale["progress"] = {
            **_record(manifest.get("progress")),
            "stage": "STALE",
        }
        return _public_manifest(stale, cache_hit=False)
    return _public_manifest(manifest, cache_hit=False)


def get_latest_extraction_shadow_run(
    *,
    project_id: str,
    document: dict[str, Any],
) -> dict[str, Any] | None:
    """Return the newest current run without creating or rebuilding one."""

    project_token = str(project_id or "").strip()
    document_id = str(document.get("id") or "").strip()
    if (
        not project_token
        or not document_id
        or str(document.get("project_id") or "").strip() != project_token
    ):
        raise ExtractionShadowRunRejected(
            "shadow_run_document_mismatch",
            "The document is not registered for this project.",
            status_code=404,
        )
    root = extraction_shadow_cache_root(create=False)
    if not root.is_dir():
        return None
    candidates: list[tuple[datetime, str, dict[str, Any]]] = []
    with _SHADOW_LOCK:
        for run_dir in root.iterdir():
            if not run_dir.is_dir() or not _RUN_ID_PATTERN.fullmatch(run_dir.name):
                continue
            manifest = _load_manifest(run_dir.name)
            if (
                manifest is None
                or str(manifest.get("project_id") or "") != project_token
                or str(manifest.get("document_id") or "") != document_id
                or not _manifest_source_is_current(document, manifest)
            ):
                continue
            status = str(manifest.get("run_status") or "").upper()
            if status == "READY" and not _ready_artifacts_exist(manifest):
                continue
            updated_at = (
                _parse_iso(str(manifest.get("updated_at") or ""))
                or _parse_iso(str(manifest.get("created_at") or ""))
                or datetime.min.replace(tzinfo=timezone.utc)
            )
            candidates.append((updated_at, run_dir.name, manifest))
    if not candidates:
        return None
    _updated_at, _run_id, latest = max(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    return _public_manifest(
        latest,
        cache_hit=str(latest.get("run_status") or "").upper() == "READY",
    )


def get_extraction_shadow_page_path(
    *,
    project_id: str,
    document: dict[str, Any],
    run_id: str,
    page_number: int,
) -> Path:
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        raise ExtractionShadowRunRejected(
            "invalid_page_number",
            "The requested page must be a positive integer.",
        )
    manifest = _authorized_ready_manifest(
        project_id=project_id,
        document=document,
        run_id=run_id,
    )
    page_count = int(_record(manifest.get("progress")).get("page_count") or 0)
    if page_number > page_count:
        raise ExtractionShadowRunRejected(
            "shadow_page_not_found",
            "The requested page is outside this document.",
            status_code=404,
        )
    path = _page_path(run_id, page_number)
    if not path.is_file():
        raise ExtractionShadowRunRejected(
            "shadow_page_not_ready",
            "The requested page snapshot is not ready.",
            status_code=409,
        )
    return path


def get_extraction_shadow_details_path(
    *,
    project_id: str,
    document: dict[str, Any],
    run_id: str,
) -> Path:
    _authorized_ready_manifest(
        project_id=project_id,
        document=document,
        run_id=run_id,
    )
    path = _details_path(run_id)
    if not path.is_file():
        raise ExtractionShadowRunRejected(
            "shadow_details_not_ready",
            "The document-level diagnostics are not ready.",
            status_code=409,
        )
    return path


def cancel_extraction_shadow_runs(document_id: str) -> None:
    document_token = str(document_id or "").strip()
    if not document_token:
        return
    with _SHADOW_LOCK:
        for run_id, _generation_id in tuple(_ACTIVE_RUNS.items()):
            manifest = _load_manifest(run_id)
            if str((manifest or {}).get("document_id") or "") == document_token:
                _CANCELLED_RUNS.add(run_id)


def purge_extraction_shadow_runs(document_id: str) -> None:
    """Remove disposable page-run artifacts for exactly one document."""

    document_token = str(document_id or "").strip()
    if not document_token:
        return
    cancel_extraction_shadow_runs(document_token)
    root = extraction_shadow_cache_root(create=False)
    if not root.exists():
        return
    with _SHADOW_LOCK:
        for run_dir in tuple(root.iterdir()):
            if not run_dir.is_dir() or not _RUN_ID_PATTERN.fullmatch(run_dir.name):
                continue
            manifest = _load_json(run_dir / "manifest.json")
            if str((manifest or {}).get("document_id") or "") == document_token:
                _remove_run_dir(run_dir)


def wait_for_extraction_shadow_run(
    run_id: str,
    *,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Test/diagnostic helper that waits without changing run state."""

    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while True:
        manifest = _load_manifest(run_id)
        status = str((manifest or {}).get("run_status") or "").upper()
        if status in {"READY", "FAILED", "CANCELLED"}:
            return manifest or {}
        if time.monotonic() >= deadline:
            return manifest or {}
        time.sleep(0.01)


def build_extraction_shadow_artifacts(
    preview: dict[str, Any],
    *,
    run_id: str,
    project_id: str,
    document_id: str,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]], dict[str, Any]]:
    """Split one immutable preview into metadata, page DTOs, and details."""

    comparison = _record(preview.get("page_comparison"))
    raw_pages = comparison.get("pages")
    if not isinstance(raw_pages, list):
        raise ValueError("page_comparison.pages must be a list")
    table_catalog = preview.get("tables")
    if not isinstance(table_catalog, list):
        raise ValueError("preview.tables must be a list")

    metadata = {
        key: value for key, value in preview.items() if key not in _HEAVY_PREVIEW_KEYS
    }
    metadata["page_comparison"] = {
        "schema_version": str(comparison.get("schema_version") or ""),
        "coordinate_space": str(comparison.get("coordinate_space") or ""),
        "interaction": str(comparison.get("interaction") or ""),
        "content_type_contract": (
            comparison.get("content_type_contract")
            if isinstance(comparison.get("content_type_contract"), list)
            else []
        ),
        "coverage": _record(comparison.get("coverage")),
        "counts": _record(comparison.get("counts")),
        "pages": [],
        "unassigned_blocks": [],
        "unassigned_issues": [],
    }

    pages: dict[int, dict[str, Any]] = {}
    for raw_page in raw_pages:
        page = _record(raw_page)
        page_number = _positive_int(page.get("page_number"))
        if page_number is None or page_number in pages:
            raise ValueError("page_comparison contains an invalid or duplicate page")
        blocks = page.get("blocks") if isinstance(page.get("blocks"), list) else []
        referenced_table_ids = {
            str(block.get("table_id") or "").strip()
            for block in blocks
            if isinstance(block, dict) and str(block.get("table_id") or "").strip()
        }
        block_source_ids = {
            str(block.get("id") or "").strip()
            for block in blocks
            if isinstance(block, dict) and str(block.get("id") or "").strip()
        }
        directly_referenced_tables = [
            table
            for table in table_catalog
            if isinstance(table, dict)
            and (
                str(table.get("id") or "").strip() in referenced_table_ids
                or str(table.get("source_node_id") or "").strip() in block_source_ids
            )
        ]
        referenced_logical_table_ids = {
            str(
                _record(table.get("continuation")).get("logical_table_id")
                or table.get("id")
                or ""
            ).strip()
            for table in directly_referenced_tables
        }
        page_tables = [
            table
            for table in table_catalog
            if isinstance(table, dict)
            and str(
                _record(table.get("continuation")).get("logical_table_id")
                or table.get("id")
                or ""
            ).strip()
            in referenced_logical_table_ids
        ]
        pages[page_number] = {
            "schema_version": SHADOW_PAGE_SCHEMA_VERSION,
            "preview_schema_version": str(preview.get("schema_version") or ""),
            "run_id": run_id,
            "project_id": project_id,
            "document_id": document_id,
            "page_number": page_number,
            "source_identity": _record(preview.get("source_identity")),
            "page_comparison": {
                "schema_version": str(comparison.get("schema_version") or ""),
                "coordinate_space": str(comparison.get("coordinate_space") or ""),
                "interaction": str(comparison.get("interaction") or ""),
                "content_type_contract": (
                    comparison.get("content_type_contract")
                    if isinstance(comparison.get("content_type_contract"), list)
                    else []
                ),
                "coverage": _record(page.get("coverage")),
                "counts": _record(page.get("counts")),
                "pages": [page],
                "unassigned_blocks": [],
                "unassigned_issues": [],
            },
            "tables": page_tables,
        }

    unassigned_blocks = comparison.get("unassigned_blocks")
    unassigned_issues = comparison.get("unassigned_issues")
    unassigned_table_ids = {
        str(block.get("table_id") or "").strip()
        for block in (unassigned_blocks if isinstance(unassigned_blocks, list) else [])
        if isinstance(block, dict) and str(block.get("table_id") or "").strip()
    }
    details = {
        "schema_version": SHADOW_DETAILS_SCHEMA_VERSION,
        "run_id": run_id,
        "project_id": project_id,
        "document_id": document_id,
        "findings": preview.get("findings")
        if isinstance(preview.get("findings"), list)
        else [],
        "diagnostics": preview.get("diagnostics")
        if isinstance(preview.get("diagnostics"), list)
        else [],
        "tables": [
            table
            for table in table_catalog
            if isinstance(table, dict)
            and str(table.get("id") or "").strip() in unassigned_table_ids
        ],
        "page_comparison": {
            "schema_version": str(comparison.get("schema_version") or ""),
            "coordinate_space": str(comparison.get("coordinate_space") or ""),
            "interaction": str(comparison.get("interaction") or ""),
            "content_type_contract": (
                comparison.get("content_type_contract")
                if isinstance(comparison.get("content_type_contract"), list)
                else []
            ),
            "coverage": _record(comparison.get("coverage")),
            "counts": _record(comparison.get("counts")),
            "pages": [],
            "unassigned_blocks": unassigned_blocks
            if isinstance(unassigned_blocks, list)
            else [],
            "unassigned_issues": unassigned_issues
            if isinstance(unassigned_issues, list)
            else [],
        },
    }
    metadata["details_counts"] = {
        "findings": len(details["findings"]),
        "diagnostics": len(details["diagnostics"]),
        "unassigned_blocks": len(details["page_comparison"]["unassigned_blocks"]),
        "unassigned_issues": len(details["page_comparison"]["unassigned_issues"]),
    }
    return metadata, pages, details


def _run_shadow_build(
    run_id: str,
    generation_id: str,
    project_id: str,
    document_id: str,
    source_path: str,
    stored_source_sha256: str,
    initial_page: int,
    visual_provider: str,
    authorized_visual_runtime: AuthorizedPdfVisualRuntime | None,
) -> None:
    started = time.perf_counter()
    queue_wait_ms = 0.0
    try:
        if _run_cancelled(run_id, generation_id):
            return
        _update_build_state(
            run_id,
            generation_id,
            run_status="QUEUED",
            stage="WAITING_FOR_CAPACITY",
        )
        with document_parse_slot() as queue_wait_ms:
            if _run_cancelled(run_id, generation_id):
                return
            _update_build_state(
                run_id,
                generation_id,
                run_status="BUILDING",
                stage="BUILDING",
            )
            preview = build_extraction_shadow_preview(
                source_path,
                expected_source_sha256=stored_source_sha256,
                visual_provider=visual_provider,
                visual_runtime=(
                    authorized_visual_runtime.runtime
                    if authorized_visual_runtime is not None
                    else None
                ),
                visual_authorization=(
                    visual_runtime_audit(authorized_visual_runtime)
                    if authorized_visual_runtime is not None
                    else None
                ),
                collection="document_outline",
                offset=0,
                limit=5_000,
            )
        if _run_cancelled(run_id, generation_id):
            return

        manifest = _load_manifest(run_id)
        if manifest is None:
            return
        expected_run_hash = str(
            _record(manifest.get("cache_identity")).get("source_sha256") or ""
        )
        actual_run_hash = str(
            _record(preview.get("source_identity")).get("source_sha256") or ""
        )
        if not expected_run_hash or actual_run_hash != expected_run_hash:
            raise ExtractionShadowRunRejected(
                "source_hash_mismatch",
                "The source changed while the page comparison was being built.",
                status_code=409,
            )
        _validate_preview_cache_identity(
            preview,
            _record(manifest.get("cache_identity")),
        )

        metadata, pages, details = build_extraction_shadow_artifacts(
            preview,
            run_id=run_id,
            project_id=project_id,
            document_id=document_id,
        )
        _update_build_state(
            run_id,
            generation_id,
            run_status="SERIALIZING",
            stage="SERIALIZING",
            page_count=len(pages),
        )
        cache_bytes = 0
        for ready_count, page_number in enumerate(
            _ordered_page_numbers(tuple(pages), initial_page),
            start=1,
        ):
            if _run_cancelled(run_id, generation_id):
                return
            cache_bytes += _write_json_atomic(
                _page_path(run_id, page_number),
                pages[page_number],
            )
            if ready_count == len(pages) or ready_count % 10 == 0:
                _update_build_state(
                    run_id,
                    generation_id,
                    run_status="SERIALIZING",
                    stage="SERIALIZING",
                    page_count=len(pages),
                    pages_ready=ready_count,
                )
        cache_bytes += _write_json_atomic(_details_path(run_id), details)
        completed_at = _now_iso()
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        with _SHADOW_LOCK:
            current = _load_manifest(run_id)
            if not _manifest_generation_matches(current, generation_id):
                return
            ready_manifest = {
                **current,
                "run_status": "READY",
                "updated_at": completed_at,
                "completed_at": completed_at,
                "progress": {
                    "stage": "READY",
                    "page_count": len(pages),
                    "pages_ready": len(pages),
                },
                "preview": metadata,
                "details_counts": _record(metadata.get("details_counts")),
                "metrics": {
                    "build_and_serialize_ms": elapsed_ms,
                    "queue_wait_ms": queue_wait_ms,
                    "cache_bytes": cache_bytes,
                    "page_count": len(pages),
                },
                "error": {},
            }
            cache_bytes += _write_json_atomic(
                _manifest_path(run_id),
                ready_manifest,
            )
            ready_manifest["metrics"]["cache_bytes"] = cache_bytes
            _write_json_atomic(_manifest_path(run_id), ready_manifest)
            _prune_shadow_cache_locked(protected_run_id=run_id)
    except ShadowPreviewRejected as exc:
        _mark_run_failed(
            run_id,
            generation_id,
            code=exc.code.value,
            message=str(exc),
            stage=exc.stage,
            cause_code=exc.cause_code,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    except ExtractionShadowRunRejected as exc:
        _mark_run_failed(
            run_id,
            generation_id,
            code=exc.code,
            message=str(exc),
            stage="run",
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    except Exception as exc:  # pragma: no cover - integration failure path
        logger.exception("Extraction Shadow run %s failed", run_id)
        _mark_run_failed(
            run_id,
            generation_id,
            code="shadow_run_failed",
            message="The page comparison run failed without producing a trusted snapshot.",
            stage="run",
            cause_code=type(exc).__name__,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )
    finally:
        with _SHADOW_LOCK:
            if _ACTIVE_RUNS.get(run_id) == generation_id:
                _ACTIVE_RUNS.pop(run_id, None)
            _CANCELLED_RUNS.discard(run_id)


def _new_manifest(
    *,
    run_id: str,
    generation_id: str,
    project_id: str,
    document_id: str,
    cache_identity: dict[str, Any],
    source: dict[str, Any],
    initial_page: int,
) -> dict[str, Any]:
    now = _now_iso()
    return {
        "schema_version": SHADOW_RUN_SCHEMA_VERSION,
        "cache_version": SHADOW_RUN_CACHE_VERSION,
        "run_id": run_id,
        "generation_id": generation_id,
        "project_id": project_id,
        "document_id": document_id,
        "run_status": "QUEUED",
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
        "initial_page": initial_page,
        "cache_identity": cache_identity,
        "source_stat": {
            "size": source["source_size"],
            "mtime_ns": source["source_mtime_ns"],
            "device": source["source_device"],
            "inode": source["source_inode"],
        },
        "progress": {
            "stage": "QUEUED",
            "page_count": 0,
            "pages_ready": 0,
        },
        "preview": {},
        "details_counts": {},
        "metrics": {},
        "error": {},
    }


def _prepare_authorized_visual_runtime(
    *,
    source: dict[str, Any],
    provider_name: str,
) -> AuthorizedPdfVisualRuntime | None:
    if provider_name == "off":
        return None
    try:
        return build_authorized_pdf_visual_runtime(
            source=Path(source["path"]),
            source_sha256=str(source["source_sha256"]),
            provider_name=provider_name,
        )
    except ExtractionVisualRouteRejected as exc:
        if exc.code in {"visual_route_source_mismatch"}:
            status_code = 409
        elif exc.code in {
            "visual_route_registry_unavailable",
            "visual_route_registry_invalid",
            "visual_route_registry_ambiguous",
        }:
            status_code = 503
        else:
            status_code = 422
        raise ExtractionShadowRunRejected(
            exc.code,
            str(exc),
            status_code=status_code,
        ) from exc


def _cache_identity(
    *,
    project_id: str,
    document_id: str,
    source_sha256: str,
    source_adapter_version: str,
    visual_provider: str,
    visual_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "identity_version": SHADOW_RUN_CACHE_VERSION,
        "project_id": project_id,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "source_adapter": {
            "id": PDF_SOURCE_IR_ADAPTER_ID,
            "version": source_adapter_version,
        },
        "structure_resolver": {
            "id": DEFAULT_STRUCTURE_RESOLVER_ID,
            "version": DEFAULT_STRUCTURE_RESOLVER_VERSION,
        },
        "shadow_presenter": SHADOW_PREVIEW_SCHEMA_VERSION,
        "page_presenter": PAGE_COMPARISON_SCHEMA_VERSION,
        "page_artifact": SHADOW_PAGE_SCHEMA_VERSION,
        "visual_provider": visual_provider,
        "visual_runtime": visual_identity,
        "collection": "document_outline",
    }


def _validate_preview_cache_identity(
    preview: dict[str, Any],
    cache_identity: dict[str, Any],
) -> None:
    pipeline = _record(preview.get("pipeline"))
    source_stage = _record(pipeline.get("source_ir"))
    structure_stage = _record(pipeline.get("structure"))
    comparison = _record(preview.get("page_comparison"))
    ocr = _record(preview.get("ocr"))
    ocr_provider = _record(ocr.get("provider"))
    ocr_route = _record(ocr.get("route"))
    expected_source = _record(cache_identity.get("source_adapter"))
    expected_structure = _record(cache_identity.get("structure_resolver"))
    expected_visual = _record(cache_identity.get("visual_runtime"))
    actual = {
        "source_id": str(source_stage.get("id") or PDF_SOURCE_IR_ADAPTER_ID),
        "source_version": str(source_stage.get("version") or ""),
        "structure_id": str(structure_stage.get("id") or DEFAULT_STRUCTURE_RESOLVER_ID),
        "structure_version": str(structure_stage.get("version") or ""),
        "shadow_presenter": str(preview.get("schema_version") or ""),
        "page_presenter": str(comparison.get("schema_version") or ""),
        "visual_provider": str(preview.get("visual_provider") or "").lower(),
        "visual_mode": str(ocr.get("mode") or "").upper(),
        "visual_provider_id": str(ocr_provider.get("id") or "") or None,
        "visual_provider_version": str(ocr_provider.get("version") or "") or None,
        "visual_route_id": str(ocr_route.get("route_id") or "") or None,
        "visual_route_sha256": str(ocr_route.get("route_sha256") or "") or None,
        "visual_authorized_page_count": int(ocr.get("authorized_page_count") or 0),
        "collection": str(preview.get("collection") or "").lower(),
    }
    expected = {
        "source_id": str(expected_source.get("id") or ""),
        "source_version": str(expected_source.get("version") or ""),
        "structure_id": str(expected_structure.get("id") or ""),
        "structure_version": str(expected_structure.get("version") or ""),
        "shadow_presenter": str(cache_identity.get("shadow_presenter") or ""),
        "page_presenter": str(cache_identity.get("page_presenter") or ""),
        "visual_provider": str(cache_identity.get("visual_provider") or "").lower(),
        "visual_mode": str(expected_visual.get("mode") or "").upper(),
        "visual_provider_id": expected_visual.get("provider_id"),
        "visual_provider_version": expected_visual.get("provider_version"),
        "visual_route_id": expected_visual.get("route_id"),
        "visual_route_sha256": expected_visual.get("route_sha256"),
        "visual_authorized_page_count": int(
            expected_visual.get("authorized_page_count") or 0
        ),
        "collection": str(cache_identity.get("collection") or "").lower(),
    }
    if actual != expected:
        raise ExtractionShadowRunRejected(
            "shadow_run_identity_mismatch",
            "The generated page comparison does not match its cache identity.",
            status_code=409,
        )


def _run_id_for_identity(identity: dict[str, Any]) -> str:
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _inspect_document_source(document: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(document.get("file_path") or "").strip())
    if not path.exists() or not path.is_file():
        raise ExtractionShadowRunRejected(
            "shadow_source_not_found",
            "The registered source file is unavailable.",
            status_code=404,
        )
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            while chunk := handle.read(_HASH_READ_CHUNK_BYTES):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        path_after = path.stat()
    except OSError as exc:
        raise ExtractionShadowRunRejected(
            "shadow_source_unavailable",
            "The registered source file could not be read.",
            status_code=404,
        ) from exc
    stat_identity_before = (
        before.st_size,
        before.st_mtime_ns,
        before.st_dev,
        before.st_ino,
    )
    stat_identity_after = (
        after.st_size,
        after.st_mtime_ns,
        after.st_dev,
        after.st_ino,
    )
    path_identity_after = (
        path_after.st_size,
        path_after.st_mtime_ns,
        path_after.st_dev,
        path_after.st_ino,
    )
    if (
        stat_identity_before != stat_identity_after
        or stat_identity_after != path_identity_after
    ):
        raise ExtractionShadowRunRejected(
            "shadow_source_changed_during_hash",
            "The source changed while its identity was being checked.",
            status_code=409,
        )
    source_sha256 = digest.hexdigest()
    stored_sha256 = str(document.get("source_sha256") or "").strip().lower()
    if stored_sha256 and (
        not _SHA256_PATTERN.fullmatch(stored_sha256) or stored_sha256 != source_sha256
    ):
        raise ExtractionShadowRunRejected(
            "source_hash_mismatch",
            "The source file no longer matches its registered identity.",
            status_code=409,
        )
    return {
        "path": path,
        "source_sha256": source_sha256,
        "source_size": after.st_size,
        "source_mtime_ns": after.st_mtime_ns,
        "source_device": after.st_dev,
        "source_inode": after.st_ino,
    }


def _authorized_manifest(
    *,
    project_id: str,
    document: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    run_token = _normalize_run_id(run_id)
    manifest = _load_manifest(run_token)
    if manifest is None:
        raise ExtractionShadowRunRejected(
            "shadow_run_not_found",
            "The requested page comparison run does not exist.",
            status_code=404,
        )
    if (
        str(manifest.get("project_id") or "") != str(project_id or "").strip()
        or str(manifest.get("document_id") or "")
        != str(document.get("id") or "").strip()
        or str(document.get("project_id") or "") != str(project_id or "").strip()
    ):
        raise ExtractionShadowRunRejected(
            "shadow_run_not_found",
            "The requested page comparison run does not exist.",
            status_code=404,
        )
    return manifest


def _authorized_ready_manifest(
    *,
    project_id: str,
    document: dict[str, Any],
    run_id: str,
) -> dict[str, Any]:
    manifest = _authorized_manifest(
        project_id=project_id,
        document=document,
        run_id=run_id,
    )
    if not _manifest_source_is_current(document, manifest):
        raise ExtractionShadowRunRejected(
            "source_hash_mismatch",
            "The source changed after this page comparison was built.",
            status_code=409,
        )
    if str(manifest.get("run_status") or "").upper() != "READY":
        raise ExtractionShadowRunRejected(
            "shadow_run_not_ready",
            "The page comparison run is not ready.",
            status_code=409,
        )
    return manifest


def _manifest_source_is_current(
    document: dict[str, Any],
    manifest: dict[str, Any],
) -> bool:
    path = Path(str(document.get("file_path") or "").strip())
    try:
        stat = path.stat()
    except OSError:
        return False
    expected_stat = _record(manifest.get("source_stat"))
    current_identity = (
        stat.st_size,
        stat.st_mtime_ns,
        stat.st_dev,
        stat.st_ino,
    )
    expected_identity = (
        int(expected_stat.get("size") or -1),
        int(expected_stat.get("mtime_ns") or -1),
        int(expected_stat.get("device") or -1),
        int(expected_stat.get("inode") or -1),
    )
    if current_identity == expected_identity:
        return True
    try:
        current = _inspect_document_source(document)
    except ExtractionShadowRunRejected:
        return False
    expected_sha256 = str(
        _record(manifest.get("cache_identity")).get("source_sha256") or ""
    )
    return str(current.get("source_sha256") or "") == expected_sha256


def _public_manifest(
    manifest: dict[str, Any],
    *,
    cache_hit: bool,
) -> dict[str, Any]:
    return {
        key: value
        for key, value in manifest.items()
        if key not in {"cache_version", "generation_id", "source_stat"}
    } | {
        "cache_hit": bool(cache_hit),
        "poll_after_ms": (
            0
            if str(manifest.get("run_status") or "").upper()
            in {"READY", "FAILED", "CANCELLED", "STALE"}
            else 500
        ),
    }


def _update_build_state(
    run_id: str,
    generation_id: str,
    *,
    run_status: str,
    stage: str,
    page_count: int | None = None,
    pages_ready: int | None = None,
) -> None:
    with _SHADOW_LOCK:
        manifest = _load_manifest(run_id)
        if not _manifest_generation_matches(manifest, generation_id):
            return
        progress = _record(manifest.get("progress"))
        if page_count is not None:
            progress["page_count"] = max(0, int(page_count))
        if pages_ready is not None:
            progress["pages_ready"] = max(0, int(pages_ready))
        progress["stage"] = str(stage or run_status).upper()
        manifest["run_status"] = str(run_status or "BUILDING").upper()
        manifest["updated_at"] = _now_iso()
        manifest["progress"] = progress
        _write_json_atomic(_manifest_path(run_id), manifest)


def _mark_run_failed(
    run_id: str,
    generation_id: str,
    *,
    code: str,
    message: str,
    stage: str,
    cause_code: str = "",
    elapsed_ms: float,
) -> None:
    with _SHADOW_LOCK:
        manifest = _load_manifest(run_id)
        if not _manifest_generation_matches(manifest, generation_id):
            return
        now = _now_iso()
        manifest.update(
            {
                "run_status": "FAILED",
                "updated_at": now,
                "completed_at": now,
                "progress": {
                    **_record(manifest.get("progress")),
                    "stage": "FAILED",
                },
                "metrics": {
                    **_record(manifest.get("metrics")),
                    "build_and_serialize_ms": elapsed_ms,
                },
                "error": {
                    "code": str(code or "shadow_run_failed"),
                    "message": str(message or "The page comparison run failed."),
                    "stage": str(stage or "run"),
                    "cause_code": str(cause_code or ""),
                },
            }
        )
        _write_json_atomic(_manifest_path(run_id), manifest)


def _ordered_page_numbers(
    page_numbers: tuple[int, ...],
    initial_page: int,
) -> tuple[int, ...]:
    available = set(page_numbers)
    preferred = [initial_page, initial_page - 1, initial_page + 1]
    ordered: list[int] = []
    for page_number in preferred:
        if page_number in available and page_number not in ordered:
            ordered.append(page_number)
    ordered.extend(sorted(available.difference(ordered)))
    return tuple(ordered)


def _ready_artifacts_exist(manifest: dict[str, Any]) -> bool:
    run_id = str(manifest.get("run_id") or "")
    if not _RUN_ID_PATTERN.fullmatch(run_id) or not _details_path(run_id).is_file():
        return False
    page_count = int(_record(manifest.get("progress")).get("page_count") or 0)
    return page_count > 0 and all(
        _page_path(run_id, page_number).is_file()
        for page_number in range(1, page_count + 1)
    )


def _run_cancelled(run_id: str, generation_id: str) -> bool:
    with _SHADOW_LOCK:
        return (
            run_id in _CANCELLED_RUNS
            or _ACTIVE_RUNS.get(run_id) != generation_id
            or not _manifest_generation_matches(_load_manifest(run_id), generation_id)
        )


def _manifest_generation_matches(
    manifest: dict[str, Any] | None,
    generation_id: str,
) -> bool:
    return bool(
        manifest
        and str(manifest.get("generation_id") or "") == str(generation_id or "")
    )


def _prune_shadow_cache_locked(*, protected_run_id: str = "") -> None:
    root = extraction_shadow_cache_root(create=False)
    if not root.exists():
        return
    now = datetime.now(timezone.utc)
    records: list[tuple[datetime, int, str, Path]] = []
    for run_dir in root.iterdir():
        if not run_dir.is_dir() or not _RUN_ID_PATTERN.fullmatch(run_dir.name):
            continue
        run_id = run_dir.name
        if run_id in _ACTIVE_RUNS or run_id == protected_run_id:
            continue
        manifest = _load_json(run_dir / "manifest.json") or {}
        completed_at = _parse_iso(
            str(manifest.get("completed_at") or manifest.get("updated_at") or "")
        )
        size = _directory_size(run_dir)
        if completed_at is not None and now - completed_at > _CACHE_TTL:
            _remove_run_dir(run_dir)
            continue
        records.append(
            (
                completed_at or datetime.min.replace(tzinfo=timezone.utc),
                size,
                run_id,
                run_dir,
            )
        )

    records.sort(key=lambda item: (item[0], item[2]))
    total_size = sum(item[1] for item in records)
    while records and (len(records) > _CACHE_MAX_RUNS or total_size > _CACHE_MAX_BYTES):
        _completed_at, size, _run_id, run_dir = records.pop(0)
        _remove_run_dir(run_dir)
        total_size = max(0, total_size - size)


def _directory_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            if child.is_file():
                total += child.stat().st_size
    except OSError:
        return total
    return total


def _remove_run_dir(path: Path) -> None:
    root = extraction_shadow_cache_root(create=False).resolve()
    try:
        target = path.resolve()
    except OSError:
        target = path.absolute()
    if target.parent != root or not _RUN_ID_PATTERN.fullmatch(target.name):
        raise RuntimeError("refusing to remove an unsafe Shadow run path")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)


def _run_dir(run_id: str) -> Path:
    return extraction_shadow_cache_root() / _normalize_run_id(run_id)


def _manifest_path(run_id: str) -> Path:
    return _run_dir(run_id) / "manifest.json"


def _details_path(run_id: str) -> Path:
    return _run_dir(run_id) / "details.json"


def _page_path(run_id: str, page_number: int) -> Path:
    return _run_dir(run_id) / "pages" / f"{int(page_number):06d}.json"


def _normalize_run_id(run_id: str) -> str:
    token = str(run_id or "").strip().lower()
    if not _RUN_ID_PATTERN.fullmatch(token):
        raise ExtractionShadowRunRejected(
            "invalid_shadow_run_id",
            "The page comparison run identifier is invalid.",
            status_code=404,
        )
    return token


def _load_manifest(run_id: str) -> dict[str, Any] | None:
    try:
        token = _normalize_run_id(run_id)
    except ExtractionShadowRunRejected:
        return None
    manifest = _load_json(
        extraction_shadow_cache_root(create=False) / token / "manifest.json"
    )
    if (
        not isinstance(manifest, dict)
        or str(manifest.get("schema_version") or "") != SHADOW_RUN_SCHEMA_VERSION
        or str(manifest.get("cache_version") or "") != SHADOW_RUN_CACHE_VERSION
        or str(manifest.get("run_id") or "") != token
    ):
        return None
    return manifest


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return len(encoded)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _record(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


__all__ = [
    "ExtractionShadowRunRejected",
    "SHADOW_DETAILS_SCHEMA_VERSION",
    "SHADOW_PAGE_SCHEMA_VERSION",
    "SHADOW_RUN_CACHE_VERSION",
    "SHADOW_RUN_SCHEMA_VERSION",
    "build_extraction_shadow_artifacts",
    "cancel_extraction_shadow_runs",
    "extraction_shadow_cache_root",
    "get_extraction_shadow_details_path",
    "get_latest_extraction_shadow_run",
    "get_extraction_shadow_page_path",
    "get_extraction_shadow_run",
    "get_extraction_shadow_visual_capabilities",
    "purge_extraction_shadow_runs",
    "start_extraction_shadow_run",
    "wait_for_extraction_shadow_run",
]
