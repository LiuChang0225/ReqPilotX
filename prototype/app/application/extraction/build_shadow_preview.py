from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping
from collections import Counter
from enum import Enum
from os import PathLike
from pathlib import Path
from time import perf_counter
from typing import Any

from app.adapters.documents.pdf_source_crop_renderer import (
    PdfSourceCropRejected,
    inspect_pdf_page_bounds,
)
from app.adapters.documents.pdf_visual_text_runtime import (
    PDF_VISUAL_ROUTING_POLICY_ID,
    SUPPORTED_PDF_VISUAL_PROVIDER_NAMES,
    PdfVisualTextRuntime,
    pdf_visual_text_routing_reasons,
    validate_pdf_visual_text_capture,
    visual_text_provider_id,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_source_ir import (
    SourceIRBuildRejected,
    build_source_ir,
)
from app.application.extraction.classify_content import (
    SemanticClassificationRejected,
    classify_content,
)
from app.application.extraction.locate_llm_scope import LlmSectionScopeSelector
from app.application.extraction.project_requirements import (
    RequirementProjectionRejected,
    project_requirements,
)
from app.application.extraction.resolve_structure import (
    StructureResolutionRejected,
    _is_toc_heading_text,
    build_structure_tree,
    superseded_native_text_source_ids,
    visual_text_authority_pages,
)
from app.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    validate_structure_snapshot_against_source_ir,
)
from app.domain.extraction.requirement_view import RequirementViewNode
from app.domain.extraction.pdf_page_furniture import (
    PdfPageFurnitureKind,
    pdf_image_furniture_kind_from_diagnostic_codes,
)
from app.domain.extraction.pdf_page_raster import (
    collapse_pdf_page_raster_blocks,
    proven_pdf_page_raster_source_ids,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceNodeKind,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureFinding,
    StructureNode,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
    VisualTableCellTextBinding,
)


SHADOW_PREVIEW_SCHEMA_VERSION = "extraction-preview/v6"
PAGE_COMPARISON_SCHEMA_VERSION = "document-page-comparison/v5"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS = 0.75
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
_CHINESE_OUTLINE_PREFIX = re.compile(
    r"^\s*(?P<label>(?:第\s*)?[零〇一二三四五六七八九十百两]{1,6}(?:\s*章|\s*[、．.]))"
)


class ShadowPreviewRejectionCode(str, Enum):
    INVALID_PAGINATION = "invalid_pagination"
    UNSUPPORTED_COLLECTION = "unsupported_collection"
    UNSUPPORTED_VISUAL_PROVIDER = "unsupported_visual_provider"
    UNSUPPORTED_DOCUMENT_FORMAT = "unsupported_document_format"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    PIPELINE_REJECTED = "pipeline_rejected"
    UNRESOLVED_SOURCE_REFERENCE = "unresolved_source_reference"


class ShadowPreviewRejected(RuntimeError):
    """Safe failure returned by the read-only Shadow experience boundary."""

    def __init__(
        self,
        code: ShadowPreviewRejectionCode,
        message: str,
        *,
        stage: str = "request",
        cause_code: str = "",
    ) -> None:
        self.code = code
        self.stage = stage
        self.cause_code = cause_code
        super().__init__(message)


def build_extraction_shadow_preview(
    source_path: str | PathLike[str],
    *,
    expected_source_sha256: str = "",
    visual_provider: str = "off",
    visual_runtime: PdfVisualTextRuntime | None = None,
    visual_authorization: Mapping[str, Any] | None = None,
    collection: str = "full",
    offset: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    """Build a frontend DTO without persisting or serving formal requirements.

    Remote visual capture is accepted only when the outer service supplies an
    exact source/page-hash runtime. Local OCR and implicit provider fallback
    are not part of this use case.
    """

    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.INVALID_PAGINATION,
            "Shadow preview offset must be a non-negative integer.",
        )
    response_collection = str(collection or "").strip().lower()
    if response_collection not in {"full", "document_outline"}:
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.UNSUPPORTED_COLLECTION,
            "Shadow preview collection must be 'full' or 'document_outline'.",
        )
    max_limit = 5_000 if response_collection == "document_outline" else 1_000
    if (
        not isinstance(limit, int)
        or isinstance(limit, bool)
        or limit < 1
        or limit > max_limit
    ):
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.INVALID_PAGINATION,
            f"Shadow preview limit must be between 1 and {max_limit}.",
        )
    selected_visual_provider = str(visual_provider or "").strip().lower()
    if selected_visual_provider == "off":
        if visual_runtime is not None or visual_authorization is not None:
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
                "A remote visual runtime cannot be attached to an OCR-off request.",
            )
    elif (
        selected_visual_provider not in SUPPORTED_PDF_VISUAL_PROVIDER_NAMES
        or visual_runtime is None
        or visual_authorization is None
    ):
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
            "Remote visual recognition requires an approved provider and an exact authorized route.",
        )
    else:
        runtime_provider = visual_runtime.visual_text_provider
        try:
            expected_provider_id = visual_text_provider_id(selected_visual_provider)
        except ValueError as exc:  # guarded above; keep the boundary fail-closed
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
                "The selected visual provider is not approved.",
            ) from exc
        if (
            runtime_provider is None
            or runtime_provider.provider_id != expected_provider_id
            or not visual_runtime.route.page_numbers
        ):
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
                "The supplied remote visual runtime does not match the selected provider route.",
            )

    expected_hash = str(expected_source_sha256 or "").strip().lower()
    if expected_hash and not _SHA256_PATTERN.fullmatch(expected_hash):
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            "The stored source identity is invalid and cannot authorize a Shadow preview.",
            stage="preflight",
        )

    path = Path(source_path)
    stage_started = perf_counter()
    report = LocalDocumentPreflight().inspect(path)
    if report.identity.detected_format.value != "pdf":
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.UNSUPPORTED_DOCUMENT_FORMAT,
            "The current Shadow experience is PDF-first; Word and Excel remain queued next.",
            stage="preflight",
        )
    if expected_hash and report.source_sha256 != expected_hash:
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            "The source file changed after upload, so the Shadow preview was stopped.",
            stage="preflight",
        )
    if (
        visual_runtime is not None
        and visual_runtime.route.source_sha256 != report.source_sha256
    ):
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            "The remote visual route does not match the current source bytes.",
            stage="preflight",
        )
    if visual_authorization is not None and (
        str(visual_authorization.get("authorization_status") or "").upper()
        != "AUTHORIZED"
        or str(visual_authorization.get("source_sha256") or "").lower()
        != report.source_sha256
    ):
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
            "The remote visual authorization metadata is not bound to this source.",
            stage="preflight",
        )

    timings: dict[str, float] = {}
    try:
        source_ir = build_source_ir(
            path,
            preflight_report=report,
            adapters=(visual_runtime.pdf_adapter,)
            if visual_runtime is not None
            else None,
        )
    except SourceIRBuildRejected as exc:
        raise _pipeline_rejection("source_ir", exc) from exc
    if visual_runtime is not None:
        try:
            validate_pdf_visual_text_capture(source_ir, visual_runtime)
        except RuntimeError as exc:
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.PIPELINE_REJECTED,
                "The remote visual capture did not satisfy its exact authorized route.",
                stage="visual_capture",
                cause_code=str(exc or "pdf_visual_text_capture_invalid"),
            ) from exc
    timings["source_ir"] = _elapsed_ms(stage_started)

    stage_started = perf_counter()
    try:
        structure = build_structure_tree(source_ir)
    except StructureResolutionRejected as exc:
        raise _pipeline_rejection("structure", exc) from exc
    timings["structure"] = _elapsed_ms(stage_started)

    stage_started = perf_counter()
    try:
        page_bounds = inspect_pdf_page_bounds(
            path,
            expected_source_sha256=report.source_sha256,
        )
    except PdfSourceCropRejected as exc:
        raise ShadowPreviewRejected(
            ShadowPreviewRejectionCode.PIPELINE_REJECTED,
            "The read-only extraction pipeline could not establish PDF page geometry.",
            stage="presenter",
            cause_code=exc.code.value,
        ) from exc
    timings["page_geometry"] = _elapsed_ms(stage_started)

    semantics = None
    requirement_view = None
    timings["semantic"] = 0.0
    timings["requirement_view"] = 0.0
    if response_collection == "full":
        stage_started = perf_counter()
        try:
            semantics = classify_content(source_ir, structure)
        except SemanticClassificationRejected as exc:
            raise _pipeline_rejection("semantic", exc) from exc
        timings["semantic"] = _elapsed_ms(stage_started)

        stage_started = perf_counter()
        try:
            requirement_view = project_requirements(source_ir, structure, semantics)
        except RequirementProjectionRejected as exc:
            raise _pipeline_rejection("requirement_view", exc) from exc
        timings["requirement_view"] = _elapsed_ms(stage_started)

    semantic_annotations = semantics.annotations if semantics is not None else ()
    requirement_nodes = requirement_view.nodes if requirement_view is not None else ()

    document_view = build_document_view(source_ir, structure)
    source_catalog = document_view["source_catalog"]
    table_catalog = document_view["tables"]
    document_outline = document_view["document_outline"]
    page_comparison = build_page_comparison_view(
        source_ir,
        structure,
        page_bounds=page_bounds,
    )
    tables_by_id = {table["id"]: table for table in table_catalog}
    structure_nodes = {node.structure_node_id: node for node in structure.nodes}
    items: list[dict[str, Any]] = []
    requirement_groups: list[dict[str, Any]] = []
    review_groups: list[dict[str, Any]] = []
    if response_collection == "full":
        annotations = {
            annotation.semantic_annotation_id: annotation
            for annotation in semantic_annotations
        }
        view_nodes = {node.requirement_view_node_id: node for node in requirement_nodes}
        depths = _view_depths(view_nodes)
        findings_by_candidate = _findings_by_candidate(
            requirement_nodes,
            structure.findings,
        )
        items = [
            _serialize_candidate(
                node,
                depth=depths[node.requirement_view_node_id],
                source_catalog=source_catalog,
                structure_nodes=structure_nodes,
                annotations=annotations,
                findings=findings_by_candidate[node.requirement_view_node_id],
            )
            for node in requirement_nodes
        ]
        requirement_groups = _serialize_requirement_groups(
            requirement_nodes,
            serialized_items={item["id"]: item for item in items},
            structure_nodes=structure_nodes,
            semantic_annotations=semantic_annotations,
            source_catalog=source_catalog,
            tables_by_id=tables_by_id,
        )
        review_groups = _serialize_review_groups(
            requirement_groups=requirement_groups,
            structure_nodes=structure.nodes,
            semantic_annotations=semantic_annotations,
            projected_structure_node_ids={
                node.target_structure_node_id for node in requirement_nodes
            },
            source_catalog=source_catalog,
            tables_by_id=tables_by_id,
        )
    paged_items = items[offset : offset + limit]
    paged_requirement_groups = requirement_groups[offset : offset + limit]
    paged_review_groups = review_groups[offset : offset + limit]
    paged_document_outline = document_outline[offset : offset + limit]
    toc_regions = [
        item for item in document_outline if item["document_region"] == "TOC"
    ]
    review_status_counts = Counter(group["review_status"] for group in review_groups)
    locatable_page_start, locatable_page_end = _locatable_page_range(source_catalog)

    source_diagnostics = [
        {
            "code": diagnostic.code,
            "severity": diagnostic.severity.value,
            "message": diagnostic.message,
            "retryable": diagnostic.retryable,
            "location": _locator_payload(diagnostic.native_locator),
        }
        for diagnostic in source_ir.diagnostics
    ]
    serialized_findings = [
        _serialize_finding(finding, source_catalog) for finding in structure.findings
    ]

    role_counts = Counter(node.role.value for node in requirement_nodes)
    decision_counts = Counter(node.decision_mode.value for node in requirement_nodes)
    semantic_class_counts = Counter(
        annotation.semantic_class.value for annotation in semantic_annotations
    )
    semantic_status_counts = Counter(
        annotation.status.value for annotation in semantic_annotations
    )
    finding_severity_counts = Counter(
        finding.severity.value for finding in structure.findings
    )
    blocking_findings = sum(finding.blocking for finding in structure.findings)
    unresolved_annotations = semantic_class_counts.get("UNRESOLVED", 0)
    visual_observations = sum(
        len(snapshot.observations) for snapshot in source_ir.visual_text_snapshots
    )
    overall_status = (
        "REVIEW_REQUIRED"
        if not expected_hash
        or source_ir.status.value != "COMPLETE"
        or blocking_findings
        or (response_collection == "full" and unresolved_annotations)
        else "READY"
    )

    return {
        "schema_version": SHADOW_PREVIEW_SCHEMA_VERSION,
        "mode": "READ_ONLY_SHADOW",
        "collection": response_collection,
        "status": overall_status,
        "serving_impact": "NONE",
        "visual_provider": selected_visual_provider,
        "requirement_set_id": None,
        "requirement_set_created": False,
        "requirement_set_activated": False,
        "source_identity_match": True if expected_hash else None,
        "source_identity": {
            "status": "MATCHED" if expected_hash else "COMPUTED_FOR_RUN",
            "stored_hash_available": bool(expected_hash),
            "match": True if expected_hash else None,
            "source_sha256": report.source_sha256,
        },
        "document": {
            "format": report.identity.detected_format.value,
            "page_count": report.page_count,
            "max_page_number": report.page_count or locatable_page_end,
            "locatable_page_start": locatable_page_start,
            "locatable_page_end": locatable_page_end,
        },
        "presentation": {
            "default_collection": "document_outline",
            "fidelity_collection": "document_outline",
            "page_comparison_schema": PAGE_COMPARISON_SCHEMA_VERSION,
            "recognized_requirement_collection": "requirement_groups",
            "diagnostic_collection": "items",
            "response_collection": response_collection,
            "omitted_collections": (
                ["items", "review_groups", "requirement_groups"]
                if response_collection == "document_outline"
                else []
            ),
        },
        "pipeline": {
            "source_ir": {
                "label": "SourceIR",
                "id": source_ir.adapter_id,
                "status": source_ir.status.value,
                "version": source_ir.adapter_version,
                "duration_ms": timings["source_ir"],
                "counts": {
                    "nodes": len(source_ir.nodes),
                    "assets": len(source_ir.assets),
                    "diagnostics": len(source_ir.diagnostics),
                    "visual_observations": visual_observations,
                },
            },
            "structure": {
                "label": "Structure",
                "status": "REVIEW_REQUIRED" if structure.findings else "COMPLETE",
                "version": structure.resolver_version,
                "duration_ms": timings["structure"],
                "counts": {
                    "nodes": len(structure.nodes),
                    "relations": len(structure.relations),
                    "findings": len(structure.findings),
                    "blocking_findings": blocking_findings,
                },
            },
            "semantic": {
                "label": "Semantic",
                "status": (
                    "SKIPPED_NOT_REQUESTED"
                    if semantics is None
                    else "REVIEW_REQUIRED"
                    if unresolved_annotations
                    else "COMPLETE"
                ),
                "version": semantics.classifier_version
                if semantics is not None
                else None,
                "duration_ms": timings["semantic"],
                "counts": {
                    "annotations": len(semantic_annotations),
                    "unresolved": unresolved_annotations,
                },
            },
            "requirement_view": {
                "label": (
                    "RequirementView"
                    if requirement_view is not None
                    else "DocumentView"
                ),
                "status": (
                    "REVIEW_REQUIRED"
                    if overall_status == "REVIEW_REQUIRED"
                    else "SHADOW_READY"
                ),
                "version": (
                    requirement_view.projector_version
                    if requirement_view is not None
                    else SHADOW_PREVIEW_SCHEMA_VERSION
                ),
                "duration_ms": timings["requirement_view"],
                "counts": {
                    "nodes": (
                        len(requirement_nodes)
                        if requirement_view is not None
                        else len(document_outline)
                    ),
                    "requirement_groups": len(requirement_groups),
                    "review_groups": len(review_groups),
                    "document_outline": len(document_outline),
                    "page_blocks": page_comparison["counts"]["blocks"],
                    "content_page_blocks": page_comparison["counts"]["content_blocks"],
                },
            },
        },
        "counts": {
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_status_counts.get("REQUIREMENT", 0),
            "needs_classification": review_status_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_status_counts.get("INFORMATION", 0),
            "requirement_groups": len(requirement_groups),
            "review_groups": len(review_groups),
            "document_outline": len(document_outline),
            "page_blocks": page_comparison["counts"]["blocks"],
            "content_page_blocks": page_comparison["counts"]["content_blocks"],
            "page_raster_blocks": page_comparison["counts"]["support_blocks"],
            "unlocatable_page_blocks": page_comparison["counts"]["unlocatable"],
            "toc_blocks": len(toc_regions),
            "tables": len(table_catalog),
            "roles": dict(sorted(role_counts.items())),
            "decision_modes": dict(sorted(decision_counts.items())),
            "semantic_classes": dict(sorted(semantic_class_counts.items())),
            "semantic_statuses": dict(sorted(semantic_status_counts.items())),
            "finding_severities": dict(sorted(finding_severity_counts.items())),
            "blocking_findings": blocking_findings,
        },
        "ocr": _ocr_payload(
            source_ir,
            selected_visual_provider=selected_visual_provider,
            visual_runtime=visual_runtime,
            visual_authorization=visual_authorization,
            page_count=int(report.page_count or 0),
        ),
        "pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(paged_items),
            "total": len(items),
            "has_more": offset + len(paged_items) < len(items),
        },
        "requirement_group_pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(paged_requirement_groups),
            "total": len(requirement_groups),
            "has_more": (
                offset + len(paged_requirement_groups) < len(requirement_groups)
            ),
        },
        "review_group_pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(paged_review_groups),
            "total": len(review_groups),
            "has_more": offset + len(paged_review_groups) < len(review_groups),
        },
        "document_outline_pagination": {
            "offset": offset,
            "limit": limit,
            "returned": len(paged_document_outline),
            "total": len(document_outline),
            "has_more": offset + len(paged_document_outline) < len(document_outline),
        },
        "review_counts": {
            "total": len(review_groups),
            "recognized_requirement_groups": len(requirement_groups),
            "requirement": review_status_counts.get("REQUIREMENT", 0),
            "needs_classification": review_status_counts.get("NEEDS_CLASSIFICATION", 0),
            "information": review_status_counts.get("INFORMATION", 0),
        },
        "review_groups": paged_review_groups,
        "document_outline": paged_document_outline,
        "page_comparison": page_comparison,
        "document_regions": {"toc": toc_regions},
        "requirement_groups": paged_requirement_groups,
        "tables": table_catalog,
        "items": paged_items,
        "findings": serialized_findings,
        "diagnostics": source_diagnostics,
    }


def _pipeline_rejection(stage: str, exc: Exception) -> ShadowPreviewRejected:
    cause = getattr(exc, "code", "")
    cause_code = cause.value if isinstance(cause, Enum) else str(cause or "")
    return ShadowPreviewRejected(
        ShadowPreviewRejectionCode.PIPELINE_REJECTED,
        f"The read-only extraction pipeline stopped at {stage}.",
        stage=stage,
        cause_code=cause_code,
    )


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 1)


def _ocr_payload(
    snapshot: SourceIRSnapshot,
    *,
    selected_visual_provider: str,
    visual_runtime: PdfVisualTextRuntime | None,
    visual_authorization: Mapping[str, Any] | None,
    page_count: int,
) -> dict[str, Any]:
    observation_count = sum(
        len(item.observations) for item in snapshot.visual_text_snapshots
    )
    geometry_mapping_status = (
        "GEOMETRY_TRANSFORM_VALIDATED"
        if observation_count > 0
        and all(
            observation.geometry_receipt is not None
            and observation.geometry_receipt.validation_status
            == "GEOMETRY_TRANSFORM_VALIDATED"
            for visual_snapshot in snapshot.visual_text_snapshots
            for observation in visual_snapshot.observations
        )
        else "LEGACY_BBOX_ONLY"
    )
    if visual_runtime is None:
        return {
            "mode": "OFF",
            "status": "NOT_REQUESTED",
            "provider": None,
            "snapshot_count": 0,
            "observation_count": observation_count,
            "authorized_page_count": 0,
            "captured_pages": [],
            "routing": {
                "policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
                "status": "DISABLED",
                "native_page_count": page_count,
                "visual_page_count": 0,
                "visual_pages": [],
            },
            "confidence": {
                "available": 0,
                "unavailable": 0,
                "minimum": None,
                "maximum": None,
                "mean": None,
            },
            "route": None,
            "coordinate_mapping": None,
            "visual_table": {
                "mode": "OFF",
                "snapshot_count": len(snapshot.visual_table_snapshots),
            },
            "local_fallback": False,
            "implicit_fallback": False,
            "failure_policy": "BLOCKING",
            "message": (
                "Native PDF extraction only. Remote visual recognition was not requested."
            ),
        }

    provider = visual_runtime.visual_text_provider
    if provider is None:  # validated before extraction; keep serialization safe
        raise AssertionError("visual runtime lost its provider")
    routing_reasons = pdf_visual_text_routing_reasons(snapshot)
    visual_pages = [
        {
            "page_number": page_number,
            "reasons": list(reasons),
        }
        for page_number, reasons in routing_reasons.items()
    ]
    confidences = [
        float(observation.confidence)
        for visual_snapshot in snapshot.visual_text_snapshots
        for observation in visual_snapshot.observations
        if observation.confidence is not None
    ]
    unavailable_confidences = observation_count - len(confidences)
    evaluated_content_types = [
        content_type
        for content_type in _PAGE_CONTENT_TYPES
        if any(
            content_type in visual_snapshot.evaluated_content_types
            for visual_snapshot in snapshot.visual_text_snapshots
        )
    ]
    full_page_layout = evaluated_content_types == list(_PAGE_CONTENT_TYPES)
    authorization = visual_authorization or {}
    authorized_pages = authorization.get("authorized_pages")
    if not isinstance(authorized_pages, list):
        authorized_pages = [
            {
                "page_number": page_number,
                "input_rotation_cw_degrees": binding.input_rotation_cw_degrees,
                "page_binding_kind": (
                    "SOURCE_ASSET_SHA256"
                    if binding.source_asset_sha256 is not None
                    else "PAGE_IMAGE_SHA256"
                ),
                "page_binding_sha256": (
                    binding.source_asset_sha256 or binding.page_image_sha256
                ),
            }
            for page_number, binding in visual_runtime.route.page_bindings.items()
        ]
    route_payload = {
        "authorization_status": "AUTHORIZED",
        "route_id": str(authorization.get("route_id") or "") or None,
        "route_sha256": str(authorization.get("route_sha256") or "") or None,
        "source_sha256": visual_runtime.route.source_sha256,
        "authorized_pages": authorized_pages,
        "allowed_provider_ids": sorted(
            visual_runtime.route.allowed_visual_provider_ids
        ),
    }
    visual_table_provider = visual_runtime.visual_table_provider
    return {
        "mode": "REMOTE_VISUAL",
        "capability": "PAGE_LAYOUT" if full_page_layout else "TEXT_OCR",
        "status": "CAPTURED",
        "provider": {
            "name": selected_visual_provider,
            "id": provider.provider_id,
            "version": provider.provider_version,
        },
        "snapshot_count": len(snapshot.visual_text_snapshots),
        "observation_count": observation_count,
        "authorized_page_count": len(visual_runtime.route.page_numbers),
        "captured_pages": sorted(
            visual_snapshot.page_number
            for visual_snapshot in snapshot.visual_text_snapshots
        ),
        "routing": {
            "policy_id": PDF_VISUAL_ROUTING_POLICY_ID,
            "status": "APPLIED",
            "native_page_count": max(0, page_count - len(visual_pages)),
            "visual_page_count": len(visual_pages),
            "visual_pages": visual_pages,
        },
        "evaluated_content_types": evaluated_content_types,
        "confidence": {
            "available": len(confidences),
            "unavailable": unavailable_confidences,
            "minimum": round(min(confidences), 6) if confidences else None,
            "maximum": round(max(confidences), 6) if confidences else None,
            "mean": (
                round(sum(confidences) / len(confidences), 6) if confidences else None
            ),
        },
        "route": route_payload,
        "coordinate_mapping": {
            "provider_space": "PAGE_IMAGE_TOP_LEFT_PIXELS",
            "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "status": geometry_mapping_status,
        },
        "visual_table": {
            "mode": "REMOTE_VISUAL" if visual_table_provider is not None else "OFF",
            "provider": (
                {
                    "id": visual_table_provider.provider_id,
                    "version": visual_table_provider.provider_version,
                }
                if visual_table_provider is not None
                else None
            ),
            "authorized_pages": list(snapshot.visual_table_page_numbers),
            "snapshot_count": len(snapshot.visual_table_snapshots),
        },
        "local_fallback": False,
        "implicit_fallback": False,
        "failure_policy": "BLOCKING",
        "message": (
            "The native-first page router sent only required, exact "
            "source/page-hash-authorized pages to the selected "
            + (
                "remote page-layout provider; six content types were evaluated "
                "in one call per page and native facts remain immutable."
                if full_page_layout
                else "remote text provider; native facts remain immutable."
            )
        ),
    }


def _build_source_catalog(
    snapshot: SourceIRSnapshot,
) -> dict[tuple[str, str], dict[str, Any]]:
    catalog: dict[tuple[str, str], dict[str, Any]] = {}
    authority_pages = visual_text_authority_pages(snapshot)
    superseded_native_source_ids = superseded_native_text_source_ids(snapshot)
    for node in snapshot.nodes:
        native_text_superseded = node.source_node_id in superseded_native_source_ids
        native_table_text_unresolved = (
            node.table_payload is not None
            and node.native_locator.page_number in authority_pages
        )
        adopted_text_suppressed = (
            native_text_superseded or native_table_text_unresolved
        )
        source_fact = {
            "kind": "source_node",
            "source_id": node.source_node_id,
            "source_kind": node.kind.value,
            "source_order": node.source_order,
            "text": "" if adopted_text_suppressed else _source_node_display_text(node),
            "text_source": (
                "superseded_native_audit_only"
                if native_text_superseded
                else "unresolved_ocr_authority"
                if native_table_text_unresolved
                else "native_source_fact"
            ),
            "location": _locator_payload(node.native_locator),
        }
        if node.table_payload is not None:
            source_fact["table_id"] = node.table_payload.table_id
        catalog[("source_node", node.source_node_id)] = source_fact
        if node.table_payload is not None:
            for cell in node.table_payload.cells:
                cell_page = (
                    cell.native_locator.page_number
                    or node.native_locator.page_number
                )
                visual_authority = cell_page in authority_pages
                catalog[("table_cell", cell.cell_id)] = {
                    "kind": "table_cell",
                    "source_id": cell.cell_id,
                    "source_kind": "cell",
                    "source_order": node.source_order,
                    "text": (
                        ""
                        if visual_authority
                        else cell.raw_text or cell.display_value or cell.formula or ""
                    ),
                    "text_source": (
                        "unresolved_ocr_authority"
                        if visual_authority
                        else "native_table_cell"
                    ),
                    "location": _locator_payload(cell.native_locator),
                    "table_id": node.table_payload.table_id,
                    "table": {
                        "row": cell.row,
                        "column": cell.column,
                        "row_span": cell.row_span,
                        "column_span": cell.column_span,
                    },
                }
    for visual_snapshot in snapshot.visual_text_snapshots:
        for observation in visual_snapshot.observations:
            table_id = (
                _visual_layout_table_id(observation.observation_id)
                if observation.content_type == "TABLE"
                and observation.layout_table is not None
                else None
            )
            catalog[("visual_text_observation", observation.observation_id)] = {
                "kind": "visual_text_observation",
                "source_id": observation.observation_id,
                "source_kind": (
                    "ocr_observation"
                    if visual_snapshot.evaluated_content_types == ("TEXT",)
                    else observation.content_type.lower()
                ),
                "source_order": None,
                "text": observation.raw_text,
                "location": {
                    "format": "pdf",
                    "page_number": observation.page_number,
                    "bbox": list(observation.bbox),
                },
                "provider": {
                    "id": observation.provider_id,
                    "version": observation.provider_version,
                    "confidence": observation.confidence,
                },
                "content_type": observation.content_type,
                "role": observation.role,
                "table_id": table_id,
            }
    return catalog


def build_document_view(
    snapshot: SourceIRSnapshot,
    structure: StructureSnapshot,
) -> dict[str, Any]:
    """Build the pure DocumentView used by both preview and formal evaluation.

    The returned source catalog remains an in-memory presenter input; only
    ``tables`` and ``document_outline`` are emitted by the preview response.
    Reusing this function lets evaluators inspect the same presentation model
    without reparsing the source PDF.
    """

    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    require_structure_snapshot(structure)
    validate_structure_snapshot_against_source_ir(structure, snapshot)
    source_catalog = _build_source_catalog(snapshot)
    tables = _build_table_catalog(
        snapshot,
        structure_nodes=structure.nodes,
        structure_relations=structure.relations,
        visual_table_cell_text_bindings=(structure.visual_table_cell_text_bindings),
    )
    tables.extend(_build_visual_layout_table_catalog(snapshot))
    tables_by_id = {table["id"]: table for table in tables}
    return {
        "source_catalog": source_catalog,
        "tables": tables,
        "document_outline": _serialize_document_outline(
            structure_nodes=structure.nodes,
            source_catalog=source_catalog,
            tables_by_id=tables_by_id,
        ),
    }


_PAGE_BLOCK_TYPES = {
    SourceNodeKind.HEADING: "TEXT",
    SourceNodeKind.PARAGRAPH: "TEXT",
    SourceNodeKind.LIST: "TEXT",
    SourceNodeKind.LIST_ITEM: "TEXT",
    SourceNodeKind.NOTE: "TEXT",
    SourceNodeKind.TABLE: "TABLE",
    SourceNodeKind.IMAGE: "FIGURE",
    SourceNodeKind.FIGURE: "FIGURE",
    SourceNodeKind.HEADER: "HEADER",
    SourceNodeKind.FOOTER: "FOOTER",
    SourceNodeKind.WATERMARK: "WATERMARK",
    SourceNodeKind.TOC_ENTRY: "TEXT",
}
_PAGE_BLOCK_PREFIXES = {
    "TEXT": "TXT",
    "TABLE": "TBL",
    "FIGURE": "FIG",
    "TOC": "TOC",
    "HEADER": "HDR",
    "FOOTER": "FTR",
    "WATERMARK": "WMR",
    "PAGE_RASTER": "PGR",
    "UNKNOWN": "UNK",
}
_PAGE_CONTENT_TYPES = (
    "TEXT",
    "TABLE",
    "FIGURE",
    "HEADER",
    "FOOTER",
    "WATERMARK",
)
def build_page_comparison_view(
    snapshot: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    page_bounds: tuple[tuple[float, float, float, float], ...],
) -> dict[str, Any]:
    """Project immutable source facts into a page-aligned inspection DTO.

    The view does not invent geometry.  A source fact without a reliable bbox
    remains visible in the page result with an explicit non-locatable status,
    while only validated boxes are sent to the permanent PDF overlay.
    """

    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    require_structure_snapshot(structure)
    validate_structure_snapshot_against_source_ir(structure, snapshot)

    normalized_page_bounds = _normalize_page_bounds(page_bounds)
    bounds_by_page = {
        page_number: bbox
        for page_number, bbox in enumerate(normalized_page_bounds, start=1)
    }
    toc_source_ids = {
        ref.source_id
        for structure_node in structure.nodes
        if structure_node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        for ref in structure_node.source_refs
        if ref.kind is StructureSourceRefKind.SOURCE_NODE
    }
    assets_by_id = {asset.asset_id: asset for asset in snapshot.assets}
    page_raster_source_ids = proven_pdf_page_raster_source_ids(snapshot)
    remote_text_pages = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
    }
    remote_evaluated_types_by_page: dict[int, set[str]] = {}
    for visual_snapshot in snapshot.visual_text_snapshots:
        remote_evaluated_types_by_page.setdefault(
            visual_snapshot.page_number,
            set(),
        ).update(visual_snapshot.evaluated_content_types)
    visual_structure_by_observation_id: dict[str, StructureNode] = {}
    for structure_node in sorted(
        structure.nodes,
        key=lambda item: (item.source_order, item.structure_node_id),
    ):
        for ref in structure_node.source_refs:
            if ref.kind is StructureSourceRefKind.VISUAL_TEXT_OBSERVATION:
                visual_structure_by_observation_id.setdefault(
                    ref.source_id,
                    structure_node,
                )
    label_counters: Counter[str] = Counter()
    page_blocks: dict[int, list[dict[str, Any]]] = {
        page_number: [] for page_number in bounds_by_page
    }
    unassigned_blocks: list[dict[str, Any]] = []

    for node in sorted(
        snapshot.nodes,
        key=lambda item: (item.source_order, item.source_node_id),
    ):
        if node.kind in {SourceNodeKind.TABLE_ROW, SourceNodeKind.CELL}:
            continue
        locator = (
            node.visual_locator
            if node.visual_locator is not None and node.visual_locator.bbox is not None
            else node.native_locator
        )
        source_location = _locator_payload(locator)
        page_number = _page_from_location(source_location)
        overlay_bbox, location_status = _overlay_bbox(
            source_location,
            page_bounds=bounds_by_page.get(page_number or 0),
        )
        asset = assets_by_id.get(node.asset_ref or "")
        block_role = (
            "TOC"
            if node.source_node_id in toc_source_ids
            or node.kind is SourceNodeKind.TOC_ENTRY
            else None
        )
        block_type = _PAGE_BLOCK_TYPES.get(node.kind, "UNKNOWN")
        image_furniture_kind = pdf_image_furniture_kind_from_diagnostic_codes(
            diagnostic.code for diagnostic in node.extraction_diagnostics
        )
        if image_furniture_kind is PdfPageFurnitureKind.HEADER:
            block_type = "HEADER"
        elif image_furniture_kind is PdfPageFurnitureKind.FOOTER:
            block_type = "FOOTER"
        if node.source_node_id in page_raster_source_ids:
            block_type = "PAGE_RASTER"
            block_role = "SCAN_LAYER"
        label_kind = "TOC" if block_role == "TOC" else block_type
        label_counters[label_kind] += 1
        block = {
            "id": node.source_node_id,
            "source_ref": {
                "kind": "source_node",
                "source_id": node.source_node_id,
            },
            "label": (
                f"{_PAGE_BLOCK_PREFIXES[label_kind]}-{label_counters[label_kind]:03d}"
            ),
            "type": block_type,
            "role": block_role,
            "default_visible": block_type != "PAGE_RASTER",
            "overlay_default": block_type != "PAGE_RASTER",
            "source_kind": node.kind.value,
            "source_order": node.source_order,
            "text": _source_node_display_text(node),
            "page_number": page_number,
            "bbox": overlay_bbox,
            "locatable": overlay_bbox is not None,
            "location_status": location_status,
            "source_location": source_location,
            "classification_basis": (
                "structure:toc_block"
                if block_role == "TOC"
                else "presenter:full_page_scan_layer"
                if block_type == "PAGE_RASTER"
                else "source_ir:repeated_image_furniture"
                if image_furniture_kind is not None
                else f"source_ir:{node.kind.value}"
            ),
            "provenance": {
                "channel": "NATIVE",
                "fact_kind": "SOURCE_NODE",
                "source_sha256": snapshot.source_sha256,
                "page_image_sha256": None,
                "adapter": {
                    "id": snapshot.adapter_id,
                    "version": snapshot.adapter_version,
                },
                "provider": None,
                "confidence": None,
                "coordinate_mapping": {
                    "source_space": "PDF_NATIVE_PAGE",
                    "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
                    "status": "NATIVE_PAGE_MATRIX_APPLIED",
                },
            },
            "table_id": (
                node.table_payload.table_id if node.table_payload is not None else None
            ),
            "asset": (
                {
                    "id": asset.asset_id,
                    "kind": asset.kind.value,
                    "media_type": asset.media_type,
                    "content_sha256": asset.content_sha256,
                }
                if asset is not None
                else None
            ),
            "diagnostics": [
                {
                    "code": diagnostic.code,
                    "severity": diagnostic.severity.value,
                    "message": diagnostic.message,
                    "retryable": diagnostic.retryable,
                }
                for diagnostic in node.extraction_diagnostics
            ],
        }
        if page_number in page_blocks:
            page_blocks[page_number].append(block)
        else:
            unassigned_blocks.append(block)

    visual_source_order = (
        max((node.source_order for node in snapshot.nodes), default=-1) + 1
    )
    for visual_snapshot in snapshot.visual_text_snapshots:
        for observation in visual_snapshot.observations:
            structure_node = visual_structure_by_observation_id.get(
                observation.observation_id
            )
            block_role = (
                "TOC"
                if observation.role == "TOC"
                or (
                    structure_node is not None
                    and structure_node.boundary_basis
                    is StructureBoundaryBasis.TOC_BLOCK
                )
                else None
            )
            block_type = observation.content_type
            label_kind = "TOC" if block_role == "TOC" else block_type
            label_counters[label_kind] += 1
            source_location = {
                "format": "pdf",
                "page_number": observation.page_number,
                "bbox": list(observation.bbox),
            }
            overlay_bbox, location_status = _overlay_bbox(
                source_location,
                page_bounds=bounds_by_page.get(observation.page_number),
            )
            block = {
                "id": observation.observation_id,
                "source_ref": {
                    "kind": "visual_text_observation",
                    "source_id": observation.observation_id,
                },
                "label": (
                    f"{_PAGE_BLOCK_PREFIXES[label_kind]}-"
                    f"{label_counters[label_kind]:03d}"
                ),
                "type": block_type,
                "role": block_role,
                "default_visible": True,
                "overlay_default": True,
                "source_kind": (
                    "ocr_observation"
                    if visual_snapshot.evaluated_content_types == ("TEXT",)
                    else observation.content_type.lower()
                ),
                "source_order": visual_source_order,
                "text": observation.raw_text,
                "page_number": observation.page_number,
                "bbox": overlay_bbox,
                "locatable": overlay_bbox is not None,
                "location_status": location_status,
                "source_location": source_location,
                "classification_basis": (
                    "provider:toc_role"
                    if observation.role == "TOC"
                    else f"structure:{structure_node.boundary_basis.value}"
                    if structure_node is not None
                    else f"provider:page_layout:{observation.content_type}"
                ),
                "provenance": {
                    "channel": "REMOTE_VISUAL",
                    "fact_kind": "VISUAL_PAGE_OBSERVATION",
                    "source_sha256": observation.source_sha256,
                    "page_image_sha256": observation.page_image_sha256,
                    "adapter": {
                        "id": snapshot.adapter_id,
                        "version": snapshot.adapter_version,
                    },
                    "provider": {
                        "id": observation.provider_id,
                        "version": observation.provider_version,
                    },
                    "confidence": observation.confidence,
                    "coordinate_mapping": {
                        "source_space": "PAGE_IMAGE_TOP_LEFT_PIXELS",
                        "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
                        "status": (
                            observation.geometry_receipt.validation_status
                            if observation.geometry_receipt is not None
                            else "LEGACY_BBOX_ONLY"
                        ),
                    },
                },
                "table_id": (
                    _visual_layout_table_id(observation.observation_id)
                    if observation.content_type == "TABLE"
                    and observation.layout_table is not None
                    else None
                ),
                "asset": None,
                "diagnostics": [],
            }
            visual_source_order += 1
            if observation.page_number in page_blocks:
                page_blocks[observation.page_number].append(block)
            else:
                unassigned_blocks.append(block)

    issues = _page_comparison_issues(
        snapshot,
        structure,
        bounds_by_page=bounds_by_page,
    )
    issues_by_page: dict[int, list[dict[str, Any]]] = {
        page_number: [] for page_number in bounds_by_page
    }
    unassigned_issues: list[dict[str, Any]] = []
    for issue in issues:
        page_number = issue.get("page_number")
        if page_number in issues_by_page:
            issues_by_page[page_number].append(issue)
        else:
            unassigned_issues.append(issue)

    pages: list[dict[str, Any]] = []
    for page_number, bounds in bounds_by_page.items():
        blocks = collapse_pdf_page_raster_blocks(
            page_blocks[page_number],
            page_bbox=bounds,
        )
        page_blocks[page_number] = blocks
        page_issues = issues_by_page[page_number]
        type_counts = Counter(block["type"] for block in blocks)
        role_counts = Counter(
            str(block["role"]) for block in blocks if block.get("role")
        )
        channel_counts = Counter(
            str(block["provenance"].get("channel") or "UNKNOWN") for block in blocks
        )
        coverage = _page_content_coverage(
            blocks,
            page_number=page_number,
            remote_text_pages=remote_text_pages,
            remote_evaluated_types=remote_evaluated_types_by_page.get(
                page_number,
                set(),
            ),
        )
        coverage["text_authority"] = _page_text_authority(
            snapshot,
            page_number=page_number,
        )
        pages.append(
            {
                "page_number": page_number,
                "bounds": list(bounds),
                "blocks": blocks,
                "issues": page_issues,
                "coverage": coverage,
                "counts": {
                    "blocks": len(blocks),
                    "content_blocks": sum(
                        block["type"] != "PAGE_RASTER" for block in blocks
                    ),
                    "support_blocks": sum(
                        block["type"] == "PAGE_RASTER" for block in blocks
                    ),
                    "locatable": sum(block["locatable"] for block in blocks),
                    "unlocatable": sum(not block["locatable"] for block in blocks),
                    "issues": len(page_issues),
                    "by_type": dict(sorted(type_counts.items())),
                    "by_role": dict(sorted(role_counts.items())),
                    "by_channel": dict(sorted(channel_counts.items())),
                },
            }
        )

    all_blocks = [block for blocks in page_blocks.values() for block in blocks]
    all_blocks.extend(unassigned_blocks)
    type_counts = Counter(block["type"] for block in all_blocks)
    role_counts = Counter(
        str(block["role"]) for block in all_blocks if block.get("role")
    )
    channel_counts = Counter(
        str(block["provenance"].get("channel") or "UNKNOWN") for block in all_blocks
    )
    document_coverage = _document_content_coverage(
        pages,
        snapshot=snapshot,
    )
    return {
        "schema_version": PAGE_COMPARISON_SCHEMA_VERSION,
        "coordinate_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
        "interaction": "PERSISTENT_LABELED_BOXES",
        "downstream_identity": "SOURCE_IR_FACT_ID",
        "content_type_contract": list(_PAGE_CONTENT_TYPES),
        "coverage": document_coverage,
        "pages": pages,
        "unassigned_blocks": unassigned_blocks,
        "unassigned_issues": unassigned_issues,
        "counts": {
            "pages": len(pages),
            "blocks": len(all_blocks),
            "content_blocks": sum(
                block["type"] != "PAGE_RASTER" for block in all_blocks
            ),
            "support_blocks": sum(
                block["type"] == "PAGE_RASTER" for block in all_blocks
            ),
            "locatable": sum(block["locatable"] for block in all_blocks),
            "unlocatable": sum(not block["locatable"] for block in all_blocks),
            "issues": len(issues),
            "by_type": dict(sorted(type_counts.items())),
            "by_role": dict(sorted(role_counts.items())),
            "by_channel": dict(sorted(channel_counts.items())),
        },
    }
def _page_content_coverage(
    blocks: list[dict[str, Any]],
    *,
    page_number: int,
    remote_text_pages: set[int],
    remote_evaluated_types: set[str],
) -> dict[str, Any]:
    type_counts = Counter(block["type"] for block in blocks)
    raster_count = type_counts.get("PAGE_RASTER", 0)
    raster_page = raster_count > 0
    remote_text_count = sum(
        block["type"] == "TEXT"
        and block["provenance"].get("channel") == "REMOTE_VISUAL"
        for block in blocks
    )
    content_types: list[dict[str, Any]] = []
    for content_type in _PAGE_CONTENT_TYPES:
        observed_count = type_counts.get(content_type, 0)
        channels = sorted(
            {
                str(block["provenance"].get("channel") or "UNKNOWN")
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
                if content_type in remote_evaluated_types
                else "NOT_EVALUATED"
            )
            basis = (
                "REMOTE_PAGE_LAYOUT_EVALUATED"
                if content_type in remote_evaluated_types
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
                if page_number in remote_text_pages
                else "NOT_EVALUATED"
            ),
            "evaluated": page_number in remote_text_pages,
            "observation_count": remote_text_count,
        },
        "remote_visual": {
            "evaluated_types": [
                content_type
                for content_type in _PAGE_CONTENT_TYPES
                if content_type in remote_evaluated_types
            ],
            "full_layout_evaluated": all(
                content_type in remote_evaluated_types
                for content_type in _PAGE_CONTENT_TYPES
            ),
        },
    }


def _page_text_authority(
    snapshot: SourceIRSnapshot,
    *,
    page_number: int,
) -> dict[str, Any]:
    authority_pages = visual_text_authority_pages(snapshot)
    if page_number not in authority_pages:
        return {
            "channel": "NATIVE",
            "status": "READY",
            "reason_codes": [],
        }
    page_snapshots = tuple(
        item
        for item in snapshot.visual_text_snapshots
        if item.page_number == page_number and "TEXT" in item.evaluated_content_types
    )
    has_text = any(
        observation.content_type == "TEXT" and observation.raw_text.strip()
        for item in page_snapshots
        for observation in item.observations
    )
    diagnostic_codes = tuple(
        sorted(
            {
                diagnostic.code
                for diagnostic in snapshot.diagnostics
                if diagnostic.native_locator is not None
                and diagnostic.native_locator.page_number == page_number
                and (
                    diagnostic.code.startswith("pdf_visual_text_")
                    or diagnostic.code
                    in {
                        *_VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES,
                        "pdf_full_page_image_with_invisible_text_layer",
                        "pdf_full_page_image_with_low_native_text_coverage",
                        "pdf_full_page_image_without_visible_text",
                        "pdf_vector_text_with_low_native_unicode_coverage",
                    }
                )
            }
        )
    )
    failed = any(
        code in _VISUAL_TEXT_TERMINAL_FAILURE_DIAGNOSTIC_CODES
        or code.endswith(("_failed", "_error", "_timeout"))
        for code in diagnostic_codes
    )
    status = (
        "READY"
        if has_text
        else "EMPTY"
        if page_snapshots
        else "FAILED"
        if failed
        else "PENDING"
    )
    return {
        "channel": "REMOTE_VISUAL",
        "status": status,
        "reason_codes": list(diagnostic_codes),
    }


def _document_content_coverage(
    pages: list[dict[str, Any]],
    *,
    snapshot: SourceIRSnapshot,
) -> dict[str, Any]:
    content_types: list[dict[str, Any]] = []
    for content_type in _PAGE_CONTENT_TYPES:
        page_entries = [
            entry
            for page in pages
            for entry in page["coverage"]["content_types"]
            if entry["type"] == content_type
        ]
        observed_pages = sum(entry["status"] == "OBSERVED" for entry in page_entries)
        not_observed_pages = sum(
            entry["status"] == "NOT_OBSERVED" for entry in page_entries
        )
        not_evaluated_pages = sum(
            entry["status"] == "NOT_EVALUATED" for entry in page_entries
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
                    int(entry["observed_count"]) for entry in page_entries
                ),
                "pages_observed": observed_pages,
                "pages_not_observed": not_observed_pages,
                "pages_not_evaluated": not_evaluated_pages,
            }
        )
    remote_pages = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
    }
    remote_pages_with_observations = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
        if visual_snapshot.observations
    }
    full_layout_pages = {
        visual_snapshot.page_number
        for visual_snapshot in snapshot.visual_text_snapshots
        if all(
            content_type in visual_snapshot.evaluated_content_types
            for content_type in _PAGE_CONTENT_TYPES
        )
    }
    authority_entries = [
        page["coverage"]["text_authority"]
        for page in pages
        if page["coverage"]["text_authority"]["channel"] == "REMOTE_VISUAL"
    ]
    return {
        "source_ir_status": snapshot.status.value,
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
                len(visual_snapshot.observations)
                for visual_snapshot in snapshot.visual_text_snapshots
            ),
            "total_pages": len(pages),
        },
        "remote_visual": {
            "pages_evaluated": len(remote_pages),
            "full_layout_pages": len(full_layout_pages),
            "evaluated_types": [
                content_type
                for content_type in _PAGE_CONTENT_TYPES
                if any(
                    content_type in visual_snapshot.evaluated_content_types
                    for visual_snapshot in snapshot.visual_text_snapshots
                )
            ],
        },
        "text_authority": {
            "visual_page_count": len(authority_entries),
            "ready_page_count": sum(
                entry["status"] == "READY" for entry in authority_entries
            ),
            "degraded_page_numbers": [
                page["page_number"]
                for page in pages
                if page["coverage"]["text_authority"]["channel"]
                == "REMOTE_VISUAL"
                and page["coverage"]["text_authority"]["status"] != "READY"
            ],
        },
    }


def _normalize_page_bounds(
    page_bounds: tuple[tuple[float, float, float, float], ...],
) -> tuple[tuple[float, float, float, float], ...]:
    if not isinstance(page_bounds, tuple):
        raise TypeError("page_bounds must be a tuple")
    normalized: list[tuple[float, float, float, float]] = []
    for bbox in page_bounds:
        if not isinstance(bbox, tuple) or len(bbox) != 4:
            raise TypeError("each page bound must be a four-value tuple")
        values = tuple(float(value) for value in bbox)
        if (
            any(not math.isfinite(value) for value in values)
            or values[2] <= values[0]
            or values[3] <= values[1]
        ):
            raise ValueError("page bounds must have finite positive area")
        normalized.append(values)
    return tuple(normalized)


def _overlay_bbox(
    location: dict[str, Any] | None,
    *,
    page_bounds: tuple[float, float, float, float] | None,
) -> tuple[list[float] | None, str]:
    if not isinstance(location, dict):
        return None, "LOCATION_MISSING"
    raw_bbox = location.get("bbox")
    if not isinstance(raw_bbox, list) or len(raw_bbox) != 4:
        return None, "BBOX_MISSING"
    try:
        bbox = tuple(float(value) for value in raw_bbox)
    except (TypeError, ValueError):
        return None, "BBOX_INVALID"
    if (
        any(not math.isfinite(value) for value in bbox)
        or bbox[2] <= bbox[0]
        or bbox[3] <= bbox[1]
    ):
        return None, "BBOX_INVALID"
    if page_bounds is None:
        return None, "PAGE_BOUNDS_MISSING"
    tolerance = 0.75
    if (
        bbox[0] < page_bounds[0] - tolerance
        or bbox[1] < page_bounds[1] - tolerance
        or bbox[2] > page_bounds[2] + tolerance
        or bbox[3] > page_bounds[3] + tolerance
    ):
        return None, "BBOX_OUTSIDE_PAGE"
    return [round(value, 3) for value in bbox], "LOCATED"


def _page_comparison_issues(
    snapshot: SourceIRSnapshot,
    structure: StructureTreeSnapshot,
    *,
    bounds_by_page: dict[int, tuple[float, float, float, float]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def append_issue(
        *,
        issue_id: str,
        code: str,
        severity: str,
        message: str,
        blocking: bool,
        stage: str,
        source_id: str | None,
        location: dict[str, Any] | None,
    ) -> None:
        page_number = _page_from_location(location)
        overlay_bbox, location_status = _overlay_bbox(
            location,
            page_bounds=bounds_by_page.get(page_number or 0),
        )
        should_overlay = overlay_bbox is not None and (
            blocking or severity.strip().lower() in {"warning", "error"}
        )
        issues.append(
            {
                "id": issue_id,
                "label": f"ISS-{len(issues) + 1:03d}",
                "type": "ISSUE",
                "code": code,
                "severity": severity,
                "message": message,
                "blocking": blocking,
                "stage": stage,
                "source_id": source_id,
                "page_number": page_number,
                "bbox": overlay_bbox,
                "locatable": overlay_bbox is not None,
                "overlay": should_overlay,
                "location_status": location_status,
                "source_location": location,
            }
        )

    for index, diagnostic in enumerate(snapshot.diagnostics):
        append_issue(
            issue_id=f"source-diagnostic:{index}:{diagnostic.code}",
            code=diagnostic.code,
            severity=diagnostic.severity.value,
            message=diagnostic.message,
            blocking=diagnostic.severity.value == "error",
            stage="SOURCE_IR",
            source_id=diagnostic.source_node_id,
            location=_locator_payload(diagnostic.native_locator),
        )

    source_catalog = _build_source_catalog(snapshot)
    for finding in structure.findings:
        resolved_refs = _resolve_refs(finding.source_refs, source_catalog)
        locations = [
            (str(ref.get("source_id") or "") or None, ref.get("location"))
            for ref in resolved_refs
            if isinstance(ref.get("location"), dict)
        ]
        if not locations:
            locations = [(None, None)]
        for ref_index, (source_id, location) in enumerate(locations):
            append_issue(
                issue_id=f"{finding.finding_id}:{ref_index}",
                code=finding.code,
                severity=finding.severity.value,
                message=finding.message_key,
                blocking=finding.blocking,
                stage="STRUCTURE",
                source_id=source_id,
                location=location,
            )
    return issues


def _build_table_catalog(
    snapshot: SourceIRSnapshot,
    *,
    structure_nodes: tuple[StructureNode, ...] = (),
    structure_relations: tuple[StructureRelation, ...] = (),
    visual_table_cell_text_bindings: tuple[VisualTableCellTextBinding, ...] = (),
) -> list[dict[str, Any]]:
    """Serialize each native table exactly once for product-side reconstruction."""

    images_by_cell_id = _cell_image_placements(
        snapshot,
        structure_nodes=structure_nodes,
        structure_relations=structure_relations,
    )
    visual_table_by_cell_id = _cell_visual_table_bindings(
        snapshot,
        structure_nodes=structure_nodes,
        bindings=visual_table_cell_text_bindings,
    )
    authority_pages = visual_text_authority_pages(snapshot)
    tables: list[dict[str, Any]] = []
    for node in snapshot.nodes:
        table = node.table_payload
        if table is None:
            continue
        locations = [
            location
            for location in (
                _locator_payload(node.native_locator),
                *(_locator_payload(cell.native_locator) for cell in table.cells),
            )
            if location is not None
        ]
        pages = sorted(
            {
                int(location["page_number"])
                for location in locations
                if location.get("page_number") is not None
            }
        )
        geometry = _table_geometry_payload(node, table)
        geometry_preserved = geometry["status"] == "PRESERVED"
        table_authority = node.native_locator.page_number in authority_pages

        def serialize_cell(cell: Any) -> dict[str, Any]:
            visual_binding = visual_table_by_cell_id.get(cell.cell_id)
            cell_page = (
                cell.native_locator.page_number
                or node.native_locator.page_number
            )
            visual_authority = cell_page in authority_pages
            if visual_binding is not None:
                adopted_text = str(visual_binding.get("text") or "")
                raw_text = adopted_text
                display_value = None
                formula = None
                text_source = "visual_table_cell_fact"
            elif visual_authority:
                adopted_text = ""
                raw_text = ""
                display_value = None
                formula = None
                text_source = "unresolved_ocr_authority"
            else:
                adopted_text = (
                    cell.raw_text or cell.display_value or cell.formula or ""
                )
                raw_text = cell.raw_text
                display_value = cell.display_value
                formula = cell.formula
                text_source = "native_table_cell"
            return {
                "id": cell.cell_id,
                "row": cell.row,
                "column": cell.column,
                "row_span": cell.row_span,
                "column_span": cell.column_span,
                "text": adopted_text,
                "raw_text": raw_text,
                "display_value": display_value,
                "formula": formula,
                "text_source": text_source,
                "visual_table_binding": visual_binding,
                "location": _locator_payload(cell.native_locator),
                "images": images_by_cell_id.get(cell.cell_id, []),
            }

        tables.append(
            {
                "id": table.table_id,
                "source_node_id": node.source_node_id,
                "source_order": node.source_order,
                "row_count": table.row_count,
                "column_count": table.column_count,
                "header_regions": [
                    _serialize_table_region(region) for region in table.header_regions
                ],
                "merged_regions": [
                    _serialize_table_region(region) for region in table.merged_regions
                ],
                "cells": [
                    serialize_cell(cell)
                    for cell in sorted(
                        table.cells,
                        key=lambda item: (item.row, item.column, item.cell_id),
                    )
                ],
                "notes": [] if table_authority else list(table.notes),
                "text": "" if table_authority else _source_node_display_text(node),
                "location": _locator_payload(node.native_locator),
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "geometry": geometry,
                "reconstruction": {
                    "mode": "STRUCTURED_TEXT_GRID",
                    "exact_visual_layout": False,
                    "merge_semantics": (
                        "PRESERVED"
                        if table.merged_regions
                        else (
                            "NO_MERGES_DETECTED"
                            if geometry_preserved
                            else "NOT_PROVIDED_BY_SOURCE_ADAPTER"
                        )
                    ),
                    "cell_geometry": geometry["status"],
                    "visual_asset_available": bool(table.visual_asset_ref),
                    "text_authority": (
                        "VISUAL"
                        if table_authority
                        and all(
                            cell.cell_id in visual_table_by_cell_id
                            for cell in table.cells
                        )
                        else "UNRESOLVED_OCR_AUTHORITY"
                        if table_authority
                        else "NATIVE"
                    ),
                },
            }
        )
    _annotate_table_continuations(
        tables,
        structure_nodes=structure_nodes,
        structure_relations=structure_relations,
    )
    for table in tables:
        if any(cell.get("images") for cell in table["cells"]):
            table["reconstruction"]["visual_asset_available"] = True
    return tables


def _visual_layout_table_id(observation_id: str) -> str:
    token = str(observation_id or "").strip()
    suffix = token[4:] if token.startswith("vto-") else token
    if not re.fullmatch(r"[0-9a-f]{32}", suffix):
        raise ValueError("visual layout table requires a stable observation identity")
    return f"table-{suffix}"


def _visual_layout_cell_id(
    observation_id: str,
    *,
    row: int,
    column: int,
) -> str:
    return f"{_visual_layout_table_id(observation_id)}-cell-{row}-{column}"


def _build_visual_layout_table_catalog(
    snapshot: SourceIRSnapshot,
) -> list[dict[str, Any]]:
    """Project strict Qwen page-layout grids without inventing native geometry."""

    tables: list[dict[str, Any]] = []
    for visual_snapshot in snapshot.visual_text_snapshots:
        observations_by_index = {
            observation.observation_index: observation
            for observation in visual_snapshot.observations
        }
        figures_by_table_cell: dict[
            tuple[int, int, int],
            list[dict[str, Any]],
        ] = {}
        for observation in visual_snapshot.observations:
            if (
                observation.content_type != "FIGURE"
                or observation.parent_observation_index is None
                or observation.parent_table_cell is None
            ):
                continue
            parent = observations_by_index.get(observation.parent_observation_index)
            if parent is None or parent.layout_table is None:
                continue
            parent_cell = parent.layout_table.cell_covering(
                observation.parent_table_cell[0],
                observation.parent_table_cell[1],
            )
            if parent_cell is None:
                continue
            figures_by_table_cell.setdefault(
                (
                    parent.observation_index,
                    parent_cell.row,
                    parent_cell.column,
                ),
                [],
            ).append(
                {
                    "id": observation.observation_id,
                    "asset_id": None,
                    "content_sha256": observation.page_image_sha256,
                    "media_type": "image/png",
                    "location": {
                        "format": "pdf",
                        "page_number": observation.page_number,
                        "bbox": list(observation.bbox),
                    },
                    "rendering": {
                        "mode": "SOURCE_PDF_CROP",
                        "available": True,
                    },
                    "provider": {
                        "id": observation.provider_id,
                        "version": observation.provider_version,
                    },
                }
            )

        for observation in visual_snapshot.observations:
            table = observation.layout_table
            if observation.content_type != "TABLE" or table is None:
                continue
            table_id = _visual_layout_table_id(observation.observation_id)
            merged_regions = [
                {
                    "row": cell.row,
                    "column": cell.column,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                }
                for cell in table.cells
                if cell.row_span > 1 or cell.column_span > 1
            ]
            cells = [
                {
                    "id": _visual_layout_cell_id(
                        observation.observation_id,
                        row=cell.row,
                        column=cell.column,
                    ),
                    "row": cell.row,
                    "column": cell.column,
                    "row_span": cell.row_span,
                    "column_span": cell.column_span,
                    "text": cell.raw_text,
                    "raw_text": cell.raw_text,
                    "display_value": None,
                    "formula": None,
                    "horizontal_alignment": cell.horizontal_alignment,
                    "vertical_alignment": cell.vertical_alignment,
                    "indent_level": cell.indent_level,
                    "text_source": "visual_page_layout_cell",
                    "visual_table_binding": {
                        "visual_table_cell_id": _visual_layout_cell_id(
                            observation.observation_id,
                            row=cell.row,
                            column=cell.column,
                        ),
                        "visual_table_snapshot_id": (
                            visual_snapshot.visual_text_snapshot_id
                        ),
                        "crop_image_sha256": observation.page_image_sha256,
                        "provider": {
                            "id": observation.provider_id,
                            "version": observation.provider_version,
                        },
                    },
                    "location": {
                        "format": "pdf",
                        "page_number": observation.page_number,
                        "bbox": None,
                    },
                    "images": figures_by_table_cell.get(
                        (
                            observation.observation_index,
                            cell.row,
                            cell.column,
                        ),
                        [],
                    ),
                }
                for cell in table.cells
            ]
            table_text = "\n".join(
                " | ".join(cell.raw_text for cell in table.cells if cell.row == row)
                for row in range(table.row_count)
            )
            tables.append(
                {
                    "id": table_id,
                    "source_node_id": observation.observation_id,
                    "source_observation_id": observation.observation_id,
                    "source_order": (
                        observation.page_number * 10_000 + observation.observation_index
                    ),
                    "row_count": table.row_count,
                    "column_count": table.column_count,
                    "header_regions": (
                        [
                            {
                                "row": 0,
                                "column": 0,
                                "row_span": table.header_row_count,
                                "column_span": table.column_count,
                            }
                        ]
                        if table.header_row_count
                        else []
                    ),
                    "merged_regions": merged_regions,
                    "cells": cells,
                    "notes": [],
                    "text": table_text,
                    "location": {
                        "format": "pdf",
                        "page_number": observation.page_number,
                        "bbox": list(observation.bbox),
                    },
                    "page_start": observation.page_number,
                    "page_end": observation.page_number,
                    "geometry": {"status": "NOT_AVAILABLE"},
                    "reconstruction": {
                        "mode": "REMOTE_STRUCTURED_GRID",
                        "exact_visual_layout": False,
                        "merge_semantics": (
                            "PRESERVED" if merged_regions else "NO_MERGES_DETECTED"
                        ),
                        "cell_geometry": "NOT_AVAILABLE",
                        "visual_asset_available": any(cell["images"] for cell in cells),
                        "provider": {
                            "id": observation.provider_id,
                            "version": observation.provider_version,
                        },
                    },
                    "layout_continuation_evidence": {
                        "starts_mid_table": table.starts_mid_table,
                        "continues_next_page": table.continues_next_page,
                    },
                }
            )
    _annotate_visual_layout_table_continuations(tables)
    return tables


def _annotate_visual_layout_table_continuations(
    tables: list[dict[str, Any]],
) -> None:
    """Join only unique adjacent model-declared table segments."""

    if not tables:
        return
    by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        by_page.setdefault(int(table["page_start"]), []).append(table)
    outgoing_candidates: dict[str, list[str]] = {}
    incoming_candidates: dict[str, list[str]] = {}
    for source in tables:
        evidence = source["layout_continuation_evidence"]
        if not evidence["continues_next_page"]:
            continue
        candidates = [
            target
            for target in by_page.get(int(source["page_end"]) + 1, [])
            if target["layout_continuation_evidence"]["starts_mid_table"]
            and target["column_count"] == source["column_count"]
        ]
        for target in candidates:
            outgoing_candidates.setdefault(source["id"], []).append(target["id"])
            incoming_candidates.setdefault(target["id"], []).append(source["id"])
    outgoing = {
        source_id: target_ids[0]
        for source_id, target_ids in outgoing_candidates.items()
        if len(target_ids) == 1 and len(incoming_candidates.get(target_ids[0], [])) == 1
    }
    incoming = {target_id: source_id for source_id, target_id in outgoing.items()}
    tables_by_id = {table["id"]: table for table in tables}
    visited: set[str] = set()
    for table in sorted(
        tables,
        key=lambda item: (item["page_start"], item["source_order"], item["id"]),
    ):
        table_id = table["id"]
        if table_id in visited or table_id in incoming:
            continue
        chain: list[str] = []
        current = table_id
        while current in tables_by_id and current not in chain:
            chain.append(current)
            visited.add(current)
            current = outgoing.get(current, "")
            if not current:
                break
        for index, segment_id in enumerate(chain):
            tables_by_id[segment_id]["continuation"] = {
                "status": "CONTINUED" if len(chain) > 1 else "SINGLE",
                "logical_table_id": chain[0],
                "segment_index": index,
                "segment_count": len(chain),
                "previous_table_id": chain[index - 1] if index else None,
                "next_table_id": (chain[index + 1] if index + 1 < len(chain) else None),
                "basis": (
                    "UNIQUE_ADJACENT_PROVIDER_FLAGS"
                    if len(chain) > 1
                    else "SINGLE_PAGE"
                ),
            }
    for table in tables:
        table.setdefault(
            "continuation",
            {
                "status": "SINGLE",
                "logical_table_id": table["id"],
                "segment_index": 0,
                "segment_count": 1,
                "previous_table_id": None,
                "next_table_id": None,
                "basis": "AMBIGUOUS_OR_SINGLE_PAGE",
            },
        )


def _cell_visual_table_bindings(
    snapshot: SourceIRSnapshot,
    *,
    structure_nodes: tuple[StructureNode, ...],
    bindings: tuple[VisualTableCellTextBinding, ...],
) -> dict[str, dict[str, Any]]:
    """Project only exact-topology visual table facts into native cells."""

    visual_cells = {
        cell.visual_table_cell_id: (visual_snapshot, cell)
        for visual_snapshot in snapshot.visual_table_snapshots
        for cell in visual_snapshot.cells
    }
    structure_by_id = {node.structure_node_id: node for node in structure_nodes}
    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    result: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        table_structure = structure_by_id.get(binding.table_structure_node_id)
        if (
            table_structure is None
            or table_structure.kind is not StructureNodeKind.TABLE
        ):
            continue
        table_source_nodes = [
            source_nodes[ref.source_id]
            for ref in table_structure.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in source_nodes
        ]
        visual_fact = visual_cells.get(binding.visual_table_cell_id)
        if visual_fact is None:
            continue
        visual_snapshot, visual_cell = visual_fact
        native_pair = next(
            (
                (source_node, cell)
                for source_node in table_source_nodes
                if source_node.table_payload is not None
                for cell in source_node.table_payload.cells
                if cell.cell_id == binding.table_cell_id
            ),
            None,
        )
        if native_pair is None:
            continue
        source_node, native_cell = native_pair
        if visual_snapshot.table_source_node_id != source_node.source_node_id or (
            visual_cell.row,
            visual_cell.column,
            visual_cell.row_span,
            visual_cell.column_span,
        ) != (
            native_cell.row,
            native_cell.column,
            native_cell.row_span,
            native_cell.column_span,
        ):
            continue
        result[binding.table_cell_id] = {
            "text": visual_cell.raw_text,
            "visual_table_cell_id": visual_cell.visual_table_cell_id,
            "visual_table_snapshot_id": visual_snapshot.visual_table_snapshot_id,
            "crop_image_sha256": visual_snapshot.crop_image_sha256,
            "provider": {
                "id": visual_snapshot.provider_id,
                "version": visual_snapshot.provider_version,
            },
        }
    return result


def _cell_image_placements(
    snapshot: SourceIRSnapshot,
    *,
    structure_nodes: tuple[StructureNode, ...],
    structure_relations: tuple[StructureRelation, ...],
) -> dict[str, list[dict[str, Any]]]:
    """Expose only image-to-cell placements already proven by StructureTree."""

    source_nodes = {node.source_node_id: node for node in snapshot.nodes}
    assets = {asset.asset_id: asset for asset in snapshot.assets}
    structure_by_id = {node.structure_node_id: node for node in structure_nodes}
    result: dict[str, list[dict[str, Any]]] = {}
    for relation in structure_relations:
        if relation.kind not in {
            StructureRelationKind.PLACED_IN_CELL,
            StructureRelationKind.PLACED_OVER_CELLS,
        }:
            continue
        cell_refs = relation.source_refs[1:]
        if (
            not relation.source_refs
            or relation.source_refs[0].kind is not StructureSourceRefKind.SOURCE_NODE
            or any(
                ref.kind is not StructureSourceRefKind.TABLE_CELL for ref in cell_refs
            )
            or (
                relation.kind is StructureRelationKind.PLACED_IN_CELL
                and len(cell_refs) != 1
            )
            or (
                relation.kind is StructureRelationKind.PLACED_OVER_CELLS
                and len(cell_refs) < 2
            )
        ):
            continue
        image_ref = relation.source_refs[0]
        image_node = source_nodes.get(image_ref.source_id)
        target_table = structure_by_id.get(relation.target_structure_node_id)
        if (
            image_node is None
            or image_node.kind not in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            or image_node.asset_ref is None
            or target_table is None
            or target_table.kind is not StructureNodeKind.TABLE
        ):
            continue
        asset = assets.get(image_node.asset_ref)
        location = _locator_payload(
            image_node.visual_locator or image_node.native_locator
        )
        if asset is None or location is None:
            continue
        target_source_node_ids = {
            ref.source_id
            for ref in target_table.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        }
        target_source_nodes = [
            source_nodes[source_node_id]
            for source_node_id in target_source_node_ids
            if source_node_id in source_nodes
        ]
        target_cell_ids = {
            cell.cell_id
            for source_node in target_source_nodes
            if source_node.table_payload is not None
            for cell in source_node.table_payload.cells
        }
        if any(cell_ref.source_id not in target_cell_ids for cell_ref in cell_refs):
            continue
        placement = {
            "id": image_node.source_node_id,
            "asset_id": asset.asset_id,
            "content_sha256": asset.content_sha256,
            "media_type": asset.media_type,
            "location": location,
            "rendering": {
                "mode": "SOURCE_PDF_CROP",
                "available": bool(
                    location.get("page_number")
                    and isinstance(location.get("bbox"), list)
                    and len(location["bbox"]) == 4
                ),
            },
        }
        if relation.kind is StructureRelationKind.PLACED_OVER_CELLS:
            placement["cell_placement"] = {
                "relation_kind": relation.kind.value,
                "covered_cell_ids": [cell_ref.source_id for cell_ref in cell_refs],
            }
        # A multi-cell overlay is rendered once at its first canonical cell.
        result.setdefault(cell_refs[0].source_id, []).append(placement)
    for placements in result.values():
        placements.sort(
            key=lambda item: (
                (item.get("location") or {}).get("page_number") or 0,
                ((item.get("location") or {}).get("bbox") or [0.0, 0.0])[1],
                ((item.get("location") or {}).get("bbox") or [0.0])[0],
                item["id"],
            )
        )
    return result


def _annotate_table_continuations(
    tables: list[dict[str, Any]],
    *,
    structure_nodes: tuple[StructureNode, ...],
    structure_relations: tuple[StructureRelation, ...],
) -> None:
    """Attach simple, grounded continuation chains without merging source facts."""

    tables_by_source_node_id = {
        str(table.get("source_node_id") or ""): table
        for table in tables
        if table.get("source_node_id")
    }
    structure_by_id = {node.structure_node_id: node for node in structure_nodes}
    table_id_by_structure_id: dict[str, str] = {}
    for structure_node in structure_nodes:
        if structure_node.kind is not StructureNodeKind.TABLE:
            continue
        source_node_ids = [
            ref.source_id
            for ref in structure_node.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in tables_by_source_node_id
        ]
        if len(source_node_ids) == 1:
            table_id_by_structure_id[structure_node.structure_node_id] = str(
                tables_by_source_node_id[source_node_ids[0]]["id"]
            )

    outgoing_candidates: dict[str, set[str]] = {}
    incoming_candidates: dict[str, set[str]] = {}
    for relation in structure_relations:
        if relation.kind is not StructureRelationKind.CONTINUES:
            continue
        source_node = structure_by_id.get(relation.source_structure_node_id)
        target_node = structure_by_id.get(relation.target_structure_node_id)
        source_table_id = table_id_by_structure_id.get(
            relation.source_structure_node_id
        )
        target_table_id = table_id_by_structure_id.get(
            relation.target_structure_node_id
        )
        if (
            source_node is None
            or target_node is None
            or source_node.kind is not StructureNodeKind.TABLE
            or target_node.kind is not StructureNodeKind.TABLE
            or source_table_id is None
            or target_table_id is None
        ):
            continue
        outgoing_candidates.setdefault(source_table_id, set()).add(target_table_id)
        incoming_candidates.setdefault(target_table_id, set()).add(source_table_id)

    outgoing = {
        source_table_id: next(iter(target_table_ids))
        for source_table_id, target_table_ids in outgoing_candidates.items()
        if len(target_table_ids) == 1
        and len(incoming_candidates.get(next(iter(target_table_ids)), set())) == 1
    }
    incoming = {target: source for source, target in outgoing.items()}
    table_by_id = {str(table["id"]): table for table in tables}
    visited: set[str] = set()
    for table in sorted(
        tables,
        key=lambda item: (item.get("source_order", 0), str(item["id"])),
    ):
        table_id = str(table["id"])
        if table_id in visited or table_id in incoming:
            continue
        chain: list[str] = []
        current = table_id
        while current in table_by_id and current not in chain:
            chain.append(current)
            visited.add(current)
            current = outgoing.get(current, "")
            if not current:
                break
        for index, segment_table_id in enumerate(chain):
            table_by_id[segment_table_id]["continuation"] = {
                "status": "CONTINUED" if len(chain) > 1 else "SINGLE",
                "logical_table_id": chain[0],
                "segment_index": index,
                "segment_count": len(chain),
                "previous_table_id": chain[index - 1] if index > 0 else None,
                "next_table_id": (chain[index + 1] if index + 1 < len(chain) else None),
            }
    for table in tables:
        if "continuation" not in table:
            table["continuation"] = {
                "status": "SINGLE",
                "logical_table_id": str(table["id"]),
                "segment_index": 0,
                "segment_count": 1,
                "previous_table_id": None,
                "next_table_id": None,
            }


def _table_geometry_payload(node: Any, table: Any) -> dict[str, Any]:
    table_bbox = getattr(getattr(node, "native_locator", None), "bbox", None)
    if (
        not isinstance(table_bbox, tuple)
        or len(table_bbox) != 4
        or table_bbox[2] <= table_bbox[0]
        or table_bbox[3] <= table_bbox[1]
    ):
        return {"status": "NOT_AVAILABLE"}

    x_coordinates = [float(table_bbox[0]), float(table_bbox[2])]
    y_coordinates = [float(table_bbox[1]), float(table_bbox[3])]
    for cell in table.cells:
        bbox = getattr(cell.native_locator, "bbox", None)
        if (
            not isinstance(bbox, tuple)
            or len(bbox) != 4
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
        ):
            return {"status": "NOT_AVAILABLE"}
        x_coordinates.extend((float(bbox[0]), float(bbox[2])))
        y_coordinates.extend((float(bbox[1]), float(bbox[3])))

    column_boundaries = _cluster_table_geometry_coordinates(x_coordinates)
    row_boundaries = _cluster_table_geometry_coordinates(y_coordinates)
    if (
        len(column_boundaries) != table.column_count + 1
        or len(row_boundaries) != table.row_count + 1
    ):
        return {"status": "NOT_AVAILABLE"}

    occupied: set[tuple[int, int]] = set()
    for cell in table.cells:
        bbox = cell.native_locator.bbox
        assert bbox is not None
        column_start = _table_geometry_boundary_index(column_boundaries, bbox[0])
        column_end = _table_geometry_boundary_index(column_boundaries, bbox[2])
        row_start = _table_geometry_boundary_index(row_boundaries, bbox[1])
        row_end = _table_geometry_boundary_index(row_boundaries, bbox[3])
        if (
            None in (row_start, row_end, column_start, column_end)
            or row_start != cell.row
            or column_start != cell.column
            or row_end - row_start != cell.row_span
            or column_end - column_start != cell.column_span
        ):
            return {"status": "NOT_AVAILABLE"}
        assert row_start is not None
        assert row_end is not None
        assert column_start is not None
        assert column_end is not None
        for row_index in range(row_start, row_end):
            for column_index in range(column_start, column_end):
                position = (row_index, column_index)
                if position in occupied:
                    return {"status": "NOT_AVAILABLE"}
                occupied.add(position)

    if len(occupied) != table.row_count * table.column_count:
        return {"status": "NOT_AVAILABLE"}
    column_widths = [
        right - left for left, right in zip(column_boundaries, column_boundaries[1:])
    ]
    row_heights = [
        bottom - top for top, bottom in zip(row_boundaries, row_boundaries[1:])
    ]
    if any(value <= 0.0 for value in (*column_widths, *row_heights)):
        return {"status": "NOT_AVAILABLE"}
    return {
        "status": "PRESERVED",
        "width_points": round(table_bbox[2] - table_bbox[0], 3),
        "height_points": round(table_bbox[3] - table_bbox[1], 3),
        "column_widths_points": [round(value, 3) for value in column_widths],
        "row_heights_points": [round(value, 3) for value in row_heights],
    }


def _cluster_table_geometry_coordinates(values: list[float]) -> tuple[float, ...]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if (
            not groups
            or abs(value - (sum(groups[-1]) / len(groups[-1])))
            > _TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS
        ):
            groups.append([value])
        else:
            groups[-1].append(value)
    return tuple(sum(group) / len(group) for group in groups)


def _table_geometry_boundary_index(
    boundaries: tuple[float, ...],
    value: float,
) -> int | None:
    matches = tuple(
        index
        for index, boundary in enumerate(boundaries)
        if abs(boundary - value) <= _TABLE_GRID_ALIGNMENT_TOLERANCE_POINTS
    )
    return matches[0] if len(matches) == 1 else None


def _serialize_table_region(region: Any) -> dict[str, int]:
    return {
        "row": region.row,
        "column": region.column,
        "row_span": region.row_span,
        "column_span": region.column_span,
    }


def _source_node_display_text(node: Any) -> str:
    if str(node.raw_text or "").strip():
        return node.raw_text
    if node.table_payload is None:
        return ""
    rows: dict[int, list[str]] = {}
    for cell in sorted(
        node.table_payload.cells,
        key=lambda item: (item.row, item.column),
    ):
        text = str(cell.raw_text or cell.display_value or cell.formula or "").strip()
        if text:
            rows.setdefault(cell.row, []).append(text)
    return "\n".join(" | ".join(rows[row]) for row in sorted(rows))


def _serialize_candidate(
    node: RequirementViewNode,
    *,
    depth: int,
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    structure_nodes: dict[str, StructureNode],
    annotations: dict[str, Any],
    findings: tuple[StructureFinding, ...],
) -> dict[str, Any]:
    target = structure_nodes[node.target_structure_node_id]
    related_annotations = [
        annotations[annotation_id]
        for annotation_id in node.semantic_annotation_ids
        if annotation_id in annotations
    ]
    exact_refs = _resolve_grounding_spans(
        node.source_span_ids,
        annotations=related_annotations,
        source_catalog=source_catalog,
    )
    context_refs = _resolve_target_context_refs(
        target=target,
        context_source_refs=node.full_context_source_refs,
        source_catalog=source_catalog,
    )
    text = _join_ref_text(exact_refs)
    context_text = _join_ref_text(context_refs)
    ancestor_path = []
    for ancestor_id in node.ancestor_structure_node_ids:
        ancestor = structure_nodes.get(ancestor_id)
        if ancestor is None:
            continue
        ancestor_refs = _resolve_structure_node_refs(
            ancestor,
            source_catalog=source_catalog,
        )
        ancestor_text = _join_ref_text(ancestor_refs)
        ancestor_path.append(
            {
                "structure_node_id": ancestor.structure_node_id,
                "kind": ancestor.kind.value,
                "label": _display_label(ancestor, ancestor_text),
            }
        )

    reason_codes = sorted(
        {
            reason_code
            for annotation in related_annotations
            for reason_code in annotation.reason_codes
        }
    )
    locations = [
        resolved["location"] for resolved in exact_refs if resolved.get("location")
    ]
    pages = sorted(
        {
            int(location["page_number"])
            for location in locations
            if location.get("page_number") is not None
        }
    )

    return {
        "id": node.requirement_view_node_id,
        "parent_id": node.parent_requirement_view_node_id,
        "depth": depth,
        "source_order": node.source_order,
        "role": node.role.value,
        "decision_mode": node.decision_mode.value,
        "semantic_class": node.semantic_class.value,
        "requirement_subtype": (
            node.requirement_subtype.value if node.requirement_subtype else None
        ),
        "semantic_statuses": sorted(
            {annotation.status.value for annotation in related_annotations}
        ),
        "reason_codes": reason_codes,
        "title": _display_label(target, text),
        "text": text,
        "context_text": context_text,
        "target_structure": {
            "id": target.structure_node_id,
            "kind": target.kind.value,
            "boundary_basis": target.boundary_basis.value,
            "explicit_label": target.explicit_label,
        },
        "ancestor_path": ancestor_path,
        "page_start": pages[0] if pages else None,
        "page_end": pages[-1] if pages else None,
        "locations": locations,
        "source_refs": exact_refs,
        "context_source_refs": context_refs,
        "table_ids": _table_ids_from_refs(context_refs),
        "finding_codes": [finding.code for finding in findings],
        "finding_ids": [finding.finding_id for finding in findings],
    }


def _serialize_document_outline(
    *,
    structure_nodes: tuple[StructureNode, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Expose the authoritative body outline plus each complete TOC block.

    This collection is intentionally independent from requirement semantics.
    It is the fidelity/navigation view used by the PDF experience.
    """

    nodes_by_id = {node.structure_node_id: node for node in structure_nodes}
    ordered_nodes = sorted(
        structure_nodes,
        key=lambda item: (item.source_order, item.structure_node_id),
    )
    outline: list[dict[str, Any]] = []
    document_root = next(
        (node for node in ordered_nodes if node.kind is StructureNodeKind.DOCUMENT),
        None,
    )
    first_outline_order = min(
        (
            node.source_order
            for node in ordered_nodes
            if node.kind is StructureNodeKind.SECTION
            or node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        ),
        default=None,
    )
    if document_root is not None:
        preface_nodes = [
            node
            for node in ordered_nodes
            if node.kind is StructureNodeKind.CONTENT
            and node.boundary_basis is not StructureBoundaryBasis.PAGE_FURNITURE
            and node.parent_structure_node_id == document_root.structure_node_id
            and (first_outline_order is None or node.source_order < first_outline_order)
        ]
        preface_refs = _dedupe_resolved_refs(
            _resolve_refs(
                tuple(ref for node in preface_nodes for ref in node.source_refs),
                source_catalog,
            )
        )
        preface_refs = [
            ref
            for ref in preface_refs
            if str(ref.get("source_kind") or "").lower() not in {"header", "footer"}
        ]
        if preface_refs:
            context_blocks = _context_blocks(
                preface_refs,
                tables_by_id=tables_by_id,
            )
            pages = _pages_from_context_blocks(context_blocks)
            outline.append(
                {
                    "id": f"{document_root.structure_node_id}-preface",
                    "kind": "DOCUMENT_REGION",
                    "document_region": "PREFACE",
                    "source_order": min(
                        (node.source_order for node in preface_nodes),
                        default=document_root.source_order,
                    ),
                    "depth": 0,
                    "boundary_structure_node_id": document_root.structure_node_id,
                    "outline_label": "封面",
                    "title": "封面与文档信息",
                    "text": _join_ref_text(preface_refs),
                    "semantic_class": "NOT_APPLICABLE",
                    "review_status": "DOCUMENT_STRUCTURE",
                    "contains_recognized_requirement": False,
                    "ancestor_path": [],
                    "context_text": _join_context_block_text(context_blocks),
                    "context_blocks": context_blocks,
                    "source_refs": preface_refs,
                    "context_source_refs": preface_refs,
                    "table_ids": [
                        block["table_id"]
                        for block in context_blocks
                        if block["kind"] == "table"
                    ],
                    "page_start": pages[0] if pages else None,
                    "page_end": pages[-1] if pages else None,
                    "assessment_points": [],
                    "item_ids": [],
                    "finding_codes": [],
                    "finding_ids": [],
                    "entries": [],
                }
            )

    for node in ordered_nodes:
        is_toc = node.boundary_basis is StructureBoundaryBasis.TOC_BLOCK
        if not is_toc and node.kind is not StructureNodeKind.SECTION:
            continue

        own_refs = _resolve_structure_node_refs(
            node,
            source_catalog=source_catalog,
        )
        own_text = _join_ref_text(own_refs)
        context_refs = (
            _resolve_refs(node.source_refs, source_catalog)
            if is_toc
            else _resolve_structure_subtree_refs(
                node.structure_node_id,
                structure_nodes=nodes_by_id,
                source_catalog=source_catalog,
            )
        )
        context_blocks = _context_blocks(context_refs, tables_by_id=tables_by_id)
        pages = _pages_from_context_blocks(context_blocks)
        section_ancestors = _section_ancestors(node, structure_nodes=nodes_by_id)
        outline.append(
            {
                "id": node.structure_node_id,
                "kind": "TOC" if is_toc else "DOCUMENT_SECTION",
                "document_region": "TOC" if is_toc else "BODY",
                "source_order": node.source_order,
                "depth": 0 if is_toc else len(section_ancestors),
                "boundary_structure_node_id": node.structure_node_id,
                "outline_label": (
                    "目录" if is_toc else _outline_display_code(node, own_text)
                ),
                "title": "目录" if is_toc else _display_label(node, own_text),
                "text": own_text,
                "semantic_class": "NOT_APPLICABLE",
                "review_status": "DOCUMENT_STRUCTURE",
                "contains_recognized_requirement": False,
                "ancestor_path": _structure_ancestor_path(
                    node,
                    structure_nodes=nodes_by_id,
                    source_catalog=source_catalog,
                ),
                "llm_scope_selector": _llm_scope_selector(
                    node,
                    structure_nodes=nodes_by_id,
                ),
                "context_text": _join_context_block_text(context_blocks),
                "context_blocks": context_blocks,
                "source_refs": own_refs,
                "context_source_refs": context_refs,
                "table_ids": [
                    block["table_id"]
                    for block in context_blocks
                    if block["kind"] == "table"
                ],
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "assessment_points": [],
                "item_ids": [],
                "finding_codes": [],
                "finding_ids": [],
                "entries": _toc_display_entries(own_refs) if is_toc else [],
            }
        )
    return outline


def _section_ancestors(
    node: StructureNode,
    *,
    structure_nodes: dict[str, StructureNode],
) -> list[StructureNode]:
    ancestors: list[StructureNode] = []
    parent_id = node.parent_structure_node_id
    while parent_id is not None:
        parent = structure_nodes.get(parent_id)
        if parent is None:
            break
        if parent.kind is StructureNodeKind.SECTION:
            ancestors.append(parent)
        parent_id = parent.parent_structure_node_id
    ancestors.reverse()
    return ancestors


def _llm_scope_selector(
    node: StructureNode,
    *,
    structure_nodes: dict[str, StructureNode],
) -> dict[str, list[str]] | None:
    labels = tuple(
        str(item.explicit_label or "").strip()
        for item in (*_section_ancestors(node, structure_nodes=structure_nodes), node)
    )
    if not labels or any(not label for label in labels):
        return None
    try:
        selector = LlmSectionScopeSelector(labels)
    except ValueError:
        return None
    return {"normalized_section_path": list(selector.normalized_section_path)}


def _outline_display_code(node: StructureNode, text: str) -> str:
    first_line = next(
        (line.strip() for line in str(text or "").splitlines() if line.strip()),
        "",
    )
    chinese = _CHINESE_OUTLINE_PREFIX.match(first_line)
    if chinese is not None:
        return chinese.group("label").rstrip("、．.").replace(" ", "")
    return str(node.explicit_label or "").strip() or first_line


def _toc_display_entries(own_refs: list[dict[str, Any]]) -> list[str]:
    """Expose native TOC lines while retaining block-level source grounding."""

    return [
        line.strip()
        for ref in own_refs
        for line in str(ref.get("text") or "").splitlines()
        if line.strip() and not _is_toc_heading_text(line)
    ]


def _serialize_requirement_groups(
    nodes: tuple[RequirementViewNode, ...],
    *,
    serialized_items: dict[str, dict[str, Any]],
    structure_nodes: dict[str, StructureNode],
    semantic_annotations: tuple[Any, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Group projected items by the nearest explicit numbered section.

    PDF native text blocks are evidence units, not product-level requirement
    boundaries.  This presenter therefore folds unnumbered PRIMARY roots into
    their nearest numbered SECTION ancestor and exposes the native blocks only
    as assessment points.  A root without any numbered ancestor remains its own
    conservative group.
    """

    nodes_by_id = {node.requirement_view_node_id: node for node in nodes}
    annotations_by_target = _annotations_by_target(semantic_annotations)

    def root_for(node: RequirementViewNode) -> RequirementViewNode:
        current = node
        visited: set[str] = set()
        while current.parent_requirement_view_node_id is not None:
            if current.requirement_view_node_id in visited:
                break
            visited.add(current.requirement_view_node_id)
            parent = nodes_by_id.get(current.parent_requirement_view_node_id)
            if parent is None:
                break
            current = parent
        return current

    def numbered_boundary_for(root: RequirementViewNode) -> StructureNode | None:
        target = structure_nodes[root.target_structure_node_id]
        if (
            target.kind is StructureNodeKind.SECTION
            and target.explicit_label is not None
        ):
            return target
        for ancestor_id in reversed(root.ancestor_structure_node_ids):
            ancestor = structure_nodes.get(ancestor_id)
            if (
                ancestor is not None
                and ancestor.kind is StructureNodeKind.SECTION
                and ancestor.explicit_label is not None
            ):
                return ancestor
        return None

    grouping_key_by_root: dict[str, tuple[str, str]] = {}
    boundary_by_group: dict[tuple[str, str], StructureNode | None] = {}
    members_by_group: dict[tuple[str, str], list[RequirementViewNode]] = {}
    for node in nodes:
        root = root_for(node)
        root_id = root.requirement_view_node_id
        if root_id not in grouping_key_by_root:
            boundary = numbered_boundary_for(root)
            group_key = (
                ("section", boundary.structure_node_id)
                if boundary is not None
                else ("view", root_id)
            )
            grouping_key_by_root[root_id] = group_key
            boundary_by_group[group_key] = boundary
        members_by_group.setdefault(grouping_key_by_root[root_id], []).append(node)

    groups: list[dict[str, Any]] = []
    projected_decision_targets = {
        node.target_structure_node_id
        for node in nodes
        if node.decision_mode.value != "NONE"
    }
    for group_key, members in members_by_group.items():
        boundary = boundary_by_group[group_key]
        ordered_members = sorted(
            members,
            key=lambda item: (item.source_order, item.requirement_view_node_id),
        )
        if boundary is not None:
            context_refs = _resolve_structure_subtree_refs(
                boundary.structure_node_id,
                structure_nodes=structure_nodes,
                source_catalog=source_catalog,
            )
            boundary_refs = _resolve_structure_node_refs(
                boundary,
                source_catalog=source_catalog,
            )
            boundary_text = _join_ref_text(boundary_refs)
            group_id = boundary.structure_node_id
            group_source_order = boundary.source_order
            outline_label = boundary.explicit_label
            title = _display_label(boundary, boundary_text)
            ancestor_path = _structure_ancestor_path(
                boundary,
                structure_nodes=structure_nodes,
                source_catalog=source_catalog,
            )
            boundary_classes = _semantic_classes_for_target(
                boundary.structure_node_id,
                annotations_by_target=annotations_by_target,
            )
            boundary_is_decision = (
                boundary.structure_node_id in projected_decision_targets
            )
            review_status = (
                "REQUIREMENT" if boundary_is_decision else "NEEDS_CLASSIFICATION"
            )
        else:
            root = root_for(ordered_members[0])
            root_item = serialized_items[root.requirement_view_node_id]
            context_refs = _dedupe_resolved_refs(
                resolved
                for member in ordered_members
                if member.parent_requirement_view_node_id is None
                for resolved in _resolve_refs(
                    member.full_context_source_refs,
                    source_catalog,
                )
            )
            boundary_text = root_item["text"]
            group_id = root.requirement_view_node_id
            group_source_order = root.source_order
            outline_label = root_item["target_structure"]["explicit_label"]
            title = root_item["title"]
            ancestor_path = root_item["ancestor_path"]
            boundary_classes = ()
            review_status = "REQUIREMENT"

        context_blocks = _context_blocks(context_refs, tables_by_id=tables_by_id)
        pages = _pages_from_context_blocks(context_blocks)
        assessment_points = [
            _serialize_assessment_point(member, serialized_items)
            for member in ordered_members
            if member.decision_mode.value != "NONE"
        ]
        group_semantic_classes = set(boundary_classes)
        if assessment_points:
            group_semantic_classes.add("REQUIREMENT")
        semantic_class = (
            next(iter(group_semantic_classes))
            if len(group_semantic_classes) == 1
            else "MIXED"
        )
        finding_codes = _dedupe_strings(
            code
            for member in ordered_members
            for code in serialized_items[member.requirement_view_node_id][
                "finding_codes"
            ]
        )
        finding_ids = _dedupe_strings(
            finding_id
            for member in ordered_members
            for finding_id in serialized_items[member.requirement_view_node_id][
                "finding_ids"
            ]
        )
        groups.append(
            {
                "id": group_id,
                "kind": "REQUIREMENT",
                "source_order": group_source_order,
                "boundary_structure_node_id": (
                    boundary.structure_node_id if boundary is not None else None
                ),
                "outline_label": outline_label,
                "title": title,
                "text": boundary_text,
                "semantic_class": semantic_class,
                "review_status": review_status,
                "contains_recognized_requirement": True,
                "ancestor_path": ancestor_path,
                "context_text": _join_context_block_text(context_blocks),
                "context_blocks": context_blocks,
                "table_ids": [
                    block["table_id"]
                    for block in context_blocks
                    if block["kind"] == "table"
                ],
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "assessment_points": assessment_points,
                "item_ids": [
                    member.requirement_view_node_id for member in ordered_members
                ],
                "finding_codes": finding_codes,
                "finding_ids": finding_ids,
            }
        )
    return sorted(groups, key=lambda group: (group["source_order"], group["id"]))


def _serialize_assessment_point(
    member: RequirementViewNode,
    serialized_items: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    item = serialized_items[member.requirement_view_node_id]
    return {
        "id": member.requirement_view_node_id,
        "source_order": member.source_order,
        "title": item["title"],
        "text": item["text"],
        "page_start": item["page_start"],
        "page_end": item["page_end"],
        "finding_codes": item["finding_codes"],
        "finding_ids": item["finding_ids"],
    }


def _serialize_review_groups(
    *,
    requirement_groups: list[dict[str, Any]],
    structure_nodes: tuple[StructureNode, ...],
    semantic_annotations: tuple[Any, ...],
    projected_structure_node_ids: set[str],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
    tables_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add unprojected numbered leaf sections without calling them requirements."""

    nodes_by_id = {node.structure_node_id: node for node in structure_nodes}
    annotations_by_target = _annotations_by_target(semantic_annotations)
    explicit_sections = {
        node.structure_node_id: node
        for node in structure_nodes
        if node.kind is StructureNodeKind.SECTION and node.explicit_label is not None
    }
    non_leaf_section_ids: set[str] = set()
    for section in explicit_sections.values():
        parent_id = section.parent_structure_node_id
        while parent_id is not None:
            if parent_id in explicit_sections:
                non_leaf_section_ids.add(parent_id)
            parent = nodes_by_id.get(parent_id)
            parent_id = parent.parent_structure_node_id if parent is not None else None

    used_boundary_ids = {
        str(group.get("boundary_structure_node_id") or "")
        for group in requirement_groups
        if group.get("boundary_structure_node_id")
    }
    review_groups = [dict(group) for group in requirement_groups]
    for section in explicit_sections.values():
        if (
            section.structure_node_id in non_leaf_section_ids
            or section.structure_node_id in projected_structure_node_ids
            or section.structure_node_id in used_boundary_ids
        ):
            continue
        context_refs = _resolve_structure_subtree_refs(
            section.structure_node_id,
            structure_nodes=nodes_by_id,
            source_catalog=source_catalog,
        )
        context_blocks = _context_blocks(context_refs, tables_by_id=tables_by_id)
        own_refs = _resolve_structure_node_refs(
            section,
            source_catalog=source_catalog,
        )
        own_text = _join_ref_text(own_refs)
        pages = _pages_from_context_blocks(context_blocks)
        semantic_classes = _semantic_classes_for_target(
            section.structure_node_id,
            annotations_by_target=annotations_by_target,
        )
        semantic_class = _combined_semantic_class(semantic_classes)
        review_status = (
            "NEEDS_CLASSIFICATION"
            if not semantic_classes
            or "UNRESOLVED" in semantic_classes
            or "REQUIREMENT" in semantic_classes
            or "HEADING" in semantic_classes
            or len(set(semantic_classes)) > 1
            else "INFORMATION"
        )
        review_groups.append(
            {
                "id": section.structure_node_id,
                "kind": "SECTION_REVIEW",
                "source_order": section.source_order,
                "boundary_structure_node_id": section.structure_node_id,
                "outline_label": section.explicit_label,
                "title": _display_label(section, own_text),
                "text": own_text,
                "semantic_class": semantic_class,
                "review_status": review_status,
                "contains_recognized_requirement": False,
                "ancestor_path": _structure_ancestor_path(
                    section,
                    structure_nodes=nodes_by_id,
                    source_catalog=source_catalog,
                ),
                "context_text": _join_context_block_text(context_blocks),
                "context_blocks": context_blocks,
                "table_ids": [
                    block["table_id"]
                    for block in context_blocks
                    if block["kind"] == "table"
                ],
                "page_start": pages[0] if pages else None,
                "page_end": pages[-1] if pages else None,
                "assessment_points": [],
                "item_ids": [],
                "finding_codes": [],
                "finding_ids": [],
            }
        )
    return sorted(
        review_groups,
        key=lambda group: (group["source_order"], group["id"]),
    )


def _resolve_structure_subtree_refs(
    root_id: str,
    *,
    structure_nodes: dict[str, StructureNode],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Resolve a subtree without discarding exact structural text spans.

    Multiple numbered sections may originate from one immutable PDF text block.
    Deduplicating their raw ``StructureSourceRef`` values before resolution
    expands the first section back to the complete source block and loses every
    sibling boundary. Resolve each node first so its half-open source span stays
    authoritative in the presentation model.
    """

    children_by_parent: dict[str, list[str]] = {}
    for node in structure_nodes.values():
        if node.parent_structure_node_id is not None:
            children_by_parent.setdefault(node.parent_structure_node_id, []).append(
                node.structure_node_id
            )
    subtree_ids: set[str] = set()
    pending = [root_id]
    while pending:
        node_id = pending.pop()
        if node_id in subtree_ids:
            continue
        subtree_ids.add(node_id)
        pending.extend(children_by_parent.get(node_id, ()))
    ordered_nodes = sorted(
        (
            structure_nodes[node_id]
            for node_id in subtree_ids
            if node_id in structure_nodes
        ),
        key=lambda node: (node.source_order, node.structure_node_id),
    )
    refs: list[dict[str, Any]] = []
    for node in ordered_nodes:
        refs.extend(
            _resolve_structure_node_refs(
                node,
                source_catalog=source_catalog,
            )
        )
    return _dedupe_resolved_refs(refs)


def _structure_ancestor_path(
    node: StructureNode,
    *,
    structure_nodes: dict[str, StructureNode],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    ancestors: list[StructureNode] = []
    parent_id = node.parent_structure_node_id
    while parent_id is not None:
        parent = structure_nodes.get(parent_id)
        if parent is None:
            break
        ancestors.append(parent)
        parent_id = parent.parent_structure_node_id
    ancestors.reverse()
    return [
        {
            "structure_node_id": ancestor.structure_node_id,
            "kind": ancestor.kind.value,
            "label": _display_label(
                ancestor,
                _join_ref_text(
                    _resolve_structure_node_refs(
                        ancestor,
                        source_catalog=source_catalog,
                    )
                ),
            ),
        }
        for ancestor in ancestors
    ]


def _annotations_by_target(
    semantic_annotations: tuple[Any, ...],
) -> dict[str, tuple[Any, ...]]:
    grouped: dict[str, list[Any]] = {}
    for annotation in semantic_annotations:
        grouped.setdefault(annotation.target_structure_node_id, []).append(annotation)
    return {target_id: tuple(annotations) for target_id, annotations in grouped.items()}


def _semantic_classes_for_target(
    target_id: str,
    *,
    annotations_by_target: dict[str, tuple[Any, ...]],
) -> tuple[str, ...]:
    return tuple(
        _dedupe_strings(
            annotation.semantic_class.value
            for annotation in annotations_by_target.get(target_id, ())
        )
    )


def _combined_semantic_class(classes: tuple[str, ...]) -> str:
    if not classes:
        return "UNRESOLVED"
    return classes[0] if len(set(classes)) == 1 else "MIXED"


def _dedupe_resolved_refs(values: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None, int | None, str]] = set()
    for value in values:
        key = (
            str(value.get("kind") or ""),
            str(value.get("source_id") or ""),
            _optional_int(value.get("start_offset")),
            _optional_int(value.get("end_offset")),
            str(value.get("table_id") or ""),
        )
        if key in seen:
            continue
        result.append(value)
        seen.add(key)
    return result


def _context_blocks(
    refs: list[dict[str, Any]],
    *,
    tables_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    blocks_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    for ref in refs:
        table_id = str(ref.get("table_id") or "")
        if table_id:
            key = ("table", table_id)
            table = tables_by_id.get(table_id, {})
            block = blocks_by_key.setdefault(
                key,
                {
                    "kind": "table",
                    "source_id": str(
                        table.get("source_node_id") or ref.get("source_id") or ""
                    ),
                    "source_order": table.get("source_order", ref.get("source_order")),
                    "text": str(table.get("text") or ""),
                    "location": table.get("location") or ref.get("location"),
                    "table_id": table_id,
                    "page_start": table.get("page_start"),
                    "page_end": table.get("page_end"),
                },
            )
            if ref.get("kind") == "source_node":
                block["source_id"] = str(ref.get("source_id") or block["source_id"])
                block["location"] = ref.get("location") or block["location"]
            continue

        source_id = str(ref.get("source_id") or "")
        key = (
            str(ref.get("kind") or "text"),
            source_id,
            _optional_int(ref.get("start_offset")),
            _optional_int(ref.get("end_offset")),
        )
        if key in blocks_by_key:
            continue
        source_kind = str(ref.get("source_kind") or "").lower()
        block_kind = (
            "ocr"
            if ref.get("kind") == "visual_text_observation"
            else "image"
            if source_kind == "image"
            else "text"
        )
        blocks_by_key[key] = {
            "kind": block_kind,
            "source_id": source_id,
            "source_order": ref.get("source_order"),
            "text": (
                str(ref.get("text") or "")
                or ("原 PDF 图像区域" if block_kind == "image" else "")
            ),
            "location": ref.get("location"),
            "table_id": None,
            "start_offset": _optional_int(ref.get("start_offset")),
            "end_offset": _optional_int(ref.get("end_offset")),
            "structure_source_span_id": str(
                ref.get("structure_source_span_id") or ""
            )
            or None,
            "grounding_span_id": str(ref.get("grounding_span_id") or "") or None,
            "page_start": _page_from_location(ref.get("location")),
            "page_end": _page_from_location(ref.get("location")),
        }
    blocks = list(blocks_by_key.values())
    table_blocks = [block for block in blocks if block["kind"] == "table"]
    blocks = [
        block
        for block in blocks
        if block["kind"] == "table"
        or not any(
            _text_block_is_covered_by_table(block, table_block)
            for table_block in table_blocks
        )
    ]
    return sorted(blocks, key=_context_block_sort_key)


def _text_block_is_covered_by_table(
    block: dict[str, Any],
    table_block: dict[str, Any],
) -> bool:
    location = block.get("location") or {}
    table_location = table_block.get("location") or {}
    if (
        location.get("page_number") != table_location.get("page_number")
        or not str(table_block.get("text") or "").strip()
    ):
        return False
    block_text = _normalized_fidelity_text(block.get("text"))
    table_text = _normalized_fidelity_text(table_block.get("text"))
    if not block_text or block_text not in table_text:
        # Geometry overlap alone is not evidence that the table reconstruction
        # preserved this text. Keep unmatched notes and overlays visible.
        return False
    bbox = location.get("bbox")
    table_bbox = table_location.get("bbox")
    if (
        not isinstance(bbox, list)
        or len(bbox) != 4
        or not isinstance(table_bbox, list)
        or len(table_bbox) != 4
    ):
        return False
    x0, y0, x1, y1 = (float(value) for value in bbox)
    tx0, ty0, tx1, ty1 = (float(value) for value in table_bbox)
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if area <= 0.0:
        return False
    intersection = max(0.0, min(x1, tx1) - max(x0, tx0)) * max(
        0.0,
        min(y1, ty1) - max(y0, ty0),
    )
    return intersection / area >= 0.95


def _normalized_fidelity_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _context_block_sort_key(block: dict[str, Any]) -> tuple[Any, ...]:
    source_order = block.get("source_order")
    location = block.get("location") or {}
    page_number = block.get("page_start") or location.get("page_number") or 0
    bbox = location.get("bbox")
    y0 = bbox[1] if isinstance(bbox, list) and len(bbox) == 4 else float("inf")
    x0 = bbox[0] if isinstance(bbox, list) and len(bbox) == 4 else float("inf")
    block_index = location.get("block_index")
    table_index = location.get("table_index")
    return (
        page_number,
        y0,
        x0,
        block_index if isinstance(block_index, int) else 1_000_000,
        table_index if isinstance(table_index, int) else 1_000_000,
        source_order if isinstance(source_order, int) else 1_000_000,
        (
            block.get("start_offset")
            if isinstance(block.get("start_offset"), int)
            else 1_000_000
        ),
        block["kind"],
        block["source_id"],
    )


def _join_context_block_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        text for block in blocks if (text := str(block.get("text") or "").strip())
    )


def _pages_from_context_blocks(blocks: list[dict[str, Any]]) -> list[int]:
    return sorted(
        {
            page
            for block in blocks
            for page in (block.get("page_start"), block.get("page_end"))
            if isinstance(page, int)
        }
    )


def _page_from_location(location: Any) -> int | None:
    if not isinstance(location, dict):
        return None
    page_number = location.get("page_number")
    return page_number if isinstance(page_number, int) else None


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _table_ids_from_refs(refs: list[dict[str, Any]]) -> list[str]:
    return _dedupe_strings(
        str(ref.get("table_id") or "") for ref in refs if ref.get("table_id")
    )


def _dedupe_strings(values: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value or "")
        if not normalized or normalized in seen:
            continue
        result.append(normalized)
        seen.add(normalized)
    return result


def _locatable_page_range(
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> tuple[int | None, int | None]:
    pages = sorted(
        {
            page
            for fact in source_catalog.values()
            if (page := _page_from_location(fact.get("location"))) is not None
        }
    )
    return (pages[0], pages[-1]) if pages else (None, None)


def _serialize_finding(
    finding: StructureFinding,
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    source_refs = _resolve_refs(finding.source_refs, source_catalog)
    pages = sorted(
        {
            int(resolved["location"]["page_number"])
            for resolved in source_refs
            if resolved.get("location", {}).get("page_number") is not None
        }
    )
    return {
        "id": finding.finding_id,
        "code": finding.code,
        "severity": finding.severity.value,
        "blocking": finding.blocking,
        "message_key": finding.message_key,
        "structure_node_id": finding.structure_node_id,
        "page_start": pages[0] if pages else None,
        "page_end": pages[-1] if pages else None,
        "source_refs": source_refs,
    }


def _resolve_refs(
    refs: tuple[StructureSourceRef, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    for ref in refs:
        key = (ref.kind.value, ref.source_id)
        fact = source_catalog.get(key)
        if fact is None:
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                "A projected source reference could not be resolved for display.",
                stage="presenter",
            )
        resolved.append(dict(fact))
    return resolved


def _resolve_structure_node_refs(
    node: StructureNode,
    *,
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    if not node.semantic_source_spans:
        return _resolve_refs(node.source_refs, source_catalog)
    resolved: list[dict[str, Any]] = []
    spans_by_ref = {
        span.source_ref: span
        for span in node.semantic_source_spans
    }
    for source_ref in node.source_refs:
        fact = _resolve_refs((source_ref,), source_catalog)[0]
        span = spans_by_ref.get(source_ref)
        if span is not None:
            text = str(fact.get("text") or "")
            if span.end_offset > len(text):
                raise ShadowPreviewRejected(
                    ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                    "A structural text span exceeds its immutable source fact.",
                    stage="presenter",
                )
            fact["text"] = text[span.start_offset : span.end_offset]
            fact["start_offset"] = span.start_offset
            fact["end_offset"] = span.end_offset
            fact["structure_source_span_id"] = (
                span.structure_source_span_id
            )
        resolved.append(fact)
    return resolved


def _resolve_target_context_refs(
    *,
    target: StructureNode,
    context_source_refs: tuple[StructureSourceRef, ...],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Replace the target's full source facts with exact structural spans."""

    context_refs = _resolve_refs(context_source_refs, source_catalog)
    if not target.semantic_source_spans:
        return _dedupe_resolved_refs(context_refs)

    spanned_keys = {
        (span.source_ref.kind.value, span.source_ref.source_id)
        for span in target.semantic_source_spans
    }
    exact_by_key = {
        (source_ref.kind.value, source_ref.source_id): resolved
        for source_ref, resolved in zip(
            target.source_refs,
            _resolve_structure_node_refs(
                target,
                source_catalog=source_catalog,
            ),
            strict=True,
        )
        if (source_ref.kind.value, source_ref.source_id) in spanned_keys
    }
    replaced: list[dict[str, Any]] = []
    used_exact_keys: set[tuple[str, str]] = set()
    for resolved in context_refs:
        key = (
            str(resolved.get("kind") or ""),
            str(resolved.get("source_id") or ""),
        )
        exact = exact_by_key.get(key)
        if exact is not None:
            if key not in used_exact_keys:
                replaced.append(exact)
                used_exact_keys.add(key)
            continue
        replaced.append(resolved)
    for key, exact in exact_by_key.items():
        if key not in used_exact_keys:
            replaced.append(exact)
    return _dedupe_resolved_refs(replaced)


def _resolve_grounding_spans(
    span_ids: tuple[str, ...],
    *,
    annotations: list[Any],
    source_catalog: dict[tuple[str, str], dict[str, Any]],
) -> list[dict[str, Any]]:
    spans_by_id = {
        span.grounding_span_id: span
        for annotation in annotations
        for span in annotation.grounding_spans
    }
    resolved: list[dict[str, Any]] = []
    for span_id in span_ids:
        span = spans_by_id.get(span_id)
        if span is None:
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                "A projected grounding span could not be resolved for display.",
                stage="presenter",
            )
        fact = _resolve_refs((span.source_ref,), source_catalog)[0]
        text = str(fact.get("text") or "")
        if span.end_offset > len(text):
            raise ShadowPreviewRejected(
                ShadowPreviewRejectionCode.UNRESOLVED_SOURCE_REFERENCE,
                "A projected grounding span exceeds its immutable source fact.",
                stage="presenter",
            )
        fact["text"] = text[span.start_offset : span.end_offset]
        fact["start_offset"] = span.start_offset
        fact["end_offset"] = span.end_offset
        fact["grounding_span_id"] = span.grounding_span_id
        resolved.append(fact)
    return resolved


def _join_ref_text(refs: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        text = str(ref.get("text") or "").strip()
        if not text or text in seen:
            continue
        parts.append(text)
        seen.add(text)
    return "\n".join(parts)


def _display_label(node: StructureNode, text: str) -> str:
    first_line = next(
        (line.strip() for line in str(text or "").splitlines() if line.strip()),
        "",
    )
    label = str(node.explicit_label or "").strip()
    if (
        label
        and first_line
        and not first_line.startswith(label)
        and _CHINESE_OUTLINE_PREFIX.match(first_line) is None
    ):
        first_line = f"{label} {first_line}"
    return first_line or label or node.kind.value.replace("_", " ").title()


def _locator_payload(locator: Any) -> dict[str, Any] | None:
    if locator is None:
        return None
    return locator.model_dump(mode="json", exclude_none=True)


def _view_depths(nodes: dict[str, RequirementViewNode]) -> dict[str, int]:
    depths: dict[str, int] = {}

    def resolve(node_id: str, visiting: set[str]) -> int:
        if node_id in depths:
            return depths[node_id]
        if node_id in visiting:
            return 0
        node = nodes[node_id]
        if node.parent_requirement_view_node_id is None:
            depth = 0
        elif node.parent_requirement_view_node_id not in nodes:
            depth = 0
        else:
            depth = 1 + resolve(
                node.parent_requirement_view_node_id,
                {*visiting, node_id},
            )
        depths[node_id] = depth
        return depth

    for node_id in nodes:
        resolve(node_id, set())
    return depths


def _findings_by_candidate(
    candidates: tuple[RequirementViewNode, ...],
    findings: tuple[StructureFinding, ...],
) -> dict[str, tuple[StructureFinding, ...]]:
    result: dict[str, tuple[StructureFinding, ...]] = {}
    for candidate in candidates:
        structure_scope = {
            candidate.target_structure_node_id,
            *candidate.ancestor_structure_node_ids,
        }
        source_scope = {
            (ref.kind.value, ref.source_id)
            for ref in candidate.full_context_source_refs
        }
        result[candidate.requirement_view_node_id] = tuple(
            finding
            for finding in findings
            if finding.structure_node_id in structure_scope
            or any(
                (ref.kind.value, ref.source_id) in source_scope
                for ref in finding.source_refs
            )
        )
    return result


__all__ = [
    "SHADOW_PREVIEW_SCHEMA_VERSION",
    "ShadowPreviewRejected",
    "ShadowPreviewRejectionCode",
    "build_extraction_shadow_preview",
]
