from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from reqpilotx.domain.extraction.source_ir import (
    SourceAssetKind,
    SourceIRSnapshot,
    SourceNodeKind,
)


PDF_PAGE_RASTER_DIAGNOSTIC_CODES = frozenset(
    {
        "pdf_full_page_image_with_invisible_text_layer",
        "pdf_full_page_image_without_visible_text",
        "pdf_mrc_page_composite_render_failed",
        "pdf_mrc_scan_page_composited",
    }
)
PDF_PAGE_RASTER_MIN_PAGE_COVERAGE = 0.90


def collapse_pdf_page_raster_blocks(
    blocks: Iterable[dict[str, Any]],
    *,
    page_bbox: Iterable[float] | None,
) -> list[dict[str, Any]]:
    """Present many physical scan layers as one logical page support block.

    SourceIR keeps every physical node and asset. The page read model exposes a
    single support card with exact lineage so an MRC fallback cannot look like
    two independently adopted figures.
    """

    materialized = [dict(block) for block in blocks]
    rasters = [block for block in materialized if block.get("type") == "PAGE_RASTER"]
    if len(rasters) <= 1:
        return materialized
    primary = min(
        rasters,
        key=lambda block: (
            str(_record(block.get("asset")).get("kind") or "")
            != SourceAssetKind.PAGE_RENDER.value,
            int(block.get("source_order") or 0),
            str(block.get("id") or ""),
        ),
    )
    logical = dict(primary)
    normalized_page_bbox = _bbox(page_bbox)
    if normalized_page_bbox is not None:
        logical["bbox"] = list(normalized_page_bbox)
        logical["locatable"] = True
        logical["location_status"] = "LOCATABLE"
        source_location = dict(_record(logical.get("source_location")))
        source_location["bbox"] = list(normalized_page_bbox)
        logical["source_location"] = source_location
    logical["classification_basis"] = "presenter:logical_page_scan_support"
    logical["physical_layer_count"] = len(rasters)
    logical["physical_layer_source_refs"] = [
        dict(_record(block.get("source_ref"))) for block in rasters
    ]
    logical["physical_layer_ids"] = [str(block.get("id") or "") for block in rasters]
    retained = [block for block in materialized if block.get("type") != "PAGE_RASTER"]
    retained.append(logical)
    return sorted(
        retained,
        key=lambda block: (
            int(block.get("source_order") or 0),
            str(block.get("id") or ""),
        ),
    )


def is_proven_pdf_page_raster_fact(
    *,
    node_kind: SourceNodeKind | str,
    asset_kind: SourceAssetKind | str | None,
    image_bbox: Iterable[float] | None,
    page_bbox: Iterable[float] | None,
    diagnostic_codes: Iterable[str] = (),
) -> bool:
    """Return true only for a proven PDF page-canvas support fact.

    A generated PAGE_RENDER is authoritative asset identity. Original embedded
    IMAGE placements need both a page-raster diagnostic and clipped coverage of
    at least 90% of the visible page. Geometry alone never turns a content
    figure into page furniture.
    """

    normalized_node_kind = _enum_value(node_kind)
    normalized_asset_kind = _enum_value(asset_kind)
    if normalized_node_kind not in {
        SourceNodeKind.IMAGE.value,
        SourceNodeKind.FIGURE.value,
    }:
        return False
    if normalized_asset_kind == SourceAssetKind.PAGE_RENDER.value:
        return True
    if normalized_node_kind != SourceNodeKind.IMAGE.value:
        return False
    if not PDF_PAGE_RASTER_DIAGNOSTIC_CODES.intersection(
        str(code or "") for code in diagnostic_codes
    ):
        return False
    return (
        clipped_page_coverage(image_bbox=image_bbox, page_bbox=page_bbox)
        >= PDF_PAGE_RASTER_MIN_PAGE_COVERAGE
    )


def proven_pdf_page_raster_source_ids(
    snapshot: SourceIRSnapshot,
) -> frozenset[str]:
    """Identify immutable SourceIR nodes that are page-canvas support only."""

    if not isinstance(snapshot, SourceIRSnapshot):
        raise TypeError("snapshot must be a SourceIRSnapshot")
    assets_by_id = {asset.asset_id: asset for asset in snapshot.assets}
    page_diagnostic_codes: dict[int, set[str]] = defaultdict(set)
    page_bounds: dict[int, tuple[float, float, float, float]] = {}
    for diagnostic in snapshot.diagnostics:
        locator = diagnostic.native_locator
        if locator is None or locator.page_number is None:
            continue
        page_number = locator.page_number
        page_diagnostic_codes[page_number].add(diagnostic.code)
        if (
            diagnostic.code in PDF_PAGE_RASTER_DIAGNOSTIC_CODES
            and locator.bbox is not None
        ):
            page_bounds.setdefault(page_number, locator.bbox)

    source_ids: set[str] = set()
    for node in snapshot.nodes:
        page_number = node.native_locator.page_number
        asset = assets_by_id.get(node.asset_ref or "")
        diagnostic_codes = set(page_diagnostic_codes.get(page_number or 0, ()))
        diagnostic_codes.update(
            diagnostic.code for diagnostic in node.extraction_diagnostics
        )
        inferred_page_bbox = page_bounds.get(page_number or 0)
        if is_proven_pdf_page_raster_fact(
            node_kind=node.kind,
            asset_kind=asset.kind if asset is not None else None,
            image_bbox=(
                node.visual_locator.bbox
                if node.visual_locator is not None
                and node.visual_locator.bbox is not None
                else node.native_locator.bbox
            ),
            page_bbox=inferred_page_bbox,
            diagnostic_codes=diagnostic_codes,
        ):
            source_ids.add(node.source_node_id)
    return frozenset(source_ids)


def clipped_page_coverage(
    *,
    image_bbox: Iterable[float] | None,
    page_bbox: Iterable[float] | None,
) -> float:
    image = _bbox(image_bbox)
    page = _bbox(page_bbox)
    if image is None or page is None:
        return 0.0
    page_area = _area(page)
    if page_area <= 0.0:
        return 0.0
    intersection = (
        max(image[0], page[0]),
        max(image[1], page[1]),
        min(image[2], page[2]),
        min(image[3], page[3]),
    )
    return _area(intersection) / page_area


def _bbox(value: Iterable[float] | None) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    try:
        converted = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if len(converted) != 4 or converted[2] < converted[0] or converted[3] < converted[1]:
        return None
    return converted


def _area(bbox: tuple[float, float, float, float]) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


__all__ = [
    "PDF_PAGE_RASTER_DIAGNOSTIC_CODES",
    "PDF_PAGE_RASTER_MIN_PAGE_COVERAGE",
    "clipped_page_coverage",
    "collapse_pdf_page_raster_blocks",
    "is_proven_pdf_page_raster_fact",
    "proven_pdf_page_raster_source_ids",
]
