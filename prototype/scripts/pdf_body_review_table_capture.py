"""Pure review-side conversion for Bailian ``document_parsing`` tables.

The converter accepts an already obtained provider response plus an explicit
page-image identity.  It performs no file, network, credential, or review
registry I/O.  Its output is evidence for a controlled human review; every
table disposition therefore starts as ``UNRESOLVED``.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


TABLE_CAPTURE_SCHEMA_VERSION = "pdf_body_review_table_capture.v1"
BAILIAN_DOCUMENT_PARSING_PROVIDER_ID = "aliyun-bailian-qwen-ocr"
BAILIAN_DOCUMENT_PARSING_TASK = "document_parsing"
BAILIAN_DOCUMENT_TABLE_PARSER_VERSION = "document-layout-table-grid/1"
BAILIAN_DOCUMENT_CAPTURE_IMPLEMENTATION_VERSION = "1.0.0"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_SAFE_VERSION_PART_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,159}$")
_MEDIA_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")
_MAX_PROVIDER_LAYOUTS = 100_000
_MAX_TABLE_ROWS = 4_000
_MAX_TABLE_COLUMNS = 1_000
_MAX_TABLE_CELLS = 500_000
_CAPTURE_FIELDS = frozenset(
    {
        "schema_version",
        "source_sha256",
        "page_number",
        "page_bbox",
        "page_image_sha256",
        "page_image_media_type",
        "page_image_width_px",
        "page_image_height_px",
        "provider_id",
        "provider_version",
        "task",
        "parser_version",
        "table_count",
        "tables",
    }
)
_CAPTURE_TABLE_FIELDS = frozenset(
    {
        "provider_unique_id",
        "provider_page_number",
        "provider_index",
        "table_bbox",
        "row_count",
        "column_count",
        "cells",
        "source_order",
        "occurrence_sha256",
        "disposition",
    }
)
_CAPTURE_CELL_FIELDS = frozenset(
    {"row", "column", "row_span", "column_span", "raw_text"}
)


class PdfBodyReviewTableCaptureError(ValueError):
    """Fail-closed conversion error with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def build_bailian_document_parsing_provider_version(
    *,
    model: str,
    seed: int,
    temperature: int | float,
    task: str = BAILIAN_DOCUMENT_PARSING_TASK,
    parser_version: str = BAILIAN_DOCUMENT_TABLE_PARSER_VERSION,
) -> str:
    """Return the deterministic identity for one capture configuration."""

    normalized_model = _require_model(model)
    normalized_seed = _require_integer(
        seed,
        "provider_seed_invalid",
        minimum=0,
        maximum=2**31 - 1,
    )
    normalized_temperature = _require_temperature(temperature)
    if task != BAILIAN_DOCUMENT_PARSING_TASK:
        _fail("provider_task_must_be_document_parsing")
    normalized_parser_version = _require_safe_version_part(
        parser_version,
        "parser_version_invalid",
    )
    return (
        f"{BAILIAN_DOCUMENT_CAPTURE_IMPLEMENTATION_VERSION};"
        f"model={normalized_model};task={task};seed={normalized_seed};"
        f"temperature={repr(normalized_temperature)};"
        f"parser={normalized_parser_version}"
    )


def convert_bailian_document_parsing_tables(
    response_payload: Mapping[str, Any],
    *,
    source_sha256: str,
    page_number: int,
    page_bbox: Sequence[int | float],
    page_image_sha256: str,
    page_image_media_type: str,
    page_image_width_px: int,
    page_image_height_px: int,
    provider_version: str,
    provider_id: str = BAILIAN_DOCUMENT_PARSING_PROVIDER_ID,
    parser_version: str = BAILIAN_DOCUMENT_TABLE_PARSER_VERSION,
) -> dict[str, Any]:
    """Convert a single-page Bailian response into review-only table facts.

    ``response_payload`` must be the decoded JSON response for the exact page
    image described by the keyword arguments.  No provider topology is repaired
    here: overlaps, gaps, out-of-range spans, and conflicting duplicate layouts
    are rejected.
    """

    binding = _validate_binding(
        source_sha256=source_sha256,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_media_type=page_image_media_type,
        page_image_width_px=page_image_width_px,
        page_image_height_px=page_image_height_px,
        provider_id=provider_id,
        provider_version=provider_version,
        parser_version=parser_version,
    )
    layouts = _response_layouts(response_payload)
    unique_layouts = _deduplicate_top_level_layouts(layouts)

    parsed_tables: list[dict[str, Any]] = []
    for layout in unique_layouts:
        if layout.get("type") != "table":
            continue
        parsed_tables.append(
            _parse_table_layout(
                layout,
                binding=binding,
            )
        )

    parsed_tables.sort(key=_canonical_table_sort_key)
    tables: list[dict[str, Any]] = []
    for source_order, table in enumerate(parsed_tables):
        table["source_order"] = source_order
        identity = _table_occurrence_identity(binding=binding, table=table)
        table["occurrence_sha256"] = _canonical_json_sha256(identity)
        table["disposition"] = "UNRESOLVED"
        tables.append(table)

    return {
        "schema_version": TABLE_CAPTURE_SCHEMA_VERSION,
        **binding,
        "table_count": len(tables),
        "tables": tables,
    }


def canonical_review_table_occurrence_sha256(
    capture: Mapping[str, Any],
    occurrence: Mapping[str, Any],
) -> str:
    """Recompute one occurrence identity hash, ignoring its disposition."""

    capture_object = _require_mapping(capture, "capture_not_object")
    binding = _validate_binding(
        source_sha256=capture_object.get("source_sha256"),
        page_number=capture_object.get("page_number"),
        page_bbox=capture_object.get("page_bbox"),
        page_image_sha256=capture_object.get("page_image_sha256"),
        page_image_media_type=capture_object.get("page_image_media_type"),
        page_image_width_px=capture_object.get("page_image_width_px"),
        page_image_height_px=capture_object.get("page_image_height_px"),
        provider_id=capture_object.get("provider_id"),
        provider_version=capture_object.get("provider_version"),
        parser_version=capture_object.get("parser_version"),
    )
    if capture_object.get("task") != BAILIAN_DOCUMENT_PARSING_TASK:
        _fail("capture_task_mismatch")
    table = _validate_occurrence_for_hash(occurrence)
    return _canonical_json_sha256(
        _table_occurrence_identity(binding=binding, table=table)
    )


def validate_bailian_document_parsing_table_capture(
    capture_payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and detach one normalized provider table capture.

    Stored captures are immutable provider evidence, not reviewed profiles.
    Consequently every table must retain the converter's ``UNRESOLVED``
    disposition.  Review decisions belong in the separately hashed body-review
    draft.
    """

    capture = _require_mapping(capture_payload, "capture_not_object")
    _require_exact_fields(capture, _CAPTURE_FIELDS, "capture_fields_invalid")
    if capture.get("schema_version") != TABLE_CAPTURE_SCHEMA_VERSION:
        _fail("capture_schema_version_mismatch")

    binding = _validate_binding(
        source_sha256=capture.get("source_sha256"),
        page_number=capture.get("page_number"),
        page_bbox=capture.get("page_bbox"),
        page_image_sha256=capture.get("page_image_sha256"),
        page_image_media_type=capture.get("page_image_media_type"),
        page_image_width_px=capture.get("page_image_width_px"),
        page_image_height_px=capture.get("page_image_height_px"),
        provider_id=capture.get("provider_id"),
        provider_version=capture.get("provider_version"),
        parser_version=capture.get("parser_version"),
    )
    if capture.get("task") != BAILIAN_DOCUMENT_PARSING_TASK:
        _fail("capture_task_mismatch")

    table_count = _require_integer(
        capture.get("table_count"),
        "capture_table_count_invalid",
        minimum=0,
        maximum=_MAX_PROVIDER_LAYOUTS,
    )
    raw_tables = capture.get("tables")
    if not isinstance(raw_tables, list):
        _fail("capture_tables_not_array")
    if len(raw_tables) > _MAX_PROVIDER_LAYOUTS:
        _fail("capture_table_limit_exceeded")
    if table_count != len(raw_tables):
        _fail("capture_table_count_mismatch")

    tables: list[dict[str, Any]] = []
    seen_unique_ids: set[str] = set()
    seen_provider_indexes: set[int] = set()
    seen_bboxes: set[bytes] = set()
    seen_occurrence_ids: set[str] = set()
    for source_order, raw_table in enumerate(raw_tables):
        table = _validate_captured_table(
            raw_table,
            binding=binding,
            expected_source_order=source_order,
        )
        if table["provider_unique_id"] in seen_unique_ids:
            _fail("capture_table_unique_id_duplicate")
        seen_unique_ids.add(table["provider_unique_id"])
        if table["provider_index"] in seen_provider_indexes:
            _fail("capture_table_provider_index_duplicate")
        seen_provider_indexes.add(table["provider_index"])
        bbox_key = _canonical_json_bytes(table["table_bbox"])
        if bbox_key in seen_bboxes:
            _fail("capture_table_bbox_duplicate")
        seen_bboxes.add(bbox_key)
        if table["occurrence_sha256"] in seen_occurrence_ids:
            _fail("capture_table_occurrence_duplicate")
        seen_occurrence_ids.add(table["occurrence_sha256"])
        tables.append(table)

    if tables != sorted(tables, key=_canonical_table_sort_key):
        _fail("capture_tables_not_canonical_order")

    return {
        "schema_version": TABLE_CAPTURE_SCHEMA_VERSION,
        **binding,
        "table_count": table_count,
        "tables": _json_clone(tables),
    }


def _validate_binding(
    *,
    source_sha256: object,
    page_number: object,
    page_bbox: object,
    page_image_sha256: object,
    page_image_media_type: object,
    page_image_width_px: object,
    page_image_height_px: object,
    provider_id: object,
    provider_version: object,
    parser_version: object,
) -> dict[str, Any]:
    source_digest = _require_sha256(source_sha256, "source_sha256_invalid")
    image_digest = _require_sha256(
        page_image_sha256,
        "page_image_sha256_invalid",
    )
    normalized_page_number = _require_integer(
        page_number,
        "page_number_invalid",
        minimum=1,
    )
    normalized_page_bbox = _require_bbox(page_bbox, "page_bbox_invalid")
    image_width = _require_integer(
        page_image_width_px,
        "page_image_width_invalid",
        minimum=1,
    )
    image_height = _require_integer(
        page_image_height_px,
        "page_image_height_invalid",
        minimum=1,
    )
    if (
        not isinstance(page_image_media_type, str)
        or _MEDIA_TYPE_PATTERN.fullmatch(page_image_media_type) is None
    ):
        _fail("page_image_media_type_invalid")
    if provider_id != BAILIAN_DOCUMENT_PARSING_PROVIDER_ID:
        _fail("provider_id_mismatch")
    normalized_provider_version = _require_nonblank_string(
        provider_version,
        "provider_version_invalid",
        maximum_length=320,
    )
    normalized_parser_version = _require_safe_version_part(
        parser_version,
        "parser_version_invalid",
    )
    return {
        "source_sha256": source_digest,
        "page_number": normalized_page_number,
        "page_bbox": normalized_page_bbox,
        "page_image_sha256": image_digest,
        "page_image_media_type": page_image_media_type,
        "page_image_width_px": image_width,
        "page_image_height_px": image_height,
        "provider_id": provider_id,
        "provider_version": normalized_provider_version,
        "task": BAILIAN_DOCUMENT_PARSING_TASK,
        "parser_version": normalized_parser_version,
    }


def _response_layouts(response_payload: object) -> list[Mapping[str, Any]]:
    payload = _require_mapping(response_payload, "response_not_object")
    output = _require_mapping(payload.get("output"), "response_output_missing")
    choices = output.get("choices")
    if not isinstance(choices, list) or len(choices) != 1:
        _fail("response_choice_count_invalid")
    choice = _require_mapping(choices[0], "response_choice_invalid")
    message = _require_mapping(
        choice.get("message"),
        "response_message_missing",
    )
    content = message.get("content")
    if not isinstance(content, list):
        _fail("response_content_missing")

    layout_sets = 0
    layouts: list[Mapping[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            continue
        ocr_result = part.get("ocr_result")
        if not isinstance(ocr_result, Mapping) or "layouts" not in ocr_result:
            continue
        candidate_layouts = ocr_result.get("layouts")
        if not isinstance(candidate_layouts, list):
            _fail("response_layouts_not_array")
        layout_sets += 1
        for layout in candidate_layouts:
            layouts.append(_require_mapping(layout, "response_layout_not_object"))
            if len(layouts) > _MAX_PROVIDER_LAYOUTS:
                _fail("response_layout_limit_exceeded")
    if layout_sets == 0:
        _fail("response_layouts_missing")
    return layouts


def _deduplicate_top_level_layouts(
    layouts: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Deduplicate only exact top-level ``(pageNum, uniqueId)`` repeats."""

    seen: dict[tuple[int, str], bytes] = {}
    result: list[Mapping[str, Any]] = []
    for layout in layouts:
        provider_page = layout.get("pageNum")
        unique_id = layout.get("uniqueId")
        if (
            isinstance(provider_page, int)
            and not isinstance(provider_page, bool)
            and isinstance(unique_id, str)
            and bool(unique_id)
        ):
            key = (provider_page, unique_id)
            payload = _canonical_json_bytes(layout)
            previous = seen.get(key)
            if previous is not None:
                if previous != payload:
                    _fail("top_level_layout_identity_conflict")
                continue
            seen[key] = payload
        result.append(layout)
    return result


def _parse_table_layout(
    layout: Mapping[str, Any],
    *,
    binding: Mapping[str, Any],
) -> dict[str, Any]:
    provider_unique_id = _require_nonblank_string(
        layout.get("uniqueId"),
        "table_unique_id_invalid",
        maximum_length=320,
    )
    provider_page_number = _require_integer(
        layout.get("pageNum"),
        "table_provider_page_number_invalid",
        minimum=0,
    )
    if provider_page_number != 0:
        _fail("table_provider_page_number_invalid")
    provider_index = _require_integer(
        layout.get("index"),
        "table_provider_index_invalid",
        minimum=0,
    )
    row_count = _require_integer(
        layout.get("numRow"),
        "table_row_count_invalid",
        minimum=1,
        maximum=_MAX_TABLE_ROWS,
    )
    column_count = _require_integer(
        layout.get("numCol"),
        "table_column_count_invalid",
        minimum=1,
        maximum=_MAX_TABLE_COLUMNS,
    )
    if row_count * column_count > _MAX_TABLE_CELLS:
        _fail("table_grid_limit_exceeded")
    table_bbox = _page_bbox_from_provider_quadrilateral(
        layout.get("pos"),
        page_bbox=binding["page_bbox"],
        image_width_px=binding["page_image_width_px"],
        image_height_px=binding["page_image_height_px"],
    )
    cells_payload = layout.get("cells")
    if not isinstance(cells_payload, list) or not cells_payload:
        _fail("table_cells_invalid")
    if len(cells_payload) > _MAX_TABLE_CELLS:
        _fail("table_cell_limit_exceeded")

    cells: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    anchors: set[tuple[int, int]] = set()
    for cell_payload in cells_payload:
        cell = _parse_cell(
            cell_payload,
            row_count=row_count,
            column_count=column_count,
        )
        anchor = (cell["row"], cell["column"])
        if anchor in anchors:
            _fail("table_cell_anchor_duplicate")
        anchors.add(anchor)
        coverage = {
            (row, column)
            for row in range(cell["row"], cell["row"] + cell["row_span"])
            for column in range(
                cell["column"],
                cell["column"] + cell["column_span"],
            )
        }
        if occupied.intersection(coverage):
            _fail("table_cell_coverage_overlap")
        occupied.update(coverage)
        cells.append(cell)

    expected = {
        (row, column) for row in range(row_count) for column in range(column_count)
    }
    if occupied != expected:
        _fail("table_cell_coverage_gap")
    cells.sort(key=lambda cell: (cell["row"], cell["column"]))
    return {
        "provider_unique_id": provider_unique_id,
        "provider_page_number": provider_page_number,
        "provider_index": provider_index,
        "table_bbox": table_bbox,
        "row_count": row_count,
        "column_count": column_count,
        "cells": cells,
    }


def _parse_cell(
    cell_payload: object,
    *,
    row_count: int,
    column_count: int,
) -> dict[str, Any]:
    cell = _require_mapping(cell_payload, "table_cell_not_object")
    column_start = _require_integer(
        cell.get("xsc"),
        "table_cell_column_start_invalid",
        minimum=0,
    )
    column_end = _require_integer(
        cell.get("xec"),
        "table_cell_column_end_invalid",
        minimum=column_start,
    )
    row_start = _require_integer(
        cell.get("ysc"),
        "table_cell_row_start_invalid",
        minimum=0,
    )
    row_end = _require_integer(
        cell.get("yec"),
        "table_cell_row_end_invalid",
        minimum=row_start,
    )
    if column_end >= column_count or row_end >= row_count:
        _fail("table_cell_span_out_of_bounds")
    nested_layouts = cell.get("layouts")
    if not isinstance(nested_layouts, list):
        _fail("table_cell_layouts_not_array")
    text_parts: list[str] = []
    for nested_layout in nested_layouts:
        nested = _require_mapping(
            nested_layout,
            "table_cell_layout_not_object",
        )
        nested_text = nested.get("text")
        if not isinstance(nested_text, str):
            _fail("table_cell_layout_text_invalid")
        # Deliberately do not deduplicate nested layouts.  Repeated provider
        # entries are observable review evidence and concatenate in input order.
        text_parts.append(nested_text)
    return {
        "row": row_start,
        "column": column_start,
        "row_span": row_end - row_start + 1,
        "column_span": column_end - column_start + 1,
        "raw_text": "".join(text_parts),
    }


def _page_bbox_from_provider_quadrilateral(
    value: object,
    *,
    page_bbox: list[float],
    image_width_px: int,
    image_height_px: int,
) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        _fail("table_position_not_quadrilateral")
    points: list[tuple[float, float]] = []
    for point in value:
        point_object = _require_mapping(point, "table_position_point_invalid")
        if set(point_object) != {"x", "y"}:
            _fail("table_position_point_invalid")
        x = _require_finite_number(
            point_object.get("x"),
            "table_position_coordinate_invalid",
        )
        y = _require_finite_number(
            point_object.get("y"),
            "table_position_coordinate_invalid",
        )
        if not 0.0 <= x <= image_width_px or not 0.0 <= y <= image_height_px:
            _fail("table_position_outside_image")
        points.append((x, y))
    if len(set(points)) != 4 or abs(_signed_double_area(points)) <= 1e-9:
        _fail("table_position_degenerate")
    if _opposite_edges_intersect(points):
        _fail("table_position_self_intersecting")

    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    if x_max <= x_min or y_max <= y_min:
        _fail("table_position_degenerate")
    page_x0, page_y0, page_x1, page_y1 = page_bbox
    page_width = page_x1 - page_x0
    page_height = page_y1 - page_y0
    return [
        page_x0 + x_min / image_width_px * page_width,
        page_y0 + y_min / image_height_px * page_height,
        page_x0 + x_max / image_width_px * page_width,
        page_y0 + y_max / image_height_px * page_height,
    ]


def _validate_occurrence_for_hash(value: object) -> dict[str, Any]:
    occurrence = _require_mapping(value, "table_occurrence_not_object")
    required_fields = {
        "provider_unique_id",
        "provider_page_number",
        "provider_index",
        "source_order",
        "table_bbox",
        "row_count",
        "column_count",
        "cells",
    }
    missing = required_fields.difference(occurrence)
    if missing:
        _fail("table_occurrence_identity_incomplete")
    return {field: _json_clone(occurrence[field]) for field in required_fields}


def _validate_captured_table(
    value: object,
    *,
    binding: Mapping[str, Any],
    expected_source_order: int,
) -> dict[str, Any]:
    table = _require_mapping(value, "capture_table_not_object")
    _require_exact_fields(
        table,
        _CAPTURE_TABLE_FIELDS,
        "capture_table_fields_invalid",
    )
    provider_unique_id = _require_nonblank_string(
        table.get("provider_unique_id"),
        "capture_table_unique_id_invalid",
        maximum_length=320,
    )
    provider_page_number = _require_integer(
        table.get("provider_page_number"),
        "capture_table_provider_page_number_invalid",
        minimum=0,
    )
    if provider_page_number != 0:
        _fail("capture_table_provider_page_number_invalid")
    provider_index = _require_integer(
        table.get("provider_index"),
        "capture_table_provider_index_invalid",
        minimum=0,
        maximum=_MAX_PROVIDER_LAYOUTS - 1,
    )
    source_order = _require_integer(
        table.get("source_order"),
        "capture_table_source_order_invalid",
        minimum=0,
        maximum=_MAX_PROVIDER_LAYOUTS - 1,
    )
    if source_order != expected_source_order:
        _fail("capture_table_source_order_mismatch")

    table_bbox = _require_bbox(table.get("table_bbox"), "capture_table_bbox_invalid")
    page_x0, page_y0, page_x1, page_y1 = binding["page_bbox"]
    table_x0, table_y0, table_x1, table_y1 = table_bbox
    if (
        table_x0 < page_x0
        or table_y0 < page_y0
        or table_x1 > page_x1
        or table_y1 > page_y1
    ):
        _fail("capture_table_bbox_outside_page")

    row_count = _require_integer(
        table.get("row_count"),
        "capture_table_row_count_invalid",
        minimum=1,
        maximum=_MAX_TABLE_ROWS,
    )
    column_count = _require_integer(
        table.get("column_count"),
        "capture_table_column_count_invalid",
        minimum=1,
        maximum=_MAX_TABLE_COLUMNS,
    )
    if row_count * column_count > _MAX_TABLE_CELLS:
        _fail("capture_table_grid_limit_exceeded")

    raw_cells = table.get("cells")
    if not isinstance(raw_cells, list) or not raw_cells:
        _fail("capture_table_cells_invalid")
    if len(raw_cells) > _MAX_TABLE_CELLS:
        _fail("capture_table_cell_limit_exceeded")
    cells: list[dict[str, Any]] = []
    occupied: set[tuple[int, int]] = set()
    anchors: set[tuple[int, int]] = set()
    for raw_cell in raw_cells:
        cell = _validate_captured_cell(
            raw_cell,
            row_count=row_count,
            column_count=column_count,
        )
        anchor = (cell["row"], cell["column"])
        if anchor in anchors:
            _fail("capture_table_cell_anchor_duplicate")
        anchors.add(anchor)
        coverage = {
            (row, column)
            for row in range(cell["row"], cell["row"] + cell["row_span"])
            for column in range(
                cell["column"],
                cell["column"] + cell["column_span"],
            )
        }
        if occupied.intersection(coverage):
            _fail("capture_table_cell_coverage_overlap")
        occupied.update(coverage)
        cells.append(cell)
    if cells != sorted(cells, key=lambda cell: (cell["row"], cell["column"])):
        _fail("capture_table_cells_not_canonical_order")
    expected_coverage = {
        (row, column) for row in range(row_count) for column in range(column_count)
    }
    if occupied != expected_coverage:
        _fail("capture_table_cell_coverage_gap")

    occurrence_sha256 = _require_sha256(
        table.get("occurrence_sha256"),
        "capture_table_occurrence_sha256_invalid",
    )
    if table.get("disposition") != "UNRESOLVED":
        _fail("capture_table_disposition_must_be_unresolved")
    validated = {
        "provider_unique_id": provider_unique_id,
        "provider_page_number": provider_page_number,
        "provider_index": provider_index,
        "table_bbox": table_bbox,
        "row_count": row_count,
        "column_count": column_count,
        "cells": cells,
        "source_order": source_order,
        "occurrence_sha256": occurrence_sha256,
        "disposition": "UNRESOLVED",
    }
    expected_occurrence_sha256 = _canonical_json_sha256(
        _table_occurrence_identity(binding=binding, table=validated)
    )
    if occurrence_sha256 != expected_occurrence_sha256:
        _fail("capture_table_occurrence_sha256_mismatch")
    return validated


def _validate_captured_cell(
    value: object,
    *,
    row_count: int,
    column_count: int,
) -> dict[str, Any]:
    cell = _require_mapping(value, "capture_table_cell_not_object")
    _require_exact_fields(
        cell,
        _CAPTURE_CELL_FIELDS,
        "capture_table_cell_fields_invalid",
    )
    row = _require_integer(
        cell.get("row"),
        "capture_table_cell_row_invalid",
        minimum=0,
    )
    column = _require_integer(
        cell.get("column"),
        "capture_table_cell_column_invalid",
        minimum=0,
    )
    row_span = _require_integer(
        cell.get("row_span"),
        "capture_table_cell_row_span_invalid",
        minimum=1,
    )
    column_span = _require_integer(
        cell.get("column_span"),
        "capture_table_cell_column_span_invalid",
        minimum=1,
    )
    if row + row_span > row_count or column + column_span > column_count:
        _fail("capture_table_cell_span_out_of_bounds")
    raw_text = cell.get("raw_text")
    if not isinstance(raw_text, str):
        _fail("capture_table_cell_raw_text_invalid")
    return {
        "row": row,
        "column": column,
        "row_span": row_span,
        "column_span": column_span,
        "raw_text": raw_text,
    }


def _table_occurrence_identity(
    *,
    binding: Mapping[str, Any],
    table: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **_json_clone(binding),
        "provider_unique_id": table["provider_unique_id"],
        "provider_page_number": table["provider_page_number"],
        "provider_index": table["provider_index"],
        "source_order": table["source_order"],
        "table_bbox": _json_clone(table["table_bbox"]),
        "row_count": table["row_count"],
        "column_count": table["column_count"],
        "cells": _json_clone(table["cells"]),
    }


def _canonical_table_sort_key(table: Mapping[str, Any]) -> tuple[Any, ...]:
    x0, y0, x1, y1 = table["table_bbox"]
    return (
        y0,
        x0,
        y1,
        x1,
        table["provider_index"],
        table["provider_unique_id"],
    )


def _require_bbox(value: object, code: str) -> list[float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 4
    ):
        _fail(code)
    bbox = [_require_finite_number(item, code) for item in value]
    if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
        _fail(code)
    return bbox


def _require_integer(
    value: object,
    code: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(code)
    if value < minimum or (maximum is not None and value > maximum):
        _fail(code)
    return value


def _require_finite_number(value: object, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(code)
    normalized = float(value)
    if not math.isfinite(normalized):
        _fail(code)
    if normalized == 0.0:
        normalized = 0.0
    return normalized


def _require_temperature(value: object) -> float:
    normalized = _require_finite_number(value, "provider_temperature_invalid")
    if not 0.0 <= normalized < 2.0:
        _fail("provider_temperature_invalid")
    return normalized


def _require_sha256(value: object, code: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        _fail(code)
    return value


def _require_model(value: object) -> str:
    if not isinstance(value, str) or _MODEL_PATTERN.fullmatch(value) is None:
        _fail("provider_model_invalid")
    return value


def _require_safe_version_part(value: object, code: str) -> str:
    if (
        not isinstance(value, str)
        or _SAFE_VERSION_PART_PATTERN.fullmatch(value) is None
    ):
        _fail(code)
    return value


def _require_nonblank_string(
    value: object,
    code: str,
    *,
    maximum_length: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum_length
    ):
        _fail(code)
    return value


def _require_mapping(value: object, code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(code)
    return value


def _require_exact_fields(
    value: Mapping[str, Any],
    expected_fields: frozenset[str],
    code: str,
) -> None:
    if set(value) != expected_fields:
        _fail(code)


def _signed_double_area(points: list[tuple[float, float]]) -> float:
    return sum(
        x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1])
    )


def _opposite_edges_intersect(points: list[tuple[float, float]]) -> bool:
    return _segments_properly_intersect(points[0], points[1], points[2], points[3]) or (
        _segments_properly_intersect(points[1], points[2], points[3], points[0])
    )


def _segments_properly_intersect(
    a: tuple[float, float],
    b: tuple[float, float],
    c: tuple[float, float],
    d: tuple[float, float],
) -> bool:
    def orientation(
        p: tuple[float, float],
        q: tuple[float, float],
        r: tuple[float, float],
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    first = orientation(a, b, c)
    second = orientation(a, b, d)
    third = orientation(c, d, a)
    fourth = orientation(c, d, b)
    return first * second < 0.0 and third * fourth < 0.0


def _canonical_json_bytes(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PdfBodyReviewTableCaptureError("payload_not_canonical_json") from exc
    return rendered.encode("utf-8")


def _canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _json_clone(value: Any) -> Any:
    return json.loads(_canonical_json_bytes(value))


def _fail(code: str) -> None:
    raise PdfBodyReviewTableCaptureError(code)


capture_bailian_document_parsing_tables = convert_bailian_document_parsing_tables


__all__ = [
    "BAILIAN_DOCUMENT_CAPTURE_IMPLEMENTATION_VERSION",
    "BAILIAN_DOCUMENT_PARSING_PROVIDER_ID",
    "BAILIAN_DOCUMENT_PARSING_TASK",
    "BAILIAN_DOCUMENT_TABLE_PARSER_VERSION",
    "TABLE_CAPTURE_SCHEMA_VERSION",
    "PdfBodyReviewTableCaptureError",
    "build_bailian_document_parsing_provider_version",
    "canonical_review_table_occurrence_sha256",
    "capture_bailian_document_parsing_tables",
    "convert_bailian_document_parsing_tables",
    "validate_bailian_document_parsing_table_capture",
]
