"""Export a source-bound, offline PDF visual review bundle.

The bundle is a technical review aid containing customer page images and
SourceIR-derived page DTOs.  It is never Human Gold, a formal approval, or a
serving activation gate.  Publication is fail-closed: the source hash and full
preview page set are validated, the destination must be outside the supplied
corpus root and absent, and a sibling staging directory is atomically renamed
only after every artifact hash has been recorded.
"""

# The repository-local app path must be installed before application imports.
# ruff: noqa: E402

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

try:
    import pymupdf
except ImportError:  # pragma: no cover - the production dependency is pinned
    pymupdf = None  # type: ignore[assignment]

try:
    import PIL
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # pragma: no cover - the production dependency is pinned
    PIL = None  # type: ignore[assignment]
    Image = None  # type: ignore[assignment]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]

from app.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    SHADOW_PREVIEW_SCHEMA_VERSION,
    ShadowPreviewRejected,
    build_extraction_shadow_preview,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
)
from app.adapters.documents.pdf_visual_text_runtime import (
    OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
    OCR_PROVIDER_BAILIAN_LAYOUT,
    SUPPORTED_PDF_VISUAL_PROVIDER_NAMES,
    visual_text_provider_id,
)
from app.services.extraction_visual_routes import (
    ExtractionVisualRouteRejected,
    build_authorized_pdf_visual_runtime,
    visual_runtime_audit,
)
from scripts.extraction_case_runner import (
    CorpusPathError,
    require_report_destination_outside_corpus,
)
from scripts.storage_backup import BackupError, _rename_no_replace


PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION = "pdf-visual-review-bundle/v2"
BUNDLE_HASH_CONTRACT = "canonical-json-manifest-without-bundle-sha256/v1"
STRUCTURE_ONLY_INSPECTION_STATUS = "STRUCTURE_ONLY_NON_AUTHENTICATED"
_STRUCTURE_SCOPE_VERIFIED = "SELF_CONSISTENCY_VERIFIED"
_VISUAL_TABLE_EXPORTER_CLAIM = "EXPORTER_CLAIM_ONLY_REQUIRES_EXTERNAL_PIN"
_PARTIAL_COVERAGE_EXPORTER_CLAIM = (
    "PARTIAL_DOCUMENT_DERIVABLE_SUBSET_ONLY_REQUIRES_EXTERNAL_PIN"
)
DEFAULT_RENDER_SCALE = 2.0
MAX_RENDER_SCALE = 4.0
_EXCESSIVE_FIGURE_FRAGMENT_COUNT = 128
_PAGE_SPANNING_TABLE_AREA_RATIO = 0.80
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SOURCE_NODE_ID_PATTERN = re.compile(r"^node-[0-9a-f]{32}$")
_VISUAL_TEXT_OBSERVATION_ID_PATTERN = re.compile(r"^vto-[0-9a-f]{32}$")
_EXTERNAL_URL_PATTERN = re.compile(
    r"""(?ix)
    \b(?:https?|ftp|file|data):
    |
    \b(?:src|href)\s*=\s*["']\s*//
    """
)
_COLORS_BY_TYPE = {
    "TEXT": "#2563eb",
    "TABLE": "#059669",
    "FIGURE": "#d97706",
    "TOC": "#7c3aed",
    "HEADER": "#64748b",
    "FOOTER": "#64748b",
    "WATERMARK": "#dc2626",
    "PAGE_RASTER": "#0f766e",
    "UNKNOWN": "#be123c",
}
_PAGE_CONTENT_TYPES = (
    "TEXT",
    "TABLE",
    "FIGURE",
    "HEADER",
    "FOOTER",
    "WATERMARK",
)
_PAGE_BLOCK_TYPES = frozenset((*_PAGE_CONTENT_TYPES, "TOC", "PAGE_RASTER", "UNKNOWN"))
_VISUAL_TEXT_AUTHORITY_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_without_visible_text",
        "pdf_vector_text_with_low_native_unicode_coverage",
        "pdf_visual_text_user_forced",
    }
)
_VISUAL_TEXT_REASON_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_with_low_native_text_coverage",
        "pdf_full_page_image_without_visible_text",
        "pdf_vector_text_with_low_native_unicode_coverage",
    }
)
_VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_visual_page_locator_unavailable",
        "pdf_visual_page_render_failed",
        "pdf_visual_text_access_denied",
        "pdf_visual_text_authentication_failed",
        "pdf_visual_text_capture_asset_hash_mismatch",
        "pdf_visual_text_capture_invalid",
        "pdf_visual_text_capture_page_ambiguous",
        "pdf_visual_text_capture_page_hash_mismatch",
        "pdf_visual_text_capture_page_not_authorized",
        "pdf_visual_text_capture_page_scope_mismatch",
        "pdf_visual_text_capture_provider_mismatch",
        "pdf_visual_text_capture_source_mismatch",
        "pdf_visual_text_invalid_input",
        "pdf_visual_text_no_observations",
        "pdf_visual_text_out_of_scope_evidence_mismatch",
        "pdf_visual_text_page_image_hash_mismatch",
        "pdf_visual_text_page_out_of_scope",
        "pdf_visual_text_provider_failed",
        "pdf_visual_text_provider_not_configured",
        "pdf_visual_text_provider_output_invalid",
        "pdf_visual_text_quota_exhausted",
        "pdf_visual_text_rate_limited",
        "pdf_visual_text_required_page_not_authorized",
        "pdf_visual_text_source_hash_mismatch",
        "pdf_visual_text_unavailable",
    }
)
_PAGE_BLOCK_FIELDS = {
    "id",
    "source_ref",
    "label",
    "type",
    "role",
    "default_visible",
    "overlay_default",
    "source_kind",
    "source_order",
    "text",
    "page_number",
    "bbox",
    "locatable",
    "location_status",
    "source_location",
    "classification_basis",
    "provenance",
    "table_id",
    "asset",
    "diagnostics",
}
_SOURCE_ASSET_KINDS = {
    "image",
    "figure",
    "table_render",
    "page_render",
    "attachment",
    "unknown",
}
_NATIVE_BLOCK_TYPES_BY_SOURCE_KIND = {
    "heading": frozenset({"TEXT"}),
    "paragraph": frozenset({"TEXT"}),
    "list": frozenset({"TEXT"}),
    "list_item": frozenset({"TEXT"}),
    "table": frozenset({"TABLE"}),
    "image": frozenset({"FIGURE", "PAGE_RASTER"}),
    "figure": frozenset({"FIGURE", "PAGE_RASTER"}),
    "note": frozenset({"TEXT"}),
    "header": frozenset({"HEADER"}),
    "footer": frozenset({"FOOTER"}),
    "watermark": frozenset({"WATERMARK"}),
    "toc_entry": frozenset({"TEXT"}),
    "attachment": frozenset({"UNKNOWN"}),
    "unknown": frozenset({"UNKNOWN"}),
}
_CAPTURE_FIELDS = {
    "mode",
    "capability",
    "status",
    "provider",
    "snapshot_count",
    "observation_count",
    "authorized_page_count",
    "captured_pages",
    "routing",
    "evaluated_content_types",
    "confidence",
    "route",
    "coordinate_mapping",
    "visual_table",
    "local_fallback",
    "implicit_fallback",
    "failure_policy",
    "message",
}
_BLOCK_PROVENANCE_FIELDS = {
    "channel",
    "fact_kind",
    "source_sha256",
    "page_image_sha256",
    "adapter",
    "provider",
    "confidence",
    "coordinate_mapping",
}


class PdfVisualReviewBundleRejected(RuntimeError):
    """Fail-closed bundle rejection with a stable, content-free code."""

    def __init__(self, code: str, *, cause_code: str = "") -> None:
        token = str(code or "").strip()
        if not token:
            raise ValueError("bundle rejection code must not be blank")
        self.code = token
        self.cause_code = str(cause_code or "").strip()
        super().__init__(token)


def export_pdf_visual_review_bundle(
    source: Path,
    *,
    document_id: str,
    expected_source_sha256: str,
    page_start: int,
    page_end: int,
    output_dir: Path,
    corpus_root: Path,
    render_scale: float = DEFAULT_RENDER_SCALE,
    visual_provider: str = "off",
    visual_registry_path: Path | None = None,
) -> dict[str, Any]:
    """Build and atomically publish an offline visual-review directory.

    Remote visual capture is opt-in and fail-closed.  When selected, the
    runtime is constructed only from the controlled exact source/page-hash
    route registry, and the resulting provider identity, route, and capture
    inventory are included in the bundle hash contract.
    """

    document_token = _require_document_id(document_id)
    expected_hash = _require_sha256(expected_source_sha256)
    selected_page_numbers = _require_page_range(page_start, page_end)
    scale = _require_render_scale(render_scale)
    selected_visual_provider = _require_visual_provider(visual_provider)
    if selected_visual_provider == "off" and visual_registry_path is not None:
        _reject("bundle_visual_registry_without_provider")
    source_path = _require_regular_file(Path(source))
    corpus_path = _require_corpus_root(Path(corpus_root))
    destination = require_report_destination_outside_corpus(
        Path(output_dir),
        corpus_path,
    )
    if destination.exists():
        _reject("bundle_output_exists")
    if _sha256_file(source_path) != expected_hash:
        _reject("bundle_source_hash_mismatch")

    authorized_visual_runtime = None
    visual_authorization: dict[str, Any] | None = None
    if selected_visual_provider != "off":
        try:
            authorized_visual_runtime = build_authorized_pdf_visual_runtime(
                source=source_path,
                source_sha256=expected_hash,
                provider_name=selected_visual_provider,
                registry_path=visual_registry_path,
            )
            visual_authorization = visual_runtime_audit(authorized_visual_runtime)
        except ExtractionVisualRouteRejected as exc:
            raise PdfVisualReviewBundleRejected(
                "bundle_visual_runtime_rejected",
                cause_code=exc.code,
            ) from exc
        except RuntimeError as exc:
            raise PdfVisualReviewBundleRejected(
                "bundle_visual_runtime_rejected",
                cause_code="visual_runtime_invalid",
            ) from exc

    try:
        preview = build_extraction_shadow_preview(
            source_path,
            expected_source_sha256=expected_hash,
            collection="document_outline",
            visual_provider=selected_visual_provider,
            visual_runtime=(
                authorized_visual_runtime.runtime
                if authorized_visual_runtime is not None
                else None
            ),
            visual_authorization=visual_authorization,
            offset=0,
            limit=5_000,
        )
    except ShadowPreviewRejected as exc:
        raise PdfVisualReviewBundleRejected(
            "bundle_preview_rejected",
            cause_code=exc.code.value,
        ) from exc

    page_dtos, page_count = _validated_preview_pages(
        preview,
        selected_page_numbers=selected_page_numbers,
        expected_source_sha256=expected_hash,
        expected_visual_provider=selected_visual_provider,
    )
    visual_capture = (
        None
        if selected_visual_provider == "off"
        else _validated_remote_visual_capture(
            preview,
            authorization=visual_authorization,
            expected_source_sha256=expected_hash,
            expected_visual_provider=selected_visual_provider,
            page_count=page_count,
        )
    )
    if visual_capture is not None and selected_page_numbers != tuple(
        range(1, page_count + 1)
    ):
        _reject("bundle_remote_full_document_required")
    _validate_page_visual_provenance(
        page_dtos,
        expected_source_sha256=expected_hash,
        expected_adapter={
            "id": preview["pipeline"]["source_ir"]["id"],
            "version": preview["pipeline"]["source_ir"]["version"],
            "status": preview["pipeline"]["source_ir"]["status"],
        },
        authorization=visual_authorization,
        capture=visual_capture,
        document_coverage=preview["page_comparison"]["coverage"],
        document_page_count=page_count,
        full_document=selected_page_numbers == tuple(range(1, page_count + 1)),
    )
    try:
        source_bytes = source_path.read_bytes()
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_source_unavailable") from exc
    if hashlib.sha256(source_bytes).hexdigest() != expected_hash:
        _reject("bundle_source_hash_drift")

    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
    )
    staging.chmod(0o700)
    try:
        page_entries = _write_page_artifacts(
            staging,
            source_bytes=source_bytes,
            page_dtos=page_dtos,
            render_scale=scale,
            source_ir_status=str(preview["pipeline"]["source_ir"]["status"]),
        )
        document_risk_flags = _document_risk_flags(
            source_ir_status=str(preview["pipeline"]["source_ir"]["status"]),
            structure_status=str(preview["pipeline"]["structure"]["status"]),
            visual_provider=selected_visual_provider,
            selected_page_numbers=selected_page_numbers,
            page_count=page_count,
            page_entries=page_entries,
            unassigned_blocks=len(preview["page_comparison"]["unassigned_blocks"]),
            unassigned_issues=len(preview["page_comparison"]["unassigned_issues"]),
        )
        index_bytes = _offline_index_html(
            document_id=document_token,
            source_sha256=expected_hash,
            page_entries=page_entries,
            document_risk_flags=document_risk_flags,
        )
        _write_bytes(staging / "index.html", index_bytes)
        index_sha256 = hashlib.sha256(index_bytes).hexdigest()

        page_comparison = preview["page_comparison"]
        pipeline: dict[str, Any] = {
            "visual_provider": selected_visual_provider,
            "collection": "document_outline",
            "adapter": {
                "id": preview["pipeline"]["source_ir"]["id"],
                "version": preview["pipeline"]["source_ir"]["version"],
                "status": preview["pipeline"]["source_ir"]["status"],
            },
            "resolver": {
                "id": DEFAULT_STRUCTURE_RESOLVER_ID,
                "version": preview["pipeline"]["structure"]["version"],
                "status": preview["pipeline"]["structure"]["status"],
            },
            "preview": {
                "schema_version": preview["schema_version"],
            },
            "page_comparison": {
                "schema_version": page_comparison["schema_version"],
            },
        }
        if visual_capture is not None:
            pipeline["visual_authorization"] = visual_authorization
            pipeline["visual_capture"] = visual_capture

        manifest: dict[str, Any] = {
            "schema_version": PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bundle_kind": "TECHNICAL_VISUAL_REVIEW_DRAFT",
            "bundle_hash_contract": BUNDLE_HASH_CONTRACT,
            "document": {
                "document_id": document_token,
                "source_sha256": expected_hash,
                "format": "pdf",
                "page_count": page_count,
            },
            "scope": {
                "page_start": selected_page_numbers[0],
                "page_end": selected_page_numbers[-1],
                "page_numbers": list(selected_page_numbers),
                "page_count": len(selected_page_numbers),
                "full_document": selected_page_numbers
                == tuple(range(1, page_count + 1)),
            },
            "pipeline": pipeline,
            "renderer": {
                "id": "pymupdf-full-page-annotation-free",
                "version": str(getattr(pymupdf, "VersionBind", "unknown")),
                "scale": scale,
                "alpha": False,
                "annotations": False,
                "annotator": {
                    "id": "pillow-bbox-type-label",
                    "version": str(getattr(PIL, "__version__", "unknown")),
                },
            },
            "coverage": {
                "page_set": {
                    "expected_document_pages": list(range(1, page_count + 1)),
                    "preview_document_pages": [
                        int(page["page_number"]) for page in page_comparison["pages"]
                    ],
                    "exported_pages": list(selected_page_numbers),
                    "preview_complete": True,
                    "preview_ordered": True,
                    "export_exact_range": True,
                },
                "document": page_comparison["coverage"],
                "unassigned_blocks": len(page_comparison["unassigned_blocks"]),
                "unassigned_issues": len(page_comparison["unassigned_issues"]),
            },
            "risk_flags": document_risk_flags,
            "entrypoint": {
                "path": "index.html",
                "sha256": index_sha256,
                "offline": True,
                "external_urls": False,
            },
            "pages": page_entries,
            "privacy": {
                "classification": "CUSTOMER_CONTENT_SENSITIVE",
                "handling": "CONTROLLED_LOCAL_REVIEW_ONLY",
                "contains_customer_content": True,
                "contains_customer_text": True,
                "contains_source_page_images": True,
                "contains_annotated_page_images": True,
                "contains_document_id": True,
                "contains_source_path": False,
                "contains_source_filename": False,
                "is_anonymized": False,
                "source_hash_is_anonymization": False,
            },
            "review_provenance": {
                "status": "TECHNICAL_DRAFT",
                "approval_status": "NOT_APPROVED",
                "is_human_gold": False,
                "is_gold": False,
                "is_formal_approval": False,
                "is_serving_activation_gate": False,
            },
        }
        manifest["bundle_sha256"] = canonical_bundle_sha256(manifest)
        _write_json(staging / "manifest.json", manifest)
        _fsync_directory(staging)

        if _sha256_file(source_path) != expected_hash:
            _reject("bundle_source_hash_drift")
        if destination.exists():
            _reject("bundle_output_exists")
        try:
            _rename_no_replace(staging, destination)
        except BackupError as exc:
            if destination.exists():
                raise PdfVisualReviewBundleRejected("bundle_output_exists") from exc
            raise PdfVisualReviewBundleRejected("bundle_atomic_publish_failed") from exc
        _fsync_directory(destination.parent)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def canonical_bundle_sha256(manifest: dict[str, Any]) -> str:
    """Hash the canonical manifest, excluding its self-referential digest."""

    payload = dict(manifest)
    payload.pop("bundle_sha256", None)
    return hashlib.sha256(
        _canonical_json_bytes(payload, trailing_newline=False)
    ).hexdigest()


def inspect_pdf_visual_review_bundle(
    bundle_dir: Path,
    *,
    require_full_document: bool = False,
) -> dict[str, Any]:
    """Compatibility API for structure-only, non-authenticating inspection.

    This validates the bundle's self-consistency but deliberately cannot prove
    that a coherently rewritten bundle is the originally published one.  Use
    :func:`verify_pdf_visual_review_bundle` with an independently stored pin
    anywhere authenticity matters.
    """

    inspection = inspect_pdf_visual_review_bundle_structure_only(
        bundle_dir,
        require_full_document=require_full_document,
    )
    return inspection["manifest"]


def inspect_pdf_visual_review_bundle_structure_only(
    bundle_dir: Path,
    *,
    require_full_document: bool = False,
) -> dict[str, Any]:
    """Return an explicitly non-authenticating structural inspection result."""

    manifest = _validate_pdf_visual_review_bundle(
        bundle_dir,
        expected_bundle_sha256=None,
        require_full_document=require_full_document,
    )
    return {
        "status": STRUCTURE_ONLY_INSPECTION_STATUS,
        "authenticity_established": False,
        "external_pin_required_for_authenticity": True,
        "validation_scope": _structure_only_validation_scope(manifest),
        "manifest": manifest,
    }


def verify_pdf_visual_review_bundle(
    bundle_dir: Path,
    *,
    expected_bundle_sha256: str,
    require_full_document: bool = False,
) -> dict[str, Any]:
    """Verify integrity and require one independently supplied bundle pin."""

    return _validate_pdf_visual_review_bundle(
        bundle_dir,
        expected_bundle_sha256=expected_bundle_sha256,
        require_full_document=require_full_document,
    )


def _validate_pdf_visual_review_bundle(
    bundle_dir: Path,
    *,
    expected_bundle_sha256: str | None,
    require_full_document: bool,
) -> dict[str, Any]:
    """Validate the strict draft contract, inventory, and artifact hashes."""

    if not isinstance(require_full_document, bool):
        _reject("bundle_full_document_requirement_invalid")
    expected_bundle_hash = (
        None
        if expected_bundle_sha256 is None
        else _require_sha256(
            expected_bundle_sha256,
            code="bundle_expected_sha256_invalid",
        )
    )

    root = Path(bundle_dir)
    try:
        metadata = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_verify_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _reject("bundle_verify_unavailable")
    manifest_path = root / "manifest.json"
    try:
        manifest_metadata = manifest_path.stat(follow_symlinks=False)
        if not stat.S_ISREG(manifest_metadata.st_mode):
            _reject("bundle_manifest_unavailable")
        manifest = json.loads(
            manifest_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise PdfVisualReviewBundleRejected("bundle_manifest_invalid") from exc
    required_root_fields = {
        "schema_version",
        "generated_at",
        "bundle_kind",
        "bundle_hash_contract",
        "document",
        "scope",
        "pipeline",
        "renderer",
        "coverage",
        "risk_flags",
        "entrypoint",
        "pages",
        "privacy",
        "review_provenance",
        "bundle_sha256",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_root_fields:
        _reject("bundle_manifest_schema_invalid")
    if (
        manifest.get("schema_version") != PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION
        or manifest.get("bundle_kind") != "TECHNICAL_VISUAL_REVIEW_DRAFT"
        or manifest.get("bundle_hash_contract") != BUNDLE_HASH_CONTRACT
        or not isinstance(manifest.get("generated_at"), str)
        or not str(manifest["generated_at"]).strip()
        or not isinstance(manifest.get("pipeline"), dict)
        or not isinstance(manifest.get("renderer"), dict)
        or not isinstance(manifest.get("risk_flags"), list)
    ):
        _reject("bundle_manifest_contract_invalid")
    render_scale = _validated_renderer_contract(manifest["renderer"])
    bundle_hash = _require_sha256(
        manifest.get("bundle_sha256"),
        code="bundle_sha256_invalid",
    )
    if bundle_hash != manifest["bundle_sha256"]:
        _reject("bundle_sha256_invalid")
    if bundle_hash != canonical_bundle_sha256(manifest):
        _reject("bundle_sha256_mismatch")
    if expected_bundle_hash is not None and bundle_hash != expected_bundle_hash:
        _reject("bundle_expected_sha256_mismatch")

    document = manifest.get("document")
    scope = manifest.get("scope")
    pages = manifest.get("pages")
    if (
        not isinstance(document, dict)
        or set(document) != {"document_id", "source_sha256", "format", "page_count"}
        or not isinstance(document.get("document_id"), str)
        or _require_document_id(document["document_id"]) != document["document_id"]
        or document.get("format") != "pdf"
        or isinstance(document.get("page_count"), bool)
        or not isinstance(document.get("page_count"), int)
        or document["page_count"] < 1
    ):
        _reject("bundle_document_contract_invalid")
    source_hash = _require_sha256(
        document.get("source_sha256"),
        code="bundle_source_hash_invalid",
    )
    if source_hash != document["source_sha256"]:
        _reject("bundle_source_hash_invalid")
    _verify_pipeline_contract(
        manifest["pipeline"],
        expected_source_sha256=source_hash,
        page_count=document["page_count"],
    )
    if (
        not isinstance(scope, dict)
        or set(scope)
        != {
            "page_start",
            "page_end",
            "page_numbers",
            "page_count",
            "full_document",
        }
        or not isinstance(pages, list)
    ):
        _reject("bundle_page_inventory_invalid")
    selected_pages = list(
        _require_page_range(scope.get("page_start"), scope.get("page_end"))
    )
    document_pages = list(range(1, document["page_count"] + 1))
    if (
        scope.get("page_numbers") != selected_pages
        or isinstance(scope.get("page_count"), bool)
        or not isinstance(scope.get("page_count"), int)
        or scope["page_count"] != len(selected_pages)
        or selected_pages[-1] > document["page_count"]
        or not isinstance(scope.get("full_document"), bool)
        or scope["full_document"] != (selected_pages == document_pages)
    ):
        _reject("bundle_page_inventory_invalid")
    if require_full_document and scope["full_document"] is not True:
        _reject("bundle_full_document_required")

    page_numbers = [
        page.get("page_number") if isinstance(page, dict) else None for page in pages
    ]
    if page_numbers != selected_pages:
        _reject("bundle_page_inventory_invalid")

    coverage = manifest.get("coverage")
    page_set = coverage.get("page_set") if isinstance(coverage, dict) else None
    if (
        not isinstance(coverage, dict)
        or set(coverage)
        != {"page_set", "document", "unassigned_blocks", "unassigned_issues"}
        or not isinstance(page_set, dict)
        or set(page_set)
        != {
            "expected_document_pages",
            "preview_document_pages",
            "exported_pages",
            "preview_complete",
            "preview_ordered",
            "export_exact_range",
        }
        or page_set.get("expected_document_pages") != document_pages
        or page_set.get("preview_document_pages") != document_pages
        or page_set.get("exported_pages") != selected_pages
        or page_set.get("preview_complete") is not True
        or page_set.get("preview_ordered") is not True
        or page_set.get("export_exact_range") is not True
        or isinstance(coverage.get("unassigned_blocks"), bool)
        or not isinstance(coverage.get("unassigned_blocks"), int)
        or coverage["unassigned_blocks"] < 0
        or isinstance(coverage.get("unassigned_issues"), bool)
        or not isinstance(coverage.get("unassigned_issues"), int)
        or coverage["unassigned_issues"] < 0
    ):
        _reject("bundle_coverage_page_set_invalid")

    review_provenance = manifest.get("review_provenance")
    if review_provenance != {
        "status": "TECHNICAL_DRAFT",
        "approval_status": "NOT_APPROVED",
        "is_human_gold": False,
        "is_gold": False,
        "is_formal_approval": False,
        "is_serving_activation_gate": False,
    }:
        _reject("bundle_review_provenance_invalid")

    privacy = manifest.get("privacy")
    if privacy != {
        "classification": "CUSTOMER_CONTENT_SENSITIVE",
        "handling": "CONTROLLED_LOCAL_REVIEW_ONLY",
        "contains_customer_content": True,
        "contains_customer_text": True,
        "contains_source_page_images": True,
        "contains_annotated_page_images": True,
        "contains_document_id": True,
        "contains_source_path": False,
        "contains_source_filename": False,
        "is_anonymized": False,
        "source_hash_is_anonymization": False,
    }:
        _reject("bundle_privacy_contract_invalid")

    artifact_records: list[dict[str, Any]] = []
    entrypoint = manifest.get("entrypoint")
    if (
        not isinstance(entrypoint, dict)
        or set(entrypoint) != {"path", "sha256", "offline", "external_urls"}
        or entrypoint.get("path") != "index.html"
        or entrypoint.get("offline") is not True
        or entrypoint.get("external_urls") is not False
    ):
        _reject("bundle_artifact_inventory_invalid")
    artifact_records.append(entrypoint)
    for page in pages:
        if not isinstance(page, dict):
            _reject("bundle_page_inventory_invalid")
        artifact_records.extend(_validated_page_artifact_records(page))

    seen_paths: set[str] = set()
    artifact_contracts: list[tuple[str, str]] = []
    for record in artifact_records:
        relative_path = _safe_artifact_path(record.get("path"))
        if relative_path in seen_paths:
            _reject("bundle_artifact_path_duplicate")
        seen_paths.add(relative_path)
        expected_hash = _require_sha256(
            record.get("sha256"),
            code="bundle_artifact_sha256_invalid",
        )
        artifact_contracts.append((relative_path, expected_hash))

    _validate_exact_bundle_inventory(
        root,
        expected_files={"manifest.json", *seen_paths},
    )
    artifact_paths: dict[str, Path] = {}
    for relative_path, expected_hash in artifact_contracts:
        artifact_path = _validated_contained_regular_path(root, relative_path)
        artifact_paths[relative_path] = artifact_path
        if _sha256_file(artifact_path) != expected_hash:
            _reject("bundle_artifact_sha256_mismatch")

    page_dtos: list[dict[str, Any]] = []
    for page in pages:
        artifact_record = page["artifact"]
        artifact_path = artifact_paths[_safe_artifact_path(artifact_record["path"])]
        try:
            page_dto = json.loads(
                artifact_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_pairs,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise PdfVisualReviewBundleRejected("bundle_page_artifact_invalid") from exc
        if not isinstance(page_dto, dict) or page_dto.get("page_number") != page.get(
            "page_number"
        ):
            _reject("bundle_page_artifact_invalid")
        _validate_page_dto(page_dto)
        render_record = page["render"]
        annotated_record = page["annotated"]
        render_dimensions = _validated_png_dimensions(
            artifact_paths[_safe_artifact_path(render_record["path"])],
            code="bundle_render_artifact_invalid",
        )
        annotated_dimensions = _validated_png_dimensions(
            artifact_paths[_safe_artifact_path(annotated_record["path"])],
            code="bundle_annotated_artifact_invalid",
        )
        _validate_page_render_geometry(
            page_dto,
            render_record=render_record,
            render_dimensions=render_dimensions,
            annotated_dimensions=annotated_dimensions,
            render_scale=render_scale,
        )
        annotated = page.get("annotated")
        if page.get("coverage") != page_dto["coverage"]:
            _reject("bundle_page_visual_provenance_invalid")
        if (
            not isinstance(annotated, dict)
            or isinstance(annotated.get("bbox_type_label_count"), bool)
            or not isinstance(annotated.get("bbox_type_label_count"), int)
            or annotated["bbox_type_label_count"] != page_dto["counts"]["locatable"]
        ):
            _reject("bundle_page_artifact_invalid")
        if page["risk_flags"] != _page_risk_flags(
            page_dto,
            source_ir_status=manifest["pipeline"]["adapter"]["status"],
        ):
            _reject("bundle_risk_flags_invalid")
        page_dtos.append(page_dto)
    pipeline = manifest["pipeline"]
    remote_pipeline = pipeline["visual_provider"] != "off"
    _validate_page_visual_provenance(
        page_dtos,
        expected_source_sha256=source_hash,
        expected_adapter=pipeline["adapter"],
        authorization=(
            pipeline.get("visual_authorization") if remote_pipeline else None
        ),
        capture=pipeline.get("visual_capture") if remote_pipeline else None,
        document_coverage=manifest["coverage"]["document"],
        document_page_count=document["page_count"],
        full_document=scope["full_document"],
    )
    expected_document_risk_flags = _document_risk_flags(
        source_ir_status=pipeline["adapter"]["status"],
        structure_status=pipeline["resolver"]["status"],
        visual_provider=pipeline["visual_provider"],
        selected_page_numbers=tuple(selected_pages),
        page_count=document["page_count"],
        page_entries=pages,
        unassigned_blocks=coverage["unassigned_blocks"],
        unassigned_issues=coverage["unassigned_issues"],
    )
    if manifest["risk_flags"] != expected_document_risk_flags:
        _reject("bundle_risk_flags_invalid")

    index_path = artifact_paths[_safe_artifact_path(entrypoint["path"])]
    try:
        index_text = index_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PdfVisualReviewBundleRejected("bundle_index_invalid") from exc
    if _EXTERNAL_URL_PATTERN.search(index_text):
        _reject("bundle_index_external_url")
    return manifest


def _structure_only_validation_scope(manifest: dict[str, Any]) -> dict[str, Any]:
    pipeline = manifest["pipeline"]
    capture = pipeline.get("visual_capture")
    visual_table = capture.get("visual_table") if isinstance(capture, dict) else None
    visual_table_present = (
        isinstance(visual_table, dict) and visual_table.get("mode") == "REMOTE_VISUAL"
    )
    full_document = manifest["scope"]["full_document"] is True
    return {
        "renderer": {
            "status": _STRUCTURE_SCOPE_VERIFIED,
            "basis": "EXACT_RENDERER_SCHEMA_AND_DECODED_PNG_GEOMETRY",
            "authenticity_established": False,
        },
        "visual_table_summary": {
            "status": (
                _VISUAL_TABLE_EXPORTER_CLAIM if visual_table_present else "NOT_PRESENT"
            ),
            "basis": (
                "BUNDLE_V1_HAS_NO_PAGE_LEVEL_VISUAL_TABLE_SNAPSHOT_INVENTORY"
                if visual_table_present
                else "VISUAL_TABLE_MODE_OFF"
            ),
            "authenticity_established": False,
        },
        "document_coverage": {
            "status": (
                "FULL_DOCUMENT_RECOMPUTED"
                if full_document
                else _PARTIAL_COVERAGE_EXPORTER_CLAIM
            ),
            "basis": (
                "ALL_PAGE_ARTIFACTS"
                if full_document
                else "EXPORTED_PAGE_LOWER_BOUNDS_ONLY"
            ),
            "authenticity_established": False,
        },
    }


def _validated_renderer_contract(renderer: Any) -> float:
    annotator = renderer.get("annotator") if isinstance(renderer, dict) else None
    scale = renderer.get("scale") if isinstance(renderer, dict) else None
    if (
        not isinstance(renderer, dict)
        or set(renderer)
        != {
            "id",
            "version",
            "scale",
            "alpha",
            "annotations",
            "annotator",
        }
        or renderer.get("id") != "pymupdf-full-page-annotation-free"
        or not isinstance(renderer.get("version"), str)
        or not renderer["version"].strip()
        or isinstance(scale, bool)
        or not isinstance(scale, (int, float))
        or not math.isfinite(float(scale))
        or not 0.25 <= float(scale) <= MAX_RENDER_SCALE
        or renderer.get("alpha") is not False
        or renderer.get("annotations") is not False
        or not isinstance(annotator, dict)
        or set(annotator) != {"id", "version"}
        or annotator.get("id") != "pillow-bbox-type-label"
        or not isinstance(annotator.get("version"), str)
        or not annotator["version"].strip()
    ):
        _reject("bundle_renderer_contract_invalid")
    return float(scale)


def _validated_page_artifact_records(
    page: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    page_number = page.get("page_number")
    artifact = page.get("artifact")
    render = page.get("render")
    annotated = page.get("annotated")
    risk_flags = page.get("risk_flags")
    if (
        set(page)
        != {
            "page_number",
            "artifact",
            "render",
            "annotated",
            "coverage",
            "risk_flags",
        }
        or isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
        or not isinstance(page.get("coverage"), dict)
        or not _is_sorted_unique_string_list(risk_flags)
    ):
        _reject("bundle_page_inventory_invalid")
    stem = f"page-{page_number:04d}"
    if (
        not isinstance(artifact, dict)
        or set(artifact) != {"path", "sha256", "media_type"}
        or artifact.get("path") != f"pages/{stem}.json"
        or artifact.get("media_type") != "application/json"
        or not isinstance(render, dict)
        or set(render) != {"path", "sha256", "media_type", "width_px", "height_px"}
        or render.get("path") != f"renders/{stem}.png"
        or render.get("media_type") != "image/png"
        or not _is_positive_int(render.get("width_px"))
        or not _is_positive_int(render.get("height_px"))
        or not isinstance(annotated, dict)
        or set(annotated) != {"path", "sha256", "media_type", "bbox_type_label_count"}
        or annotated.get("path") != f"annotated/{stem}.png"
        or annotated.get("media_type") != "image/png"
        or not _is_nonnegative_int(annotated.get("bbox_type_label_count"))
    ):
        _reject("bundle_artifact_inventory_invalid")
    return artifact, render, annotated


def _validated_png_dimensions(path: Path, *, code: str) -> tuple[int, int]:
    if Image is None:
        _reject("bundle_render_dependencies_unavailable")
    try:
        with Image.open(path) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                _reject(code)
            dimensions = (int(image.width), int(image.height))
            if dimensions[0] < 1 or dimensions[1] < 1:
                _reject(code)
            image.verify()
        with Image.open(path) as decoded:
            if decoded.format != "PNG" or decoded.size != dimensions:
                _reject(code)
            decoded.load()
    except PdfVisualReviewBundleRejected:
        raise
    except Exception as exc:
        raise PdfVisualReviewBundleRejected(code) from exc
    return dimensions


def _validate_page_render_geometry(
    page_dto: dict[str, Any],
    *,
    render_record: dict[str, Any],
    render_dimensions: tuple[int, int],
    annotated_dimensions: tuple[int, int],
    render_scale: float,
) -> None:
    declared_dimensions = (
        int(render_record["width_px"]),
        int(render_record["height_px"]),
    )
    if render_dimensions != declared_dimensions:
        _reject("bundle_render_dimensions_invalid")
    if annotated_dimensions != render_dimensions:
        _reject("bundle_annotated_dimensions_invalid")
    bounds = _validated_bbox(
        page_dto["bounds"],
        code="bundle_page_bounds_invalid",
    )
    expected_dimensions = (
        math.ceil(bounds[2] * render_scale - 1e-9)
        - math.floor(bounds[0] * render_scale + 1e-9),
        math.ceil(bounds[3] * render_scale - 1e-9)
        - math.floor(bounds[1] * render_scale + 1e-9),
    )
    if render_dimensions != expected_dimensions:
        _reject("bundle_render_geometry_invalid")


def _verify_pipeline_contract(
    pipeline: Any,
    *,
    expected_source_sha256: str,
    page_count: int,
) -> None:
    if not isinstance(pipeline, dict):
        _reject("bundle_pipeline_contract_invalid")
    base_fields = {
        "visual_provider",
        "collection",
        "adapter",
        "resolver",
        "preview",
        "page_comparison",
    }
    selected_visual_provider = _require_visual_provider(pipeline.get("visual_provider"))
    expected_fields = (
        base_fields
        if selected_visual_provider == "off"
        else base_fields | {"visual_authorization", "visual_capture"}
    )
    adapter = pipeline.get("adapter")
    resolver = pipeline.get("resolver")
    preview = pipeline.get("preview")
    page_comparison = pipeline.get("page_comparison")
    if (
        set(pipeline) != expected_fields
        or pipeline.get("collection") != "document_outline"
        or not isinstance(adapter, dict)
        or set(adapter) != {"id", "version", "status"}
        or any(
            not isinstance(adapter.get(field), str) or not adapter[field]
            for field in ("id", "version", "status")
        )
        or not isinstance(resolver, dict)
        or set(resolver) != {"id", "version", "status"}
        or resolver.get("id") != DEFAULT_STRUCTURE_RESOLVER_ID
        or any(
            not isinstance(resolver.get(field), str) or not resolver[field]
            for field in ("version", "status")
        )
        or preview != {"schema_version": SHADOW_PREVIEW_SCHEMA_VERSION}
        or page_comparison != {"schema_version": PAGE_COMPARISON_SCHEMA_VERSION}
    ):
        _reject("bundle_pipeline_contract_invalid")
    if selected_visual_provider != "off":
        _validated_remote_visual_capture(
            {"ocr": pipeline.get("visual_capture")},
            authorization=pipeline.get("visual_authorization"),
            expected_source_sha256=expected_source_sha256,
            expected_visual_provider=selected_visual_provider,
            page_count=page_count,
        )


def _validated_preview_pages(
    preview: Any,
    *,
    selected_page_numbers: tuple[int, ...],
    expected_source_sha256: str,
    expected_visual_provider: str,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(preview, dict):
        _reject("bundle_preview_invalid")
    if (
        preview.get("schema_version") != SHADOW_PREVIEW_SCHEMA_VERSION
        or preview.get("collection") != "document_outline"
        or preview.get("visual_provider") != expected_visual_provider
        or preview.get("source_identity_match") is not True
    ):
        _reject("bundle_preview_contract_mismatch")
    source_identity = preview.get("source_identity")
    if (
        not isinstance(source_identity, dict)
        or source_identity.get("source_sha256") != expected_source_sha256
        or source_identity.get("match") is not True
    ):
        _reject("bundle_preview_source_mismatch")
    document = preview.get("document")
    if not isinstance(document, dict):
        _reject("bundle_preview_document_invalid")
    page_count = document.get("page_count")
    if (
        isinstance(page_count, bool)
        or not isinstance(page_count, int)
        or page_count < 1
    ):
        _reject("bundle_preview_page_count_invalid")
    if selected_page_numbers[-1] > page_count:
        _reject("bundle_page_range_out_of_bounds")

    outline_pagination = preview.get("document_outline_pagination")
    outline = preview.get("document_outline")
    if (
        not isinstance(outline_pagination, dict)
        or not isinstance(outline, list)
        or outline_pagination.get("has_more") is not False
        or outline_pagination.get("returned") != len(outline)
        or outline_pagination.get("total") != len(outline)
    ):
        _reject("bundle_document_outline_incomplete")

    page_comparison = preview.get("page_comparison")
    if (
        not isinstance(page_comparison, dict)
        or page_comparison.get("schema_version") != PAGE_COMPARISON_SCHEMA_VERSION
        or page_comparison.get("coordinate_space") != "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"
    ):
        _reject("bundle_page_comparison_contract_mismatch")
    pages = page_comparison.get("pages")
    if not isinstance(pages, list):
        _reject("bundle_preview_page_set_invalid")
    actual_page_numbers = [
        page.get("page_number") if isinstance(page, dict) else None for page in pages
    ]
    expected_document_pages = list(range(1, page_count + 1))
    if actual_page_numbers != expected_document_pages:
        _reject("bundle_preview_page_set_invalid")
    counts = page_comparison.get("counts")
    if (
        not isinstance(counts, dict)
        or counts.get("pages") != page_count
        or len(pages) != page_count
    ):
        _reject("bundle_preview_page_set_invalid")
    if not isinstance(page_comparison.get("unassigned_blocks"), list) or not isinstance(
        page_comparison.get("unassigned_issues"), list
    ):
        _reject("bundle_page_comparison_contract_mismatch")

    selected: list[dict[str, Any]] = []
    selected_set = set(selected_page_numbers)
    for page in pages:
        _validate_page_dto(page)
        if page["page_number"] in selected_set:
            selected.append(page)
    if [page["page_number"] for page in selected] != list(selected_page_numbers):
        _reject("bundle_export_page_set_invalid")
    return selected, page_count


def _validated_remote_visual_capture(
    preview: dict[str, Any],
    *,
    authorization: dict[str, Any] | None,
    expected_source_sha256: str,
    expected_visual_provider: str,
    page_count: int,
) -> dict[str, Any]:
    if not isinstance(authorization, dict):
        _reject("bundle_visual_authorization_invalid")
    (
        authorized_pages,
        selected_provider,
        authorized_provider_ids,
    ) = _validated_visual_authorization(
        authorization,
        expected_source_sha256=expected_source_sha256,
        expected_visual_provider=expected_visual_provider,
        page_count=page_count,
    )
    capture = preview.get("ocr")
    if not isinstance(capture, dict) or set(capture) != _CAPTURE_FIELDS:
        _reject("bundle_visual_capture_invalid")
    provider = capture.get("provider")
    route = capture.get("route")
    routing = capture.get("routing")
    coordinate_mapping = capture.get("coordinate_mapping")
    visual_table = capture.get("visual_table")
    captured_pages = capture.get("captured_pages")
    evaluated_content_types = capture.get("evaluated_content_types")
    snapshot_count = capture.get("snapshot_count")
    observation_count = capture.get("observation_count")
    authorized_page_count = capture.get("authorized_page_count")
    if (
        not isinstance(captured_pages, list)
        or any(
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number not in authorized_pages
            for page_number in captured_pages
        )
        or captured_pages != sorted(set(captured_pages))
    ):
        _reject("bundle_visual_capture_invalid")
    if (
        not isinstance(evaluated_content_types, list)
        or any(
            content_type not in _PAGE_CONTENT_TYPES
            for content_type in evaluated_content_types
        )
        or evaluated_content_types
        != [
            content_type
            for content_type in _PAGE_CONTENT_TYPES
            if content_type in evaluated_content_types
        ]
        or len(evaluated_content_types) != len(set(evaluated_content_types))
        or bool(evaluated_content_types) != bool(captured_pages)
    ):
        _reject("bundle_visual_capture_invalid")
    expected_capability = (
        "PAGE_LAYOUT"
        if evaluated_content_types == list(_PAGE_CONTENT_TYPES)
        else "TEXT_OCR"
    )
    provider_capability = (
        "PAGE_LAYOUT"
        if expected_visual_provider
        in {
            OCR_PROVIDER_BAILIAN_LAYOUT,
            OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
        }
        else "TEXT_OCR"
    )
    allowed_provider_ids = (
        route.get("allowed_provider_ids") if isinstance(route, dict) else None
    )
    visual_pages = routing.get("visual_pages") if isinstance(routing, dict) else None
    if (
        capture.get("mode") != "REMOTE_VISUAL"
        or capture.get("status") != "CAPTURED"
        or capture.get("capability") != expected_capability
        or (bool(captured_pages) and capture.get("capability") != provider_capability)
        or provider != selected_provider
        or type(authorized_page_count) is not int
        or authorized_page_count != len(authorized_pages)
        or type(snapshot_count) is not int
        or snapshot_count != len(captured_pages)
        or type(observation_count) is not int
        or observation_count < 0
        or capture.get("local_fallback") is not False
        or capture.get("implicit_fallback") is not False
        or capture.get("failure_policy") != "BLOCKING"
        or not isinstance(capture.get("message"), str)
        or not capture["message"].strip()
        or not isinstance(route, dict)
        or set(route)
        != {
            "authorization_status",
            "route_id",
            "route_sha256",
            "source_sha256",
            "authorized_pages",
            "allowed_provider_ids",
        }
        or route.get("authorization_status") != "AUTHORIZED"
        or route.get("route_id") != authorization.get("route_id")
        or route.get("route_sha256") != authorization.get("route_sha256")
        or route.get("source_sha256") != expected_source_sha256
        or route.get("authorized_pages") != authorization.get("authorized_pages")
        or not _is_sorted_unique_string_list(allowed_provider_ids)
        or any(
            provider_id not in authorized_provider_ids
            for provider_id in allowed_provider_ids
        )
        or selected_provider["id"] not in allowed_provider_ids
        or not isinstance(routing, dict)
        or set(routing)
        != {
            "policy_id",
            "status",
            "native_page_count",
            "visual_page_count",
            "visual_pages",
        }
        or routing.get("policy_id") != authorization.get("routing_policy_id")
        or routing.get("status") != "APPLIED"
        or type(routing.get("native_page_count")) is not int
        or routing.get("native_page_count") != page_count - len(captured_pages)
        or type(routing.get("visual_page_count")) is not int
        or routing.get("visual_page_count") != len(captured_pages)
        or not isinstance(visual_pages, list)
        or len(visual_pages) != len(captured_pages)
        or not isinstance(coordinate_mapping, dict)
        or set(coordinate_mapping)
        != {"provider_space", "target_space", "status"}
        or coordinate_mapping.get("provider_space")
        != "PAGE_IMAGE_TOP_LEFT_PIXELS"
        or coordinate_mapping.get("target_space")
        != "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT"
        or coordinate_mapping.get("status")
        not in {"GEOMETRY_TRANSFORM_VALIDATED", "LEGACY_BBOX_ONLY"}
        or not isinstance(visual_table, dict)
    ):
        _reject("bundle_visual_capture_invalid")
    for index, record in enumerate(visual_pages):
        reasons = record.get("reasons") if isinstance(record, dict) else None
        if (
            not isinstance(record, dict)
            or set(record) != {"page_number", "reasons"}
            or record.get("page_number") != captured_pages[index]
            or not isinstance(reasons, list)
            or not reasons
            or any(not isinstance(reason, str) or not reason for reason in reasons)
            or reasons != sorted(set(reasons))
        ):
            _reject("bundle_visual_capture_invalid")
    _validated_capture_confidence(
        capture.get("confidence"),
        observation_count=observation_count,
    )
    _validate_visual_table_capture(
        visual_table,
        captured_pages=captured_pages,
        authorized_pages=authorized_pages,
        expected_provider_id=selected_provider["id"],
    )
    return capture


def _validated_visual_authorization(
    authorization: dict[str, Any],
    *,
    expected_source_sha256: str,
    expected_visual_provider: str,
    page_count: int,
) -> tuple[list[int], dict[str, str], set[str]]:
    required_fields = {
        "schema_version",
        "authorization_status",
        "source_sha256",
        "route_id",
        "route_sha256",
        "authorized_page_count",
        "authorized_pages",
        "providers",
        "routing_policy_id",
        "remote_only",
        "local_ocr",
        "implicit_fallback",
        "selected_provider",
    }
    if (
        set(authorization) != required_fields
        or not isinstance(authorization.get("schema_version"), str)
        or not authorization["schema_version"]
        or authorization.get("authorization_status") != "AUTHORIZED"
        or authorization.get("source_sha256") != expected_source_sha256
        or not isinstance(authorization.get("route_id"), str)
        or not authorization["route_id"]
        or _require_sha256(
            authorization.get("route_sha256"),
            code="bundle_visual_authorization_invalid",
        )
        != authorization["route_sha256"]
        or not isinstance(authorization.get("routing_policy_id"), str)
        or not authorization["routing_policy_id"]
        or authorization.get("remote_only") is not True
        or authorization.get("local_ocr") is not False
        or authorization.get("implicit_fallback") is not False
    ):
        _reject("bundle_visual_authorization_invalid")

    raw_pages = authorization.get("authorized_pages")
    authorized_page_count = authorization.get("authorized_page_count")
    if (
        not isinstance(raw_pages, list)
        or isinstance(authorized_page_count, bool)
        or not isinstance(authorized_page_count, int)
        or authorized_page_count < 1
        or authorized_page_count != len(raw_pages)
    ):
        _reject("bundle_visual_authorization_invalid")
    authorized_pages: list[int] = []
    for record in raw_pages:
        if not isinstance(record, dict) or set(record) != {
            "page_number",
            "input_rotation_cw_degrees",
            "page_binding_kind",
            "page_binding_sha256",
        }:
            _reject("bundle_visual_authorization_invalid")
        page_number = record.get("page_number")
        rotation = record.get("input_rotation_cw_degrees")
        binding_kind = record.get("page_binding_kind")
        if (
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number < 1
            or page_number > page_count
            or isinstance(rotation, bool)
            or not isinstance(rotation, int)
            or rotation not in {0, 90, 180, 270}
            or binding_kind not in {"SOURCE_ASSET_SHA256", "PAGE_IMAGE_SHA256"}
            or _require_sha256(
                record.get("page_binding_sha256"),
                code="bundle_visual_authorization_invalid",
            )
            != record["page_binding_sha256"]
        ):
            _reject("bundle_visual_authorization_invalid")
        authorized_pages.append(page_number)
    if authorized_pages != sorted(set(authorized_pages)):
        _reject("bundle_visual_authorization_invalid")

    selected_provider = authorization.get("selected_provider")
    try:
        canonical_selected_provider_id = visual_text_provider_id(
            expected_visual_provider
        )
    except ValueError:
        _reject("bundle_visual_authorization_invalid")
    if (
        not isinstance(selected_provider, dict)
        or set(selected_provider) != {"name", "id", "version"}
        or selected_provider.get("name") != expected_visual_provider
        or selected_provider.get("id") != canonical_selected_provider_id
        or not isinstance(selected_provider.get("version"), str)
        or not selected_provider["version"]
    ):
        _reject("bundle_visual_authorization_invalid")
    raw_providers = authorization.get("providers")
    if not isinstance(raw_providers, list) or not raw_providers:
        _reject("bundle_visual_authorization_invalid")
    provider_names: set[str] = set()
    authorized_provider_ids: set[str] = set()
    for record in raw_providers:
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "id", "authorized", "selectable", "reason_code"}
            or not isinstance(record.get("name"), str)
            or not record["name"]
            or not isinstance(record.get("id"), str)
            or not record["id"]
            or type(record.get("authorized")) is not bool
            or type(record.get("selectable")) is not bool
            or not isinstance(record.get("reason_code"), str)
            or record["name"] in provider_names
            or (record["selectable"] and not record["authorized"])
        ):
            _reject("bundle_visual_authorization_invalid")
        try:
            canonical_provider_id = visual_text_provider_id(record["name"])
        except ValueError:
            _reject("bundle_visual_authorization_invalid")
        if record["id"] != canonical_provider_id:
            _reject("bundle_visual_authorization_invalid")
        provider_names.add(record["name"])
        if record["authorized"] and record["selectable"]:
            authorized_provider_ids.add(record["id"])
    selected_capabilities = [
        provider
        for provider in raw_providers
        if isinstance(provider, dict)
        and provider.get("name") == expected_visual_provider
        and provider.get("id") == selected_provider["id"]
    ]
    if (
        len(selected_capabilities) != 1
        or selected_capabilities[0].get("authorized") is not True
        or selected_capabilities[0].get("selectable") is not True
        or selected_provider["id"] not in authorized_provider_ids
    ):
        _reject("bundle_visual_authorization_invalid")
    return authorized_pages, selected_provider, authorized_provider_ids


def _validate_visual_table_capture(
    visual_table: dict[str, Any],
    *,
    captured_pages: list[int],
    authorized_pages: list[int],
    expected_provider_id: str,
) -> None:
    """Validate only the v1 exporter-claim shape.

    Bundle v1 does not carry page-level visual-table snapshot hashes or an
    exact per-page snapshot inventory.  The summary therefore becomes trusted
    only when the whole bundle is matched to an independently stored external
    pin; structure-only inspection reports that limitation explicitly.
    """

    if set(visual_table) != {
        "mode",
        "provider",
        "authorized_pages",
        "snapshot_count",
    }:
        _reject("bundle_visual_table_capture_invalid")
    mode = visual_table.get("mode")
    table_pages = visual_table.get("authorized_pages")
    snapshot_count = visual_table.get("snapshot_count")
    if (
        not isinstance(table_pages, list)
        or any(
            isinstance(page_number, bool)
            or not isinstance(page_number, int)
            or page_number not in authorized_pages
            for page_number in table_pages
        )
        or table_pages != sorted(set(table_pages))
        or any(page_number not in captured_pages for page_number in table_pages)
        or type(snapshot_count) is not int
        or snapshot_count < 0
    ):
        _reject("bundle_visual_table_capture_invalid")
    if mode == "OFF":
        if (
            visual_table.get("provider") is not None
            or table_pages
            or snapshot_count != 0
        ):
            _reject("bundle_visual_table_capture_invalid")
        return
    provider = visual_table.get("provider")
    if (
        mode != "REMOTE_VISUAL"
        or not isinstance(provider, dict)
        or set(provider) != {"id", "version"}
        or provider.get("id") != expected_provider_id
        or not isinstance(provider.get("version"), str)
        or not provider["version"]
        or not table_pages
        or snapshot_count < len(table_pages)
    ):
        _reject("bundle_visual_table_capture_invalid")


def _validated_capture_confidence(
    confidence: Any,
    *,
    observation_count: int,
) -> dict[str, Any]:
    if not isinstance(confidence, dict) or set(confidence) != {
        "available",
        "unavailable",
        "minimum",
        "maximum",
        "mean",
    }:
        _reject("bundle_visual_capture_invalid")
    available = confidence.get("available")
    unavailable = confidence.get("unavailable")
    if (
        type(available) is not int
        or available < 0
        or type(unavailable) is not int
        or unavailable < 0
        or available + unavailable != observation_count
    ):
        _reject("bundle_visual_capture_invalid")
    values = (
        confidence.get("minimum"),
        confidence.get("maximum"),
        confidence.get("mean"),
    )
    if available == 0:
        if values != (None, None, None):
            _reject("bundle_visual_capture_invalid")
        return confidence
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 <= float(value) <= 1
        for value in values
    ):
        _reject("bundle_visual_capture_invalid")
    minimum, maximum, mean = (float(value) for value in values)
    if minimum > mean or mean > maximum:
        _reject("bundle_visual_capture_invalid")
    return confidence


def _is_sorted_unique_string_list(value: Any) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and bool(item) for item in value)
        and value == sorted(set(value))
    )


def _validate_page_visual_provenance(
    page_dtos: list[dict[str, Any]],
    *,
    expected_source_sha256: str,
    expected_adapter: dict[str, Any],
    authorization: dict[str, Any] | None,
    capture: dict[str, Any] | None,
    document_coverage: Any,
    document_page_count: int,
    full_document: bool,
) -> None:
    if (
        not isinstance(expected_adapter, dict)
        or set(expected_adapter) != {"id", "version", "status"}
        or any(
            not isinstance(expected_adapter.get(field), str)
            or not expected_adapter[field]
            for field in ("id", "version", "status")
        )
        or type(document_page_count) is not int
        or document_page_count < 1
        or type(full_document) is not bool
    ):
        _reject("bundle_page_visual_provenance_invalid")
    if capture is not None and not full_document:
        _reject("bundle_page_visual_provenance_invalid")
    captured_pages = (
        set()
        if capture is None
        else set(int(page_number) for page_number in capture["captured_pages"])
    )
    authorized_bindings = (
        {}
        if authorization is None
        else {
            int(record["page_number"]): record
            for record in authorization["authorized_pages"]
        }
    )
    selected_provider = (
        None if authorization is None else authorization["selected_provider"]
    )
    expected_block_provider = (
        None
        if selected_provider is None
        else {
            "id": selected_provider["id"],
            "version": selected_provider["version"],
        }
    )
    evaluated_content_types = (
        [] if capture is None else list(capture["evaluated_content_types"])
    )
    observed_page_numbers: list[int] = []
    remote_observation_count = 0
    page_evaluated_type_union: set[str] = set()
    remote_confidences: list[float | None] = []
    seen_block_ids: set[str] = set()
    for page in page_dtos:
        page_number = int(page["page_number"])
        page_bounds = _validated_bbox(
            page["bounds"],
            code="bundle_page_bounds_invalid",
        )
        blocks = page.get("blocks")
        coverage = page.get("coverage")
        if not isinstance(blocks, list) or not isinstance(coverage, dict):
            _reject("bundle_page_visual_provenance_invalid")
        remote_text = coverage.get("remote_text")
        remote_visual = coverage.get("remote_visual")
        _validate_page_coverage_shape(coverage)
        page_evaluated_types = remote_visual.get("evaluated_types")
        if (
            set(remote_text) != {"status", "evaluated", "observation_count"}
            or set(remote_visual) != {"evaluated_types", "full_layout_evaluated"}
            or not isinstance(page_evaluated_types, list)
            or not isinstance(remote_visual.get("full_layout_evaluated"), bool)
            or any(
                content_type not in _PAGE_CONTENT_TYPES
                for content_type in page_evaluated_types
            )
            or page_evaluated_types
            != [
                content_type
                for content_type in _PAGE_CONTENT_TYPES
                if content_type in page_evaluated_types
            ]
            or len(page_evaluated_types) != len(set(page_evaluated_types))
        ):
            _reject("bundle_page_visual_provenance_invalid")
        if bool(page_evaluated_types) != (page_number in captured_pages) or any(
            content_type not in set(evaluated_content_types)
            for content_type in page_evaluated_types
        ):
            _reject("bundle_page_visual_provenance_invalid")
        binding = authorized_bindings.get(page_number)
        if page_number in captured_pages and binding is None:
            _reject("bundle_page_visual_provenance_invalid")
        remote_blocks: list[dict[str, Any]] = []
        for block in blocks:
            channel, confidence = _validate_page_block_provenance(
                block,
                page_number=page_number,
                page_bounds=page_bounds,
                expected_source_sha256=expected_source_sha256,
                expected_adapter=expected_adapter,
                expected_remote_provider=expected_block_provider,
                authorization_binding=binding,
                page_evaluated_types=page_evaluated_types,
                remote_capture_enabled=capture is not None,
                expected_remote_coordinate_status=(
                    None
                    if capture is None
                    else capture["coordinate_mapping"]["status"]
                ),
            )
            block_id = block["id"]
            if block_id in seen_block_ids:
                _reject("bundle_page_visual_provenance_invalid")
            seen_block_ids.add(block_id)
            if channel == "REMOTE_VISUAL":
                remote_blocks.append(block)
                remote_confidences.append(confidence)
        if (
            binding is not None
            and binding["page_binding_kind"] == "SOURCE_ASSET_SHA256"
            and _native_route_asset_hash_inventory(blocks).count(
                binding["page_binding_sha256"]
            )
            != 1
        ):
            _reject("bundle_page_visual_provenance_invalid")
        expected_coverage = _expected_page_coverage(
            blocks,
            issues=page["issues"],
            captured=page_number in captured_pages,
            evaluated_content_types=page_evaluated_types,
        )
        if coverage != expected_coverage:
            _reject("bundle_page_visual_provenance_invalid")
        _validate_page_counts_shape(page.get("counts"))
        if page["counts"] != _expected_page_counts(page):
            _reject("bundle_page_visual_provenance_invalid")
        if page_number in captured_pages:
            observed_page_numbers.append(page_number)
            page_evaluated_type_union.update(page_evaluated_types)
        remote_observation_count += len(remote_blocks)
    if observed_page_numbers != sorted(captured_pages):
        _reject("bundle_page_visual_provenance_invalid")
    ordered_evaluated_types = [
        content_type
        for content_type in _PAGE_CONTENT_TYPES
        if content_type in page_evaluated_type_union
    ]
    if capture is not None and (
        capture.get("observation_count") != remote_observation_count
        or capture.get("evaluated_content_types") != ordered_evaluated_types
        or capture.get("confidence") != _expected_capture_confidence(remote_confidences)
    ):
        _reject("bundle_page_visual_provenance_invalid")
    _validate_document_coverage(
        document_coverage,
        page_dtos=page_dtos,
        expected_source_ir_status=expected_adapter["status"],
        document_page_count=document_page_count,
        full_document=full_document,
        remote_capture_enabled=capture is not None,
    )


def _validate_page_block_provenance(
    block: dict[str, Any],
    *,
    page_number: int,
    page_bounds: tuple[float, float, float, float],
    expected_source_sha256: str,
    expected_adapter: dict[str, Any],
    expected_remote_provider: dict[str, str] | None,
    authorization_binding: dict[str, Any] | None,
    page_evaluated_types: list[str],
    remote_capture_enabled: bool,
    expected_remote_coordinate_status: str | None,
) -> tuple[str, float | None]:
    provenance = block.get("provenance")
    source_ref = block.get("source_ref")
    source_location = block.get("source_location")
    block_id = block.get("id")
    if (
        not isinstance(block_id, str)
        or not block_id
        or not isinstance(source_ref, dict)
        or set(source_ref) != {"kind", "source_id"}
        or source_ref.get("source_id") != block_id
        or not isinstance(block.get("source_kind"), str)
        or not block["source_kind"]
        or not isinstance(provenance, dict)
        or set(provenance) != _BLOCK_PROVENANCE_FIELDS
        or provenance.get("source_sha256") != expected_source_sha256
        or provenance.get("adapter")
        != {
            "id": expected_adapter["id"],
            "version": expected_adapter["version"],
        }
        or not isinstance(source_location, dict)
        or source_location.get("format") != "pdf"
        or source_location.get("page_number") != page_number
    ):
        _reject("bundle_page_visual_provenance_invalid")
    asset = _validated_block_asset(block.get("asset"))
    expected_bbox, expected_location_status = _expected_overlay_location(
        source_location,
        page_bounds=page_bounds,
    )
    if (
        block.get("bbox") != expected_bbox
        or block.get("locatable") is not (expected_bbox is not None)
        or block.get("location_status") != expected_location_status
    ):
        _reject("bundle_page_visual_provenance_invalid")
    channel = provenance.get("channel")
    if channel == "NATIVE":
        source_kind = block["source_kind"]
        if (
            _SOURCE_NODE_ID_PATTERN.fullmatch(block_id) is None
            or source_ref.get("kind") != "source_node"
            or provenance.get("fact_kind") != "SOURCE_NODE"
            or provenance.get("page_image_sha256") is not None
            or provenance.get("provider") is not None
            or provenance.get("confidence") is not None
            or source_kind not in _NATIVE_BLOCK_TYPES_BY_SOURCE_KIND
            or block.get("type") not in _NATIVE_BLOCK_TYPES_BY_SOURCE_KIND[source_kind]
            or provenance.get("coordinate_mapping")
            != {
                "source_space": "PDF_NATIVE_PAGE",
                "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
                "status": "NATIVE_PAGE_MATRIX_APPLIED",
            }
        ):
            _reject("bundle_page_visual_provenance_invalid")
        return channel, None
    if channel != "REMOTE_VISUAL" or not remote_capture_enabled:
        _reject("bundle_page_visual_provenance_invalid")
    if (
        _VISUAL_TEXT_OBSERVATION_ID_PATTERN.fullmatch(block_id) is None
        or source_ref.get("kind") != "visual_text_observation"
        or provenance.get("fact_kind") != "VISUAL_PAGE_OBSERVATION"
        or expected_remote_provider is None
        or provenance.get("provider") != expected_remote_provider
        or authorization_binding is None
        or block.get("type") not in page_evaluated_types
        or asset is not None
        or block.get("locatable") is not True
        or block.get("location_status") != "LOCATED"
        or provenance.get("coordinate_mapping")
        != {
            "source_space": "PAGE_IMAGE_TOP_LEFT_PIXELS",
            "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "status": expected_remote_coordinate_status,
        }
    ):
        _reject("bundle_page_visual_provenance_invalid")
    expected_source_kind = (
        "ocr_observation"
        if page_evaluated_types == ["TEXT"]
        else str(block["type"]).lower()
    )
    if block.get("source_kind") != expected_source_kind:
        _reject("bundle_page_visual_provenance_invalid")
    block_bbox = _validated_bbox(
        block.get("bbox"),
        code="bundle_page_visual_provenance_invalid",
    )
    source_bbox = _validated_bbox(
        source_location.get("bbox"),
        code="bundle_page_visual_provenance_invalid",
    )
    if not _bbox_is_within(block_bbox, page_bounds) or not _bbox_is_within(
        source_bbox,
        page_bounds,
    ):
        _reject("bundle_page_visual_provenance_invalid")
    page_image_sha256 = _require_sha256(
        provenance.get("page_image_sha256"),
        code="bundle_page_visual_provenance_invalid",
    )
    if (
        authorization_binding["page_binding_kind"] == "PAGE_IMAGE_SHA256"
        and page_image_sha256 != authorization_binding["page_binding_sha256"]
    ):
        _reject("bundle_page_visual_provenance_invalid")
    confidence = provenance.get("confidence")
    if confidence is None:
        return channel, None
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(float(confidence))
        or not 0 <= float(confidence) <= 1
    ):
        _reject("bundle_page_visual_provenance_invalid")
    return channel, float(confidence)


def _validated_block_asset(asset: Any) -> dict[str, Any] | None:
    if asset is None:
        return None
    if (
        not isinstance(asset, dict)
        or set(asset) != {"id", "kind", "media_type", "content_sha256"}
        or not isinstance(asset.get("id"), str)
        or not asset["id"].strip()
        or asset.get("kind") not in _SOURCE_ASSET_KINDS
        or not isinstance(asset.get("media_type"), str)
        or not asset["media_type"].strip()
        or _require_sha256(
            asset.get("content_sha256"),
            code="bundle_page_visual_provenance_invalid",
        )
        != asset.get("content_sha256")
    ):
        _reject("bundle_page_visual_provenance_invalid")
    return asset


def _expected_overlay_location(
    source_location: dict[str, Any],
    *,
    page_bounds: tuple[float, float, float, float],
) -> tuple[list[float] | None, str]:
    raw_bbox = source_location.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        return None, "BBOX_MISSING"
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        for value in raw_bbox
    ):
        return None, "BBOX_INVALID"
    bbox = tuple(float(value) for value in raw_bbox)
    if (
        any(not math.isfinite(value) for value in bbox)
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        return None, "BBOX_INVALID"
    tolerance = 0.75
    if (
        bbox[0] < page_bounds[0] - tolerance
        or bbox[1] < page_bounds[1] - tolerance
        or bbox[2] > page_bounds[2] + tolerance
        or bbox[3] > page_bounds[3] + tolerance
    ):
        return None, "BBOX_OUTSIDE_PAGE"
    return [round(value, 3) for value in bbox], "LOCATED"


def _native_route_asset_hash_inventory(
    blocks: list[dict[str, Any]],
) -> tuple[str, ...]:
    candidates: list[tuple[float, str]] = []
    for block in blocks:
        provenance = block["provenance"]
        asset = block["asset"]
        source_bbox = block["source_location"].get("bbox")
        if (
            provenance["channel"] != "NATIVE"
            or block["source_kind"] not in {"image", "figure"}
            or not isinstance(asset, dict)
            or not isinstance(source_bbox, list)
            or len(source_bbox) != 4
            or any(
                isinstance(value, bool) or not isinstance(value, (int, float))
                for value in source_bbox
            )
        ):
            continue
        bbox = tuple(float(value) for value in source_bbox)
        if (
            any(not math.isfinite(value) for value in bbox)
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
        ):
            continue
        candidates.append(
            (
                (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]),
                str(asset["content_sha256"]),
            )
        )
    if not candidates:
        return ()
    maximum_area = max(area for area, _digest in candidates)
    return tuple(
        sorted(digest for area, digest in candidates if area >= maximum_area * 0.95)
    )


def _expected_page_counts(page: dict[str, Any]) -> dict[str, Any]:
    blocks = page["blocks"]
    issues = page["issues"]
    type_counts = Counter(str(block["type"]) for block in blocks)
    role_counts = Counter(str(block["role"]) for block in blocks if block.get("role"))
    channel_counts = Counter(str(block["provenance"]["channel"]) for block in blocks)
    return {
        "blocks": len(blocks),
        "content_blocks": sum(block["type"] != "PAGE_RASTER" for block in blocks),
        "support_blocks": sum(block["type"] == "PAGE_RASTER" for block in blocks),
        "locatable": sum(block["locatable"] for block in blocks),
        "unlocatable": sum(not block["locatable"] for block in blocks),
        "issues": len(issues),
        "by_type": dict(sorted(type_counts.items())),
        "by_role": dict(sorted(role_counts.items())),
        "by_channel": dict(sorted(channel_counts.items())),
    }


def _validate_page_counts_shape(counts: Any) -> None:
    scalar_fields = {
        "blocks",
        "content_blocks",
        "support_blocks",
        "locatable",
        "unlocatable",
        "issues",
    }
    counter_fields = {"by_type", "by_role", "by_channel"}
    if (
        not isinstance(counts, dict)
        or set(counts) != scalar_fields | counter_fields
        or any(not _is_nonnegative_int(counts.get(field)) for field in scalar_fields)
    ):
        _reject("bundle_page_visual_provenance_invalid")
    for field in counter_fields:
        counter = counts.get(field)
        if not isinstance(counter, dict) or any(
            not isinstance(key, str) or not key or type(value) is not int or value < 1
            for key, value in counter.items()
        ):
            _reject("bundle_page_visual_provenance_invalid")


def _validate_page_coverage_shape(coverage: Any) -> None:
    if not isinstance(coverage, dict) or set(coverage) != {
        "content_types",
        "page_raster",
        "remote_text",
        "remote_visual",
        "text_authority",
    }:
        _reject("bundle_page_visual_provenance_invalid")
    content_types = coverage.get("content_types")
    page_raster = coverage.get("page_raster")
    remote_text = coverage.get("remote_text")
    remote_visual = coverage.get("remote_visual")
    text_authority = coverage.get("text_authority")
    if (
        not isinstance(content_types, list)
        or len(content_types) != len(_PAGE_CONTENT_TYPES)
        or not isinstance(page_raster, dict)
        or set(page_raster)
        != {"present", "block_count", "content_type", "default_visible"}
        or type(page_raster.get("present")) is not bool
        or not _is_nonnegative_int(page_raster.get("block_count"))
        or page_raster.get("content_type") is not False
        or page_raster.get("default_visible") is not False
        or not isinstance(remote_text, dict)
        or set(remote_text) != {"status", "evaluated", "observation_count"}
        or remote_text.get("status")
        not in {"OBSERVED", "NOT_OBSERVED", "NOT_EVALUATED"}
        or type(remote_text.get("evaluated")) is not bool
        or not _is_nonnegative_int(remote_text.get("observation_count"))
        or not isinstance(remote_visual, dict)
        or set(remote_visual) != {"evaluated_types", "full_layout_evaluated"}
        or type(remote_visual.get("full_layout_evaluated")) is not bool
    ):
        _reject("bundle_page_visual_provenance_invalid")
    _validate_page_text_authority_shape(text_authority)
    for index, entry in enumerate(content_types):
        channels = entry.get("channels") if isinstance(entry, dict) else None
        if (
            not isinstance(entry, dict)
            or set(entry) != {"type", "status", "observed_count", "channels", "basis"}
            or entry.get("type") != _PAGE_CONTENT_TYPES[index]
            or entry.get("status") not in {"OBSERVED", "NOT_OBSERVED", "NOT_EVALUATED"}
            or not _is_nonnegative_int(entry.get("observed_count"))
            or not _is_sorted_unique_string_list(channels)
            or any(channel not in {"NATIVE", "REMOTE_VISUAL"} for channel in channels)
            or not isinstance(entry.get("basis"), str)
            or not entry["basis"]
        ):
            _reject("bundle_page_visual_provenance_invalid")


def _validate_page_text_authority_shape(text_authority: Any) -> None:
    if (
        not isinstance(text_authority, dict)
        or set(text_authority) != {"channel", "status", "reason_codes"}
        or text_authority.get("channel") not in {"NATIVE", "REMOTE_VISUAL"}
        or text_authority.get("status")
        not in {"READY", "EMPTY", "FAILED", "PENDING"}
        or not _is_sorted_unique_string_list(text_authority.get("reason_codes"))
        or (
            text_authority.get("channel") == "NATIVE"
            and (
                text_authority.get("status") != "READY"
                or text_authority.get("reason_codes") != []
            )
        )
    ):
        _reject("bundle_page_visual_provenance_invalid")


def _expected_page_coverage(
    blocks: list[dict[str, Any]],
    *,
    issues: list[dict[str, Any]],
    captured: bool,
    evaluated_content_types: list[str],
) -> dict[str, Any]:
    type_counts = Counter(str(block["type"]) for block in blocks)
    raster_count = type_counts.get("PAGE_RASTER", 0)
    raster_page = raster_count > 0
    remote_text_count = sum(
        block["type"] == "TEXT" and block["provenance"]["channel"] == "REMOTE_VISUAL"
        for block in blocks
    )
    content_types: list[dict[str, Any]] = []
    for content_type in _PAGE_CONTENT_TYPES:
        observed_count = type_counts.get(content_type, 0)
        channels = sorted(
            {
                str(block["provenance"]["channel"])
                for block in blocks
                if block["type"] == content_type
            }
        )
        if observed_count:
            status = "OBSERVED"
            basis = "SOURCE_IR_FACTS"
        elif raster_page:
            status = (
                "NOT_OBSERVED"
                if content_type in evaluated_content_types
                else "NOT_EVALUATED"
            )
            basis = (
                "REMOTE_PAGE_LAYOUT_EVALUATED"
                if content_type in evaluated_content_types
                else "RASTER_TEXT_NOT_EVALUATED"
                if content_type == "TEXT"
                else "RASTER_LAYOUT_NOT_EVALUATED"
            )
        else:
            status = "NOT_OBSERVED"
            basis = "NATIVE_PDF_INSPECTION"
        content_types.append(
            {
                "type": content_type,
                "status": status,
                "observed_count": observed_count,
                "channels": channels,
                "basis": basis,
            }
        )
    return {
        "content_types": content_types,
        "page_raster": {
            "present": raster_page,
            "block_count": raster_count,
            "content_type": False,
            "default_visible": False,
        },
        "remote_text": {
            "status": (
                "OBSERVED"
                if remote_text_count
                else "NOT_OBSERVED"
                if captured
                else "NOT_EVALUATED"
            ),
            "evaluated": captured,
            "observation_count": remote_text_count,
        },
        "remote_visual": {
            "evaluated_types": evaluated_content_types,
            "full_layout_evaluated": evaluated_content_types
            == list(_PAGE_CONTENT_TYPES),
        },
        "text_authority": _expected_page_text_authority(
            blocks,
            issues=issues,
            evaluated_content_types=evaluated_content_types,
        ),
    }


def _expected_page_text_authority(
    blocks: list[dict[str, Any]],
    *,
    issues: list[dict[str, Any]],
    evaluated_content_types: list[str],
) -> dict[str, Any]:
    reason_codes = sorted(
        {
            str(issue["code"])
            for issue in issues
            if isinstance(issue, dict)
            and issue.get("stage") == "SOURCE_IR"
            and isinstance(issue.get("code"), str)
            and (
                issue["code"].startswith("pdf_visual_text_")
                or issue["code"] in _VISUAL_TEXT_REASON_DIAGNOSTIC_CODES
            )
        }
    )
    visual_authority = "TEXT" in evaluated_content_types or any(
        code in _VISUAL_TEXT_AUTHORITY_DIAGNOSTIC_CODES for code in reason_codes
    )
    if not visual_authority:
        return {"channel": "NATIVE", "status": "READY", "reason_codes": []}
    has_remote_text = any(
        block.get("type") == "TEXT"
        and isinstance(block.get("provenance"), dict)
        and block["provenance"].get("channel") == "REMOTE_VISUAL"
        and isinstance(block.get("text"), str)
        and bool(block["text"].strip())
        for block in blocks
    )
    failed = any(
        code in _VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES
        or code.endswith(("_failed", "_error", "_timeout"))
        for code in reason_codes
    )
    status = (
        "READY"
        if has_remote_text
        else "EMPTY"
        if "TEXT" in evaluated_content_types
        else "FAILED"
        if failed
        else "PENDING"
    )
    return {
        "channel": "REMOTE_VISUAL",
        "status": status,
        "reason_codes": reason_codes,
    }


def _expected_capture_confidence(
    confidences: list[float | None],
) -> dict[str, Any]:
    available = [value for value in confidences if value is not None]
    return {
        "available": len(available),
        "unavailable": len(confidences) - len(available),
        "minimum": round(min(available), 6) if available else None,
        "maximum": round(max(available), 6) if available else None,
        "mean": (round(sum(available) / len(available), 6) if available else None),
    }


def _expected_document_coverage(
    pages: list[dict[str, Any]],
    *,
    source_ir_status: str,
) -> dict[str, Any]:
    content_types: list[dict[str, Any]] = []
    for content_type in _PAGE_CONTENT_TYPES:
        entries = [
            entry
            for page in pages
            for entry in page["coverage"]["content_types"]
            if entry["type"] == content_type
        ]
        observed_pages = sum(entry["status"] == "OBSERVED" for entry in entries)
        not_observed_pages = sum(entry["status"] == "NOT_OBSERVED" for entry in entries)
        not_evaluated_pages = sum(
            entry["status"] == "NOT_EVALUATED" for entry in entries
        )
        if not_evaluated_pages:
            status = (
                "PARTIAL" if observed_pages or not_observed_pages else "NOT_EVALUATED"
            )
        elif observed_pages:
            status = "OBSERVED"
        else:
            status = "NOT_OBSERVED"
        content_types.append(
            {
                "type": content_type,
                "status": status,
                "observed_blocks": sum(
                    int(entry["observed_count"]) for entry in entries
                ),
                "pages_observed": observed_pages,
                "pages_not_observed": not_observed_pages,
                "pages_not_evaluated": not_evaluated_pages,
            }
        )
    remote_pages = {
        page["page_number"]
        for page in pages
        if page["coverage"]["remote_text"]["evaluated"]
    }
    remote_pages_with_observations = {
        page["page_number"]
        for page in pages
        if any(
            block["provenance"]["channel"] == "REMOTE_VISUAL"
            for block in page["blocks"]
        )
    }
    evaluated_type_union = {
        content_type
        for page in pages
        for content_type in page["coverage"]["remote_visual"]["evaluated_types"]
    }
    authority_pages = [
        page
        for page in pages
        if page["coverage"]["text_authority"]["channel"] == "REMOTE_VISUAL"
    ]
    return {
        "source_ir_status": source_ir_status,
        "content_types": content_types,
        "page_raster": {
            "pages": sum(page["coverage"]["page_raster"]["present"] for page in pages),
            "blocks": sum(
                page["coverage"]["page_raster"]["block_count"] for page in pages
            ),
            "content_type": False,
        },
        "remote_text": {
            "pages_evaluated": len(remote_pages),
            "pages_with_observations": len(remote_pages_with_observations),
            "observations": sum(
                block["provenance"]["channel"] == "REMOTE_VISUAL"
                for page in pages
                for block in page["blocks"]
            ),
            "total_pages": len(pages),
        },
        "remote_visual": {
            "pages_evaluated": len(remote_pages),
            "full_layout_pages": sum(
                page["coverage"]["remote_visual"]["full_layout_evaluated"]
                for page in pages
            ),
            "evaluated_types": [
                content_type
                for content_type in _PAGE_CONTENT_TYPES
                if content_type in evaluated_type_union
            ],
        },
        "text_authority": {
            "visual_page_count": len(authority_pages),
            "ready_page_count": sum(
                page["coverage"]["text_authority"]["status"] == "READY"
                for page in authority_pages
            ),
            "degraded_page_numbers": [
                page["page_number"]
                for page in authority_pages
                if page["coverage"]["text_authority"]["status"] != "READY"
            ],
        },
    }


def _validate_document_coverage(
    document_coverage: Any,
    *,
    page_dtos: list[dict[str, Any]],
    expected_source_ir_status: str,
    document_page_count: int,
    full_document: bool,
    remote_capture_enabled: bool,
) -> None:
    if not isinstance(document_coverage, dict):
        _reject("bundle_page_visual_provenance_invalid")
    content_types = document_coverage.get("content_types")
    page_raster = document_coverage.get("page_raster")
    remote_text = document_coverage.get("remote_text")
    remote_visual = document_coverage.get("remote_visual")
    text_authority = document_coverage.get("text_authority")
    if (
        set(document_coverage)
        != {
            "source_ir_status",
            "content_types",
            "page_raster",
            "remote_text",
            "remote_visual",
            "text_authority",
        }
        or document_coverage.get("source_ir_status") != expected_source_ir_status
        or not isinstance(content_types, list)
        or len(content_types) != len(_PAGE_CONTENT_TYPES)
        or not isinstance(page_raster, dict)
        or set(page_raster) != {"pages", "blocks", "content_type"}
        or not _is_nonnegative_int(page_raster.get("pages"))
        or page_raster["pages"] > document_page_count
        or not _is_nonnegative_int(page_raster.get("blocks"))
        or page_raster.get("content_type") is not False
        or not isinstance(remote_text, dict)
        or set(remote_text)
        != {
            "pages_evaluated",
            "pages_with_observations",
            "observations",
            "total_pages",
        }
        or any(
            not _is_nonnegative_int(remote_text.get(field))
            for field in (
                "pages_evaluated",
                "pages_with_observations",
                "observations",
                "total_pages",
            )
        )
        or remote_text["total_pages"] != document_page_count
        or remote_text["pages_evaluated"] > document_page_count
        or remote_text["pages_with_observations"] > remote_text["pages_evaluated"]
        or not isinstance(remote_visual, dict)
        or set(remote_visual)
        != {"pages_evaluated", "full_layout_pages", "evaluated_types"}
        or not _is_nonnegative_int(remote_visual.get("pages_evaluated"))
        or remote_visual["pages_evaluated"] > document_page_count
        or not _is_nonnegative_int(remote_visual.get("full_layout_pages"))
        or remote_visual["full_layout_pages"] > remote_visual["pages_evaluated"]
        or not isinstance(remote_visual.get("evaluated_types"), list)
        or any(
            content_type not in _PAGE_CONTENT_TYPES
            for content_type in remote_visual["evaluated_types"]
        )
        or remote_visual["evaluated_types"]
        != [
            content_type
            for content_type in _PAGE_CONTENT_TYPES
            if content_type in remote_visual["evaluated_types"]
        ]
        or len(remote_visual["evaluated_types"])
        != len(set(remote_visual["evaluated_types"]))
        or not isinstance(text_authority, dict)
        or set(text_authority)
        != {"visual_page_count", "ready_page_count", "degraded_page_numbers"}
        or not _is_nonnegative_int(text_authority.get("visual_page_count"))
        or text_authority["visual_page_count"] > document_page_count
        or not _is_nonnegative_int(text_authority.get("ready_page_count"))
        or text_authority["ready_page_count"] > text_authority["visual_page_count"]
        or not isinstance(text_authority.get("degraded_page_numbers"), list)
        or text_authority["degraded_page_numbers"]
        != sorted(set(text_authority["degraded_page_numbers"]))
        or any(
            not _is_positive_int(page_number)
            or page_number > document_page_count
            for page_number in text_authority["degraded_page_numbers"]
        )
        or text_authority["visual_page_count"]
        != text_authority["ready_page_count"]
        + len(text_authority["degraded_page_numbers"])
    ):
        _reject("bundle_page_visual_provenance_invalid")
    for index, entry in enumerate(content_types):
        if not isinstance(entry, dict) or set(entry) != {
            "type",
            "status",
            "observed_blocks",
            "pages_observed",
            "pages_not_observed",
            "pages_not_evaluated",
        }:
            _reject("bundle_page_visual_provenance_invalid")
        observed = entry.get("pages_observed")
        not_observed = entry.get("pages_not_observed")
        not_evaluated = entry.get("pages_not_evaluated")
        if (
            entry.get("type") != _PAGE_CONTENT_TYPES[index]
            or not _is_nonnegative_int(entry.get("observed_blocks"))
            or not _is_nonnegative_int(observed)
            or not _is_nonnegative_int(not_observed)
            or not _is_nonnegative_int(not_evaluated)
            or observed + not_observed + not_evaluated != document_page_count
        ):
            _reject("bundle_page_visual_provenance_invalid")
        expected_status = (
            "PARTIAL"
            if not_evaluated and (observed or not_observed)
            else "NOT_EVALUATED"
            if not_evaluated
            else "OBSERVED"
            if observed
            else "NOT_OBSERVED"
        )
        if entry.get("status") != expected_status:
            _reject("bundle_page_visual_provenance_invalid")
    if full_document:
        if document_coverage != _expected_document_coverage(
            page_dtos,
            source_ir_status=expected_source_ir_status,
        ):
            _reject("bundle_page_visual_provenance_invalid")
        return
    _validate_partial_document_coverage_derivable(
        document_coverage,
        page_dtos=page_dtos,
        expected_source_ir_status=expected_source_ir_status,
    )
    if (
        remote_capture_enabled
        or remote_text
        != {
            "pages_evaluated": 0,
            "pages_with_observations": 0,
            "observations": 0,
            "total_pages": document_page_count,
        }
        or remote_visual
        != {
            "pages_evaluated": 0,
            "full_layout_pages": 0,
            "evaluated_types": [],
        }
        or text_authority
        != {
            "visual_page_count": 0,
            "ready_page_count": 0,
            "degraded_page_numbers": [],
        }
    ):
        _reject("bundle_page_visual_provenance_invalid")


def _validate_partial_document_coverage_derivable(
    document_coverage: dict[str, Any],
    *,
    page_dtos: list[dict[str, Any]],
    expected_source_ir_status: str,
) -> None:
    """Validate only facts derivable from the exported subset.

    Counts for pages outside the bundle are an exporter claim and cannot be
    recomputed from a partial bundle.  For every exported page, however, the
    full-document summary must contain at least the observed blocks and page
    dispositions visible in that subset.
    """

    exported = _expected_document_coverage(
        page_dtos,
        source_ir_status=expected_source_ir_status,
    )
    exported_content = {entry["type"]: entry for entry in exported["content_types"]}
    for document_entry in document_coverage["content_types"]:
        exported_entry = exported_content[document_entry["type"]]
        if (
            document_entry["observed_blocks"] < exported_entry["observed_blocks"]
            or document_entry["pages_observed"] < exported_entry["pages_observed"]
            or document_entry["pages_not_observed"]
            < exported_entry["pages_not_observed"]
            or document_entry["pages_not_evaluated"]
            < exported_entry["pages_not_evaluated"]
            or document_entry["observed_blocks"] < document_entry["pages_observed"]
        ):
            _reject("bundle_page_visual_provenance_invalid")
    document_raster = document_coverage["page_raster"]
    exported_raster = exported["page_raster"]
    if (
        document_raster["pages"] < exported_raster["pages"]
        or document_raster["blocks"] < exported_raster["blocks"]
        or document_raster["blocks"] < document_raster["pages"]
    ):
        _reject("bundle_page_visual_provenance_invalid")
    document_authority = document_coverage["text_authority"]
    exported_authority = exported["text_authority"]
    if (
        document_authority["visual_page_count"]
        < exported_authority["visual_page_count"]
        or document_authority["ready_page_count"]
        < exported_authority["ready_page_count"]
        or not set(exported_authority["degraded_page_numbers"]).issubset(
            document_authority["degraded_page_numbers"]
        )
    ):
        _reject("bundle_page_visual_provenance_invalid")


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _bbox_is_within(
    child: tuple[float, float, float, float],
    parent: tuple[float, float, float, float],
) -> bool:
    tolerance = 0.001
    return (
        child[0] >= parent[0] - tolerance
        and child[1] >= parent[1] - tolerance
        and child[2] <= parent[2] + tolerance
        and child[3] <= parent[3] + tolerance
    )


def _validate_page_dto(page: dict[str, Any]) -> None:
    if not isinstance(page, dict):
        _reject("bundle_page_dto_invalid")
    page_number = page.get("page_number")
    if (
        set(page) != {"page_number", "bounds", "blocks", "issues", "coverage", "counts"}
        or isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number < 1
    ):
        _reject("bundle_page_dto_invalid")
    _validated_bbox(page.get("bounds"), code="bundle_page_bounds_invalid")
    blocks = page.get("blocks")
    issues = page.get("issues")
    coverage = page.get("coverage")
    counts = page.get("counts")
    if (
        not isinstance(blocks, list)
        or not isinstance(issues, list)
        or not isinstance(coverage, dict)
        or not isinstance(counts, dict)
    ):
        _reject("bundle_page_dto_invalid")
    for block in blocks:
        if (
            not isinstance(block, dict)
            or set(block) != _PAGE_BLOCK_FIELDS
            or block.get("page_number") != page_number
            or not isinstance(block.get("id"), str)
            or not block["id"]
            or not isinstance(block.get("label"), str)
            or not block["label"]
            or block.get("type") not in _PAGE_BLOCK_TYPES
            or (
                block.get("role") is not None and not isinstance(block.get("role"), str)
            )
            or type(block.get("default_visible")) is not bool
            or type(block.get("overlay_default")) is not bool
            or not _is_nonnegative_int(block.get("source_order"))
            or not isinstance(block.get("text"), str)
            or type(block.get("locatable")) is not bool
            or not isinstance(block.get("location_status"), str)
            or not block["location_status"]
            or not isinstance(block.get("source_location"), dict)
            or not isinstance(block.get("classification_basis"), str)
            or not block["classification_basis"]
            or (
                block.get("table_id") is not None
                and (
                    not isinstance(block.get("table_id"), str) or not block["table_id"]
                )
            )
            or (
                block.get("asset") is not None
                and not isinstance(block.get("asset"), dict)
            )
            or not isinstance(block.get("diagnostics"), list)
        ):
            _reject("bundle_page_block_invalid")
        if block.get("locatable") is True:
            _validated_bbox(block.get("bbox"), code="bundle_page_block_bbox_invalid")
        elif block.get("bbox") is not None:
            _reject("bundle_page_block_bbox_invalid")


def _write_page_artifacts(
    staging: Path,
    *,
    source_bytes: bytes,
    page_dtos: list[dict[str, Any]],
    render_scale: float,
    source_ir_status: str,
) -> list[dict[str, Any]]:
    if pymupdf is None or Image is None or ImageDraw is None or ImageFont is None:
        _reject("bundle_render_dependencies_unavailable")
    try:
        document = pymupdf.open(stream=source_bytes, filetype="pdf")
    except Exception as exc:
        raise PdfVisualReviewBundleRejected("bundle_pdf_open_failed") from exc
    try:
        expected_total = max(page["page_number"] for page in page_dtos)
        if document.page_count < expected_total:
            _reject("bundle_render_page_set_invalid")
        entries: list[dict[str, Any]] = []
        for page_dto in page_dtos:
            page_number = int(page_dto["page_number"])
            stem = f"page-{page_number:04d}"
            artifact_path = f"pages/{stem}.json"
            render_path = f"renders/{stem}.png"
            annotated_path = f"annotated/{stem}.png"

            artifact_bytes = _canonical_json_bytes(page_dto, trailing_newline=True)
            _write_bytes(staging / artifact_path, artifact_bytes)
            page = document.load_page(page_number - 1)
            render_bytes, width, height = _render_page_png(
                page,
                render_scale=render_scale,
            )
            _write_bytes(staging / render_path, render_bytes)
            annotated_bytes, annotated_box_count = _annotate_page_png(
                render_bytes,
                page_dto=page_dto,
                width=width,
                height=height,
            )
            _write_bytes(staging / annotated_path, annotated_bytes)
            entries.append(
                {
                    "page_number": page_number,
                    "artifact": {
                        "path": artifact_path,
                        "sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                        "media_type": "application/json",
                    },
                    "render": {
                        "path": render_path,
                        "sha256": hashlib.sha256(render_bytes).hexdigest(),
                        "media_type": "image/png",
                        "width_px": width,
                        "height_px": height,
                    },
                    "annotated": {
                        "path": annotated_path,
                        "sha256": hashlib.sha256(annotated_bytes).hexdigest(),
                        "media_type": "image/png",
                        "bbox_type_label_count": annotated_box_count,
                    },
                    "coverage": page_dto["coverage"],
                    "risk_flags": _page_risk_flags(
                        page_dto,
                        source_ir_status=source_ir_status,
                    ),
                }
            )
        return entries
    except PdfVisualReviewBundleRejected:
        raise
    except Exception as exc:
        raise PdfVisualReviewBundleRejected("bundle_page_render_failed") from exc
    finally:
        document.close()


def _render_page_png(
    page: Any,
    *,
    render_scale: float,
) -> tuple[bytes, int, int]:
    matrix = pymupdf.Matrix(render_scale, render_scale)
    pixmap = page.get_pixmap(
        matrix=matrix,
        alpha=False,
        annots=False,
    )
    width = int(pixmap.width)
    height = int(pixmap.height)
    if width < 1 or height < 1:
        _reject("bundle_page_render_invalid")
    payload = bytes(pixmap.tobytes("png"))
    if not payload:
        _reject("bundle_page_render_invalid")
    return payload, width, height


def _annotate_page_png(
    render_bytes: bytes,
    *,
    page_dto: dict[str, Any],
    width: int,
    height: int,
) -> tuple[bytes, int]:
    bounds = _validated_bbox(
        page_dto["bounds"],
        code="bundle_page_bounds_invalid",
    )
    try:
        image = Image.open(BytesIO(render_bytes)).convert("RGB")
        image.load()
    except Exception as exc:
        raise PdfVisualReviewBundleRejected("bundle_annotation_input_invalid") from exc
    if image.size != (width, height):
        _reject("bundle_annotation_dimensions_invalid")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()
    line_width = max(2, int(round(min(width, height) / 500)))
    scale_x = width / (bounds[2] - bounds[0])
    scale_y = height / (bounds[3] - bounds[1])
    annotated = 0
    for block in page_dto["blocks"]:
        if block.get("locatable") is not True:
            continue
        bbox = _validated_bbox(
            block.get("bbox"),
            code="bundle_page_block_bbox_invalid",
        )
        pixel_left = max(
            0,
            min(width - 1, round((bbox[0] - bounds[0]) * scale_x)),
        )
        pixel_top = max(
            0,
            min(height - 1, round((bbox[1] - bounds[1]) * scale_y)),
        )
        pixel_right = max(
            pixel_left + 1,
            min(width, round((bbox[2] - bounds[0]) * scale_x)),
        )
        pixel_bottom = max(
            pixel_top + 1,
            min(height, round((bbox[3] - bounds[1]) * scale_y)),
        )
        pixel_bbox = (
            pixel_left,
            pixel_top,
            pixel_right,
            pixel_bottom,
        )
        block_type = str(block.get("type") or "UNKNOWN").upper()
        color = _COLORS_BY_TYPE.get(block_type, _COLORS_BY_TYPE["UNKNOWN"])
        draw.rectangle(pixel_bbox, outline=color, width=line_width)
        label = f"{block_type} {str(block.get('label') or '').strip()}".strip()
        text_bbox = draw.textbbox((0, 0), label, font=font)
        label_width = max(1, text_bbox[2] - text_bbox[0]) + 8
        label_height = max(1, text_bbox[3] - text_bbox[1]) + 6
        label_left = pixel_bbox[0]
        label_top = max(0, pixel_bbox[1] - label_height)
        label_right = min(width, label_left + label_width)
        draw.rectangle(
            (label_left, label_top, label_right, label_top + label_height),
            fill=color,
        )
        draw.text(
            (label_left + 4, label_top + 3),
            label,
            fill="#ffffff",
            font=font,
        )
        annotated += 1
    output = BytesIO()
    image.save(output, format="PNG", compress_level=6)
    payload = output.getvalue()
    if not payload:
        _reject("bundle_annotation_output_invalid")
    return payload, annotated


def _page_risk_flags(
    page: dict[str, Any],
    *,
    source_ir_status: str,
) -> list[str]:
    flags: list[str] = []
    counts = page["counts"]
    coverage = page["coverage"]
    if source_ir_status != "COMPLETE":
        flags.append("SOURCE_IR_NOT_COMPLETE")
    if int(counts.get("content_blocks") or 0) == 0:
        flags.append("NO_CONTENT_FACTS_OBSERVED")
    if int(counts.get("unlocatable") or 0):
        flags.append("UNLOCATABLE_FACTS_PRESENT")
    if int(counts.get("issues") or 0):
        flags.append("PAGE_ISSUES_PRESENT")
    if coverage.get("page_raster", {}).get("present") is True:
        flags.append("PAGE_RASTER_PRESENT")
    if int(counts.get("by_type", {}).get("FIGURE") or 0) >= (
        _EXCESSIVE_FIGURE_FRAGMENT_COUNT
    ):
        flags.append("EXCESSIVE_FIGURE_FRAGMENTATION")
    if _has_page_spanning_table(page):
        flags.append("PAGE_SPANNING_TABLE_REVIEW_REQUIRED")
    for entry in coverage.get("content_types", []):
        if isinstance(entry, dict) and entry.get("status") == "NOT_EVALUATED":
            content_type = str(entry.get("type") or "UNKNOWN").upper()
            flags.append(f"{content_type}_NOT_EVALUATED")
    return sorted(set(flags))


def _has_page_spanning_table(page: dict[str, Any]) -> bool:
    bounds = page.get("bounds")
    blocks = page.get("blocks")
    if (
        not isinstance(bounds, (list, tuple))
        or len(bounds) != 4
        or not isinstance(blocks, list)
    ):
        return False
    try:
        page_x0, page_y0, page_x1, page_y1 = (
            float(coordinate) for coordinate in bounds
        )
    except (TypeError, ValueError, OverflowError):
        return False
    page_area = (page_x1 - page_x0) * (page_y1 - page_y0)
    if not math.isfinite(page_area) or page_area <= 0:
        return False
    for block in blocks:
        if (
            not isinstance(block, dict)
            or block.get("type") != "TABLE"
            or block.get("locatable") is not True
        ):
            continue
        bbox = block.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            x0, y0, x1, y1 = (float(coordinate) for coordinate in bbox)
        except (TypeError, ValueError, OverflowError):
            continue
        table_area = (x1 - x0) * (y1 - y0)
        if (
            math.isfinite(table_area)
            and table_area > 0
            and table_area / page_area >= _PAGE_SPANNING_TABLE_AREA_RATIO
        ):
            return True
    return False


def _document_risk_flags(
    *,
    source_ir_status: str,
    structure_status: str,
    visual_provider: str,
    selected_page_numbers: tuple[int, ...],
    page_count: int,
    page_entries: list[dict[str, Any]],
    unassigned_blocks: int,
    unassigned_issues: int,
) -> list[str]:
    flags = {
        "CUSTOMER_CONTENT_PRESENT",
        "NON_HUMAN_TECHNICAL_DRAFT",
        "NOT_FORMALLY_APPROVED",
    }
    if visual_provider == "off":
        flags.add("REMOTE_VISUAL_DISABLED")
    if selected_page_numbers != tuple(range(1, page_count + 1)):
        flags.add("PARTIAL_DOCUMENT_PAGE_RANGE")
    if source_ir_status != "COMPLETE":
        flags.add("SOURCE_IR_NOT_COMPLETE")
    if structure_status != "COMPLETE":
        flags.add("STRUCTURE_REVIEW_REQUIRED")
    if unassigned_blocks:
        flags.add("UNASSIGNED_FACTS_PRESENT")
    if unassigned_issues:
        flags.add("UNASSIGNED_ISSUES_PRESENT")
    if any(page["risk_flags"] for page in page_entries):
        flags.add("PAGE_LEVEL_RISKS_PRESENT")
    return sorted(flags)


def _offline_index_html(
    *,
    document_id: str,
    source_sha256: str,
    page_entries: list[dict[str, Any]],
    document_risk_flags: list[str],
) -> bytes:
    page_links = "\n".join(
        (f'<a href="#page-{entry["page_number"]}">P{entry["page_number"]}</a>')
        for entry in page_entries
    )
    page_sections = "\n".join(_offline_page_section(entry) for entry in page_entries)
    risk_text = ", ".join(document_risk_flags) or "NONE"
    styles = """
:root { color-scheme: light; font-family: Inter, system-ui, sans-serif; }
* { box-sizing: border-box; }
body { margin: 0; color: #172033; background: #eef2f7; }
header { position: sticky; top: 0; z-index: 10; padding: 16px 22px;
  color: #f8fafc; background: #172033; box-shadow: 0 2px 12px #0003; }
h1 { margin: 0 0 8px; font-size: 20px; }
.warning { margin: 10px 0; padding: 10px 12px; color: #7f1d1d;
  background: #fee2e2; border: 2px solid #ef4444; border-radius: 8px; }
.meta { display: grid; gap: 4px; font: 12px ui-monospace, monospace; }
nav { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
nav a, .dto-link { color: #bfdbfe; }
main { max-width: 1800px; margin: 0 auto; padding: 20px; }
.page { margin: 0 0 28px; padding: 18px; background: #fff;
  border: 1px solid #cbd5e1; border-radius: 12px; }
.page-head { display: flex; flex-wrap: wrap; justify-content: space-between;
  align-items: baseline; gap: 12px; margin-bottom: 12px; }
.page-head h2 { margin: 0; }
.page-head .dto-link { color: #1d4ed8; }
.risk { color: #9f1239; font: 12px ui-monospace, monospace; }
.views { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px; }
figure { margin: 0; }
figcaption { margin-bottom: 6px; font-weight: 700; }
img { display: block; width: 100%; height: auto; border: 1px solid #94a3b8;
  background: white; }
@media (max-width: 900px) { .views { grid-template-columns: 1fr; } }
"""
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PDF visual review draft</title>
  <style>{styles}</style>
</head>
<body>
  <header>
    <h1>PDF visual review bundle</h1>
    <div class="warning">CUSTOMER CONTENT - CONTROLLED LOCAL REVIEW ONLY.
    Technical draft; NOT Human Gold; NOT a formal approval; NOT a serving gate.</div>
    <div class="meta">
      <span>Document ID: {html.escape(document_id)}</span>
      <span>Source SHA-256: {html.escape(source_sha256)}</span>
      <span>Risk flags: {html.escape(risk_text)}</span>
    </div>
    <nav>{page_links}</nav>
  </header>
  <main>{page_sections}</main>
</body>
</html>
"""
    if _EXTERNAL_URL_PATTERN.search(document):
        _reject("bundle_index_external_url")
    return document.encode("utf-8")


def _offline_page_section(entry: dict[str, Any]) -> str:
    page_number = int(entry["page_number"])
    risk_flags = ", ".join(entry["risk_flags"]) or "NONE"
    artifact_path = html.escape(str(entry["artifact"]["path"]), quote=True)
    render_path = html.escape(str(entry["render"]["path"]), quote=True)
    annotated_path = html.escape(str(entry["annotated"]["path"]), quote=True)
    return f"""
<section class="page" id="page-{page_number}">
  <div class="page-head">
    <h2>Page {page_number}</h2>
    <a class="dto-link" href="{artifact_path}">Raw page DTO JSON</a>
    <span class="risk">Risk flags: {html.escape(risk_flags)}</span>
  </div>
  <div class="views">
    <figure>
      <figcaption>Original annotation-free full-page render</figcaption>
      <img src="{render_path}" alt="Original full-page render for page {page_number}"
        loading="lazy">
    </figure>
    <figure>
      <figcaption>SourceIR bbox and type labels</figcaption>
      <img src="{annotated_path}" alt="Annotated full-page render for page {page_number}"
        loading="lazy">
    </figure>
  </div>
</section>
"""


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _canonical_json_bytes(value: Any, *, trailing_newline: bool) -> bytes:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if trailing_newline:
        serialized += "\n"
    return serialized.encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    _write_bytes(
        path,
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n",
    )


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _reject("bundle_staging_artifact_exists")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_staging_write_failed") from exc


def _safe_artifact_path(value: Any) -> str:
    token = str(value or "")
    path = PurePosixPath(token)
    if (
        not token
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in token
        or token != path.as_posix()
    ):
        _reject("bundle_artifact_path_invalid")
    return path.as_posix()


def _validate_exact_bundle_inventory(
    root: Path,
    *,
    expected_files: set[str],
) -> None:
    expected_directories: set[str] = set()
    for relative_path in expected_files:
        parts = PurePosixPath(relative_path).parts[:-1]
        for length in range(1, len(parts) + 1):
            expected_directories.add(PurePosixPath(*parts[:length]).as_posix())

    actual_files: set[str] = set()
    actual_directories: set[str] = set()

    def walk(directory: Path, relative_parts: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as entries:
                ordered_entries = sorted(entries, key=lambda entry: entry.name)
        except OSError as exc:
            raise PdfVisualReviewBundleRejected("bundle_inventory_invalid") from exc
        for entry in ordered_entries:
            relative = PurePosixPath(*relative_parts, entry.name).as_posix()
            try:
                metadata = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PdfVisualReviewBundleRejected("bundle_inventory_invalid") from exc
            if stat.S_ISLNK(metadata.st_mode):
                _reject("bundle_inventory_invalid")
            if stat.S_ISDIR(metadata.st_mode):
                actual_directories.add(relative)
                walk(Path(entry.path), (*relative_parts, entry.name))
            elif stat.S_ISREG(metadata.st_mode):
                actual_files.add(relative)
            else:
                _reject("bundle_inventory_invalid")

    walk(root, ())
    if actual_files != expected_files or actual_directories != expected_directories:
        _reject("bundle_inventory_invalid")


def _validated_contained_regular_path(root: Path, relative_path: str) -> Path:
    canonical_path = _safe_artifact_path(relative_path)
    try:
        root_resolved = root.resolve(strict=True)
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_artifact_unavailable") from exc
    current = root
    parts = PurePosixPath(canonical_path).parts
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise PdfVisualReviewBundleRejected("bundle_artifact_unavailable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            _reject("bundle_artifact_path_invalid")
        if index < len(parts) - 1:
            if not stat.S_ISDIR(metadata.st_mode):
                _reject("bundle_artifact_path_invalid")
        elif not stat.S_ISREG(metadata.st_mode):
            _reject("bundle_artifact_unavailable")
    try:
        resolved = current.resolve(strict=True)
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_artifact_unavailable") from exc
    if not resolved.is_relative_to(root_resolved):
        _reject("bundle_artifact_path_invalid")
    return current


def _validated_bbox(value: Any, *, code: str) -> tuple[float, float, float, float]:
    if not isinstance(value, list) or len(value) != 4:
        _reject(code)
    if any(
        isinstance(item, bool) or not isinstance(item, (int, float)) for item in value
    ):
        _reject(code)
    bbox = tuple(float(item) for item in value)
    if (
        any(not math.isfinite(item) for item in bbox)
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        _reject(code)
    return bbox  # type: ignore[return-value]


def _require_document_id(value: Any) -> str:
    token = str(value or "").strip()
    if _DOCUMENT_ID_PATTERN.fullmatch(token) is None:
        _reject("bundle_document_id_invalid")
    return token


def _require_sha256(value: Any, *, code: str = "bundle_source_hash_invalid") -> str:
    token = str(value or "").strip().lower()
    if _SHA256_PATTERN.fullmatch(token) is None:
        _reject(code)
    return token


def _require_page_range(page_start: Any, page_end: Any) -> tuple[int, ...]:
    if (
        isinstance(page_start, bool)
        or not isinstance(page_start, int)
        or isinstance(page_end, bool)
        or not isinstance(page_end, int)
        or page_start < 1
        or page_end < page_start
    ):
        _reject("bundle_page_range_invalid")
    return tuple(range(page_start, page_end + 1))


def _require_render_scale(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject("bundle_render_scale_invalid")
    scale = float(value)
    if not math.isfinite(scale) or not 0.25 <= scale <= MAX_RENDER_SCALE:
        _reject("bundle_render_scale_invalid")
    return scale


def _require_visual_provider(value: Any) -> str:
    if not isinstance(value, str):
        _reject("bundle_visual_provider_invalid")
    selected = value.strip().lower()
    if selected != "off" and selected not in SUPPORTED_PDF_VISUAL_PROVIDER_NAMES:
        _reject("bundle_visual_provider_invalid")
    return selected


def _require_regular_file(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_source_unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        _reject("bundle_source_unavailable")
    return candidate


def _require_corpus_root(path: Path) -> Path:
    candidate = path.expanduser().resolve()
    try:
        metadata = candidate.stat()
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_corpus_root_unavailable") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        _reject("bundle_corpus_root_unavailable")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise PdfVisualReviewBundleRejected("bundle_source_unavailable") from exc
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _reject(code: str) -> None:
    raise PdfVisualReviewBundleRejected(code)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export an offline, source-bound PDF visual review draft. "
            "The result contains customer content and is not Gold or approval."
        )
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--page-start", required=True, type=int)
    parser.add_argument("--page-end", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--corpus-root", required=True, type=Path)
    parser.add_argument(
        "--render-scale",
        type=float,
        default=DEFAULT_RENDER_SCALE,
    )
    parser.add_argument(
        "--visual-provider",
        choices=["off", *sorted(SUPPORTED_PDF_VISUAL_PROVIDER_NAMES)],
        default="off",
        help=(
            "Use only an exact source/page-hash-authorized remote visual route; "
            "defaults to native-only extraction."
        ),
    )
    parser.add_argument(
        "--visual-registry-path",
        type=Path,
        help=(
            "Optional controlled route registry override. It is rejected when "
            "--visual-provider=off."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        manifest = export_pdf_visual_review_bundle(
            args.source,
            document_id=args.document_id,
            expected_source_sha256=args.expected_source_sha256,
            page_start=args.page_start,
            page_end=args.page_end,
            output_dir=args.output_dir,
            corpus_root=args.corpus_root,
            render_scale=args.render_scale,
            visual_provider=args.visual_provider,
            visual_registry_path=args.visual_registry_path,
        )
    except CorpusPathError:
        print("bundle_output_inside_corpus", file=sys.stderr)
        return 2
    except PdfVisualReviewBundleRejected as exc:
        detail = f":{exc.cause_code}" if exc.cause_code else ""
        print(f"{exc.code}{detail}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": "WRITTEN",
                "bundle_sha256": manifest["bundle_sha256"],
                "page_numbers": manifest["scope"]["page_numbers"],
            },
            sort_keys=True,
        )
    )
    return 0


__all__ = [
    "BUNDLE_HASH_CONTRACT",
    "DEFAULT_RENDER_SCALE",
    "PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION",
    "STRUCTURE_ONLY_INSPECTION_STATUS",
    "PdfVisualReviewBundleRejected",
    "canonical_bundle_sha256",
    "export_pdf_visual_review_bundle",
    "inspect_pdf_visual_review_bundle",
    "inspect_pdf_visual_review_bundle_structure_only",
    "main",
    "verify_pdf_visual_review_bundle",
]


if __name__ == "__main__":
    raise SystemExit(main())
