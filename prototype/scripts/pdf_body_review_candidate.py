"""Build text-free, non-Gold PDF body review candidates.

The candidate may be derived from the current native/visual capture, but it is
never an approval.  Every image and table occurrence is exposed with a stable
identity and starts as ``UNRESOLVED`` unless an external review decision is
provided.  ``prepare_pdf_body_review_draft`` remains the contract boundary that
marks the result as controlled, non-human Gold.

This module deliberately performs no file, network, credential, or reviewer
registry I/O.  It also does not create approval or READY artifacts.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from app.domain.extraction.pdf_page_furniture import visual_pdf_page_furniture_ids
from app.domain.extraction.source_ir import SourceNodeKind
from scripts.extraction_case_runner import (
    _canonical_pdf_body_review_table_topology,
    _canonical_pdf_physical_lines,
    _measure_pdf_body_page,
    _pdf_body_review_image_occurrence_identity,
    _pdf_body_review_native_table_records,
)
from scripts.pdf_body_review_artifacts import (
    PDF_BODY_REVIEW_EVALUATOR_ID,
    PROFILE_SCHEMA_VERSION,
    canonical_json_sha256,
    canonical_pdf_image_occurrence_sha256,
    canonical_pdf_table_occurrence_sha256,
    prepare_pdf_body_review_draft,
    summarize_pdf_content_image_occurrences,
    summarize_pdf_content_table_occurrences,
)
from scripts.pdf_body_review_table_capture import (
    BAILIAN_DOCUMENT_PARSING_PROVIDER_ID,
    TABLE_CAPTURE_SCHEMA_VERSION,
    canonical_review_table_occurrence_sha256,
    validate_bailian_document_parsing_table_capture,
)


_ALLOWED_DISPOSITIONS = frozenset({"CONTENT", "NON_CONTENT", "UNRESOLVED"})
_EXCLUDED_NODE_KINDS = frozenset(
    {
        SourceNodeKind.HEADER.value,
        SourceNodeKind.FOOTER.value,
        SourceNodeKind.WATERMARK.value,
        SourceNodeKind.TOC_ENTRY.value,
    }
)


class PdfBodyReviewCandidateError(ValueError):
    """Fail-closed candidate construction error with a stable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def prepare_pdf_body_review_candidate_draft(
    snapshot: Any,
    *,
    assertion_id: str,
    document_id: str,
    body_page_start: int,
    body_page_end: int,
    review_mode: str,
    image_dispositions_by_occurrence: Mapping[str, str] | None = None,
    table_dispositions_by_occurrence: Mapping[str, str] | None = None,
    remote_table_captures_by_page: Mapping[int, Mapping[str, Any]] | None = None,
    approved_visual_provider: str | None = None,
) -> dict[str, Any]:
    """Return a validated, text-free DRAFT for an external human review.

    Omitted decisions remain ``UNRESOLVED``.  Unknown decision keys are
    rejected, preventing a stale review decision set from silently applying to
    a changed candidate.
    """

    source_sha256 = _require_sha256(
        getattr(snapshot, "source_sha256", None),
        "candidate_source_sha256_invalid",
    )
    page_numbers = _page_numbers(body_page_start, body_page_end)
    image_decisions = _validated_decisions(
        image_dispositions_by_occurrence,
        code_prefix="candidate_image_decisions",
    )
    table_decisions = _validated_decisions(
        table_dispositions_by_occurrence,
        code_prefix="candidate_table_decisions",
    )

    visual_by_page: dict[int, Any] = {}
    visual_furniture_ids = frozenset()
    profile_visual_fields: dict[str, str] = {}
    table_captures: dict[int, Mapping[str, Any]] = {}
    if review_mode == "REMOTE_VISUAL":
        if approved_visual_provider != "bailian":
            _fail("candidate_remote_visual_provider_invalid")
        table_captures = _validate_remote_table_capture_set(
            remote_table_captures_by_page,
            source_sha256=source_sha256,
            page_numbers=page_numbers,
        )
        provider_id = BAILIAN_DOCUMENT_PARSING_PROVIDER_ID
        visual_snapshots = tuple(
            item
            for item in getattr(snapshot, "visual_text_snapshots", ())
            if getattr(item, "provider_id", None) == provider_id
        )
        if not visual_snapshots:
            _fail("candidate_visual_snapshots_missing")
        visual_versions = {
            getattr(item, "provider_version", None) for item in visual_snapshots
        }
        if len(visual_versions) != 1 or not all(
            isinstance(item, str) and item for item in visual_versions
        ):
            _fail("candidate_visual_provider_version_inconsistent")
        visual_version = next(iter(visual_versions))
        for page_number in page_numbers:
            captures = [
                item
                for item in visual_snapshots
                if getattr(item, "page_number", None) == page_number
            ]
            if len(captures) != 1:
                _fail("candidate_visual_page_capture_not_unique")
            visual_by_page[page_number] = captures[0]
        visual_furniture_ids = visual_pdf_page_furniture_ids(visual_snapshots)
        table_versions = {
            str(capture["provider_version"]) for capture in table_captures.values()
        }
        if len(table_versions) != 1:
            _fail("candidate_table_provider_version_inconsistent")
        profile_visual_fields = {
            "approved_visual_provider": approved_visual_provider,
            "visual_provider_id": provider_id,
            "visual_text_provider_version_sha256": _sha256_text(visual_version),
            "visual_table_provider_version_sha256": _sha256_text(
                next(iter(table_versions))
            ),
        }
    elif review_mode == "NATIVE":
        if approved_visual_provider is not None:
            _fail("candidate_native_forbids_visual_provider")
        if remote_table_captures_by_page:
            _fail("candidate_native_forbids_remote_table_captures")
    else:
        _fail("candidate_review_mode_invalid")

    all_image_occurrence_ids: set[str] = set()
    all_table_occurrence_ids: set[str] = set()
    pages: list[dict[str, Any]] = []
    for page_number in page_numbers:
        image_occurrences = _page_image_occurrences(
            snapshot,
            page_number=page_number,
            dispositions=image_decisions,
        )
        all_image_occurrence_ids.update(
            str(item["occurrence_sha256"]) for item in image_occurrences
        )
        if review_mode == "NATIVE":
            table_pairs = _native_page_table_occurrences(
                snapshot,
                page_number=page_number,
                dispositions=table_decisions,
            )
            page_image_sha256 = None
            visual_snapshot = None
        else:
            visual_snapshot = visual_by_page[page_number]
            capture = table_captures[page_number]
            if capture["page_image_sha256"] != getattr(
                visual_snapshot, "page_image_sha256", None
            ):
                _fail("candidate_table_text_page_image_mismatch")
            table_pairs = _remote_page_table_occurrences(
                capture,
                dispositions=table_decisions,
            )
            page_image_sha256 = _require_sha256(
                getattr(visual_snapshot, "page_image_sha256", None),
                "candidate_page_image_sha256_invalid",
            )
        table_occurrences = [item[0] for item in table_pairs]
        content_table_topologies = [
            topology
            for occurrence, topology in table_pairs
            if occurrence["disposition"] == "CONTENT"
        ]
        all_table_occurrence_ids.update(
            str(item["occurrence_sha256"]) for item in table_occurrences
        )

        measurement = _measure_pdf_body_page(
            snapshot,
            page_number=page_number,
            review_mode=review_mode,
            visual_snapshot=visual_snapshot,
            excluded_visual_observation_ids=visual_furniture_ids,
            reviewed_image_occurrences=image_occurrences,
            reviewed_table_occurrences=(
                table_occurrences if review_mode == "NATIVE" else None
            ),
            table_topologies_override=(
                content_table_topologies
                if review_mode == "REMOTE_VISUAL"
                else None
            ),
        )
        if not measurement["image_inventory_matches"]:
            _fail("candidate_image_inventory_drift")
        if not measurement["table_inventory_matches"]:
            _fail("candidate_table_inventory_drift")
        image_summary = summarize_pdf_content_image_occurrences(image_occurrences)
        table_summary = summarize_pdf_content_table_occurrences(table_occurrences)
        if any(
            int(measurement[measurement_name]) != int(table_summary[expected_name])
            for measurement_name, expected_name in (
                ("table_count", "expected_table_count"),
                ("table_row_count", "expected_table_row_count"),
                ("table_cell_count", "expected_table_cell_count"),
                ("merged_region_count", "expected_merged_region_count"),
            )
        ):
            _fail("candidate_table_summary_drift")
        page: dict[str, Any] = {
            "page_number": page_number,
            "expected_body_line_count": int(measurement["body_line_count"]),
            **image_summary,
            "image_occurrences": image_occurrences,
            **table_summary,
            "table_occurrences": table_occurrences,
            "content_multiset_sha256": measurement["content_multiset_sha256"],
            "sequence_sha256": measurement["sequence_sha256"],
        }
        if page_image_sha256 is not None:
            page["page_image_sha256"] = page_image_sha256
        pages.append(page)

    if set(image_decisions) != all_image_occurrence_ids.intersection(image_decisions):
        _fail("candidate_image_decision_unknown_occurrence")
    if set(table_decisions) != all_table_occurrence_ids.intersection(table_decisions):
        _fail("candidate_table_decision_unknown_occurrence")

    profile = {
        "schema_version": PROFILE_SCHEMA_VERSION,
        "assertion_id": assertion_id,
        "evaluator_id": PDF_BODY_REVIEW_EVALUATOR_ID,
        "document_id": document_id,
        "source_sha256": source_sha256,
        "body_page_start": body_page_start,
        "body_page_end": body_page_end,
        "review_mode": review_mode,
        **profile_visual_fields,
        "pages": pages,
    }
    return prepare_pdf_body_review_draft(profile)


def _page_image_occurrences(
    snapshot: Any,
    *,
    page_number: int,
    dispositions: Mapping[str, str],
) -> list[dict[str, Any]]:
    assets_by_id = {
        getattr(asset, "asset_id", None): asset
        for asset in getattr(snapshot, "assets", ())
    }
    occurrences: list[dict[str, Any]] = []
    for node in getattr(snapshot, "nodes", ()):
        native = getattr(node, "native_locator", None)
        native_format = _enum_value(getattr(native, "format", None))
        kind = _enum_value(getattr(node, "kind", None))
        if (
            native_format != "pdf"
            or getattr(native, "page_number", None) != page_number
            or kind in _EXCLUDED_NODE_KINDS
            or kind not in {SourceNodeKind.IMAGE.value, SourceNodeKind.FIGURE.value}
        ):
            continue
        asset = assets_by_id.get(getattr(node, "asset_ref", None))
        if asset is None:
            _fail("candidate_image_asset_missing")
        identity = _pdf_body_review_image_occurrence_identity(
            node,
            asset=asset,
            node_kind=kind,
            native_format=native_format,
        )
        occurrence_sha256 = canonical_pdf_image_occurrence_sha256(identity)
        occurrences.append(
            {
                "occurrence_sha256": occurrence_sha256,
                **identity,
                "disposition": dispositions.get(
                    occurrence_sha256,
                    "UNRESOLVED",
                ),
            }
        )
    occurrences.sort(key=lambda item: int(item["source_order"]))
    return occurrences


def _native_page_table_occurrences(
    snapshot: Any,
    *,
    page_number: int,
    dispositions: Mapping[str, str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for identity, topology in _pdf_body_review_native_table_records(
        snapshot,
        page_number=page_number,
    ):
        occurrence_sha256 = canonical_pdf_table_occurrence_sha256(identity)
        pairs.append(
            (
                {
                    "occurrence_sha256": occurrence_sha256,
                    **identity,
                    "disposition": dispositions.get(
                        occurrence_sha256,
                        "UNRESOLVED",
                    ),
                },
                topology,
            )
        )
    return pairs


def _remote_page_table_occurrences(
    capture: Mapping[str, Any],
    *,
    dispositions: Mapping[str, str],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for expected_source_order, table in enumerate(capture["tables"]):
        if table.get("source_order") != expected_source_order:
            _fail("candidate_remote_table_source_order_invalid")
        provider_index = table.get("provider_index")
        if (
            isinstance(provider_index, bool)
            or not isinstance(provider_index, int)
            or provider_index < 0
        ):
            _fail("candidate_remote_table_provider_index_invalid")
        if canonical_review_table_occurrence_sha256(capture, table) != table.get(
            "occurrence_sha256"
        ):
            _fail("candidate_remote_table_capture_hash_mismatch")
        provider_unique_id = table.get("provider_unique_id")
        if not isinstance(provider_unique_id, str) or not provider_unique_id:
            _fail("candidate_remote_table_provider_id_invalid")
        topology = _canonical_pdf_body_review_table_topology(
            {
                "row_count": table.get("row_count"),
                "column_count": table.get("column_count"),
                "cells": [
                    {
                        "row": cell.get("row"),
                        "column": cell.get("column"),
                        "row_span": cell.get("row_span"),
                        "column_span": cell.get("column_span"),
                        "text_lines": _canonical_pdf_physical_lines(
                            cell.get("raw_text", "")
                        ),
                    }
                    for cell in table.get("cells", [])
                ],
            }
        )
        identity = _table_occurrence_identity(
            provider_object_id_sha256=canonical_json_sha256(
                {
                    "provider_unique_id": provider_unique_id,
                    "provider_index": provider_index,
                    "capture_occurrence_sha256": table["occurrence_sha256"],
                }
            ),
            # The artifact inventory is ordered in canonical visual source
            # order.  The provider's raw layout index remains bound inside the
            # object hash above and may legitimately be non-monotonic.
            provider_index=expected_source_order,
            page_number=int(capture["page_number"]),
            bbox=_bbox(
                table.get("table_bbox"),
                "candidate_remote_table_bbox_invalid",
            ),
            topology=topology,
        )
        occurrence_sha256 = canonical_pdf_table_occurrence_sha256(identity)
        pairs.append(
            (
                {
                    "occurrence_sha256": occurrence_sha256,
                    **identity,
                    "disposition": dispositions.get(
                        occurrence_sha256,
                        "UNRESOLVED",
                    ),
                },
                topology,
            )
        )
    return pairs


def _table_occurrence_identity(
    *,
    provider_object_id_sha256: str,
    provider_index: int,
    page_number: int,
    bbox: list[float],
    topology: Mapping[str, Any],
) -> dict[str, Any]:
    cells = list(topology["cells"])
    text_free_topology = {
        "row_count": int(topology["row_count"]),
        "column_count": int(topology["column_count"]),
        "cells": [
            {
                "row": int(cell["row"]),
                "column": int(cell["column"]),
                "row_span": int(cell["row_span"]),
                "column_span": int(cell["column_span"]),
            }
            for cell in cells
        ],
    }
    return {
        "provider_object_id_sha256": provider_object_id_sha256,
        "provider_index": provider_index,
        "page_number": page_number,
        "bbox": bbox,
        "row_count": int(topology["row_count"]),
        "column_count": int(topology["column_count"]),
        "cell_count": len(cells),
        "merged_region_count": sum(
            1
            for cell in cells
            if int(cell["row_span"]) > 1 or int(cell["column_span"]) > 1
        ),
        "topology_sha256": canonical_json_sha256(text_free_topology),
    }


def _validate_remote_table_capture_set(
    captures: Mapping[int, Mapping[str, Any]] | None,
    *,
    source_sha256: str,
    page_numbers: list[int],
) -> dict[int, Mapping[str, Any]]:
    if not isinstance(captures, Mapping):
        _fail("candidate_remote_table_captures_missing")
    if set(captures) != set(page_numbers):
        _fail("candidate_remote_table_capture_pages_mismatch")
    validated: dict[int, Mapping[str, Any]] = {}
    for page_number in page_numbers:
        capture = captures[page_number]
        if not isinstance(capture, Mapping):
            _fail("candidate_remote_table_capture_invalid")
        try:
            capture = validate_bailian_document_parsing_table_capture(capture)
        except ValueError as exc:
            raise PdfBodyReviewCandidateError(
                "candidate_remote_table_capture_invalid"
            ) from exc
        if capture.get("schema_version") != TABLE_CAPTURE_SCHEMA_VERSION:
            _fail("candidate_remote_table_capture_schema_mismatch")
        if capture.get("source_sha256") != source_sha256:
            _fail("candidate_remote_table_capture_source_mismatch")
        if capture.get("page_number") != page_number:
            _fail("candidate_remote_table_capture_page_mismatch")
        validated[page_number] = capture
    return validated


def _validated_decisions(
    decisions: Mapping[str, str] | None,
    *,
    code_prefix: str,
) -> dict[str, str]:
    if decisions is None:
        return {}
    if not isinstance(decisions, Mapping):
        _fail(f"{code_prefix}_invalid")
    result: dict[str, str] = {}
    for occurrence_sha256, disposition in decisions.items():
        digest = _require_sha256(
            occurrence_sha256,
            f"{code_prefix}_occurrence_sha256_invalid",
        )
        if disposition not in _ALLOWED_DISPOSITIONS:
            _fail(f"{code_prefix}_disposition_invalid")
        result[digest] = disposition
    return result


def _page_numbers(start: Any, end: Any) -> list[int]:
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or start < 1
        or isinstance(end, bool)
        or not isinstance(end, int)
        or end < start
    ):
        _fail("candidate_page_range_invalid")
    return list(range(start, end + 1))


def _bbox(value: Any, code: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        _fail(code)
    try:
        bbox = [float(item) for item in value]
    except (TypeError, ValueError) as exc:
        raise PdfBodyReviewCandidateError(code) from exc
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        _fail(code)
    return bbox


def _require_sha256(value: Any, code: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(code)
    return value


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fail(code: str) -> None:
    raise PdfBodyReviewCandidateError(code)


__all__ = [
    "PdfBodyReviewCandidateError",
    "prepare_pdf_body_review_candidate_draft",
]
