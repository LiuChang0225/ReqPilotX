"""Deterministic vector-grid recovery for one visually bounded PDF table."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Iterable, Mapping

import fitz

from reqpilotx_server.application.agent_workflow.source_requirement_baseline import (
    canonical_json_sha256,
)


class PdfVisualTableRecoveryError(ValueError):
    """Raised when a visual table cannot be recovered without guessing."""


def recover_visual_subcell_table_fragments(
    source_path: Path,
    *,
    expected_source_sha256: str,
    logical_table_scope_id: str,
    fragment_specs: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    source = source_path.expanduser().resolve(strict=True)
    if (
        source.is_symlink()
        or not source.is_file()
        or source.suffix.lower() != ".pdf"
        or _sha256_file(source) != expected_source_sha256
    ):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_SOURCE_IDENTITY_INVALID")
    specs = tuple(fragment_specs)
    if not logical_table_scope_id.strip() or not specs:
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_SCOPE_INVALID")
    document = fitz.open(source)
    try:
        fragments = [
            _recover_fragment(
                document,
                expected_source_sha256=expected_source_sha256,
                logical_table_scope_id=logical_table_scope_id,
                spec=spec,
            )
            for spec in specs
        ]
    finally:
        document.close()
    fragments.sort(key=lambda item: (item["page"], item["row_range"][0]))
    receipt_payload = {
        "schema_version": "pdf-visual-table-recovery-receipt/v1",
        "status": "RECOVERED",
        "method": "NATIVE_WORDS_WITH_VECTOR_GRID",
        "source_sha256": expected_source_sha256,
        "logical_table_scope_id": logical_table_scope_id,
        "pages": sorted({int(item["page"]) for item in fragments}),
        "fragment_sha256s": [
            canonical_json_sha256(item) for item in fragments
        ],
    }
    return fragments, {
        **receipt_payload,
        "receipt_sha256": canonical_json_sha256(receipt_payload),
    }


def _recover_fragment(
    document: fitz.Document,
    *,
    expected_source_sha256: str,
    logical_table_scope_id: str,
    spec: Mapping[str, Any],
) -> dict[str, Any]:
    page_number = int(spec.get("page") or 0)
    table_token = str(spec.get("table_token") or "")
    visual_bbox = _bbox(spec.get("visual_bbox"))
    if (
        page_number < 1
        or page_number > document.page_count
        or not table_token
    ):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_FRAGMENT_IDENTITY_INVALID")
    page = document.load_page(page_number - 1)
    vertical, horizontal = _grid_boundaries(page, visual_bbox=visual_bbox)
    row_count = len(horizontal) - 1
    column_count = len(vertical) - 1
    if not (1 <= row_count <= 256 and 2 <= column_count <= 64):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_GRID_SHAPE_INVALID")
    words = tuple(page.get_text("words", sort=True))
    cells: list[dict[str, Any]] = []
    assigned_word_indices: set[int] = set()
    visual_word_indices = {
        index
        for index, word in enumerate(words)
        if _point_inside(
            (
                (float(word[0]) + float(word[2])) / 2.0,
                (float(word[1]) + float(word[3])) / 2.0,
            ),
            visual_bbox,
        )
    }
    for row in range(row_count):
        for column in range(column_count):
            bounds = (
                vertical[column],
                horizontal[row],
                vertical[column + 1],
                horizontal[row + 1],
            )
            selected: list[tuple[float, float, str, int, int, int]] = []
            for index, word in enumerate(words):
                center = (
                    (float(word[0]) + float(word[2])) / 2.0,
                    (float(word[1]) + float(word[3])) / 2.0,
                )
                if not _point_inside(center, bounds):
                    continue
                selected.append(
                    (
                        float(word[1]),
                        float(word[0]),
                        str(word[4]),
                        int(word[5]),
                        int(word[6]),
                        index,
                    )
                )
                assigned_word_indices.add(index)
            text = _cell_text(selected)
            cell_identity = canonical_json_sha256(
                {
                    "source_sha256": expected_source_sha256,
                    "scope_id": logical_table_scope_id,
                    "page": page_number,
                    "table_token": table_token,
                    "row": row,
                    "column": column,
                    "bbox": bounds,
                    "text": text,
                }
            )
            cells.append(
                {
                    "cell_id": f"visual-cell-{cell_identity[:32]}",
                    "row": row,
                    "column": column,
                    "row_span": 1,
                    "column_span": 1,
                    "text": text,
                    "bbox": [float(item) for item in bounds],
                }
            )
    if not visual_word_indices.issubset(assigned_word_indices):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_WORD_COVERAGE_INCOMPLETE")
    if any(
        not str(cell["text"]).strip()
        for cell in cells
        if int(cell["row"]) == 0
    ):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_HEADER_INCOMPLETE")
    fragment_identity = canonical_json_sha256(
        {
            "source_sha256": expected_source_sha256,
            "scope_id": logical_table_scope_id,
            "page": page_number,
            "table_token": table_token,
            "vertical": vertical,
            "horizontal": horizontal,
            "cells": cells,
        }
    )
    page_rect = page.rect
    return {
        "fragment_id": f"visual-table-{fragment_identity[:32]}",
        "table_token": table_token,
        "page": page_number,
        "row_range": [0, row_count],
        "row_count": row_count,
        "column_count": column_count,
        "bbox": [vertical[0], horizontal[0], vertical[-1], horizontal[-1]],
        "source_bounds": [
            float(page_rect.x0),
            float(page_rect.y0),
            float(page_rect.x1),
            float(page_rect.y1),
        ],
        "cells": cells,
    }


def _grid_boundaries(
    page: fitz.Page,
    *,
    visual_bbox: tuple[float, float, float, float],
) -> tuple[list[float], list[float]]:
    width = visual_bbox[2] - visual_bbox[0]
    height = visual_bbox[3] - visual_bbox[1]
    margin_x = min(24.0, max(12.0, width * 0.05))
    margin_y = min(64.0, max(24.0, height * 0.8))
    vertical_candidates: list[float] = []
    horizontal_candidates: list[float] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None:
            continue
        rect_width = float(rect.x1 - rect.x0)
        rect_height = float(rect.y1 - rect.y0)
        if (
            rect_width <= 1.6
            and rect_height >= max(12.0, height * 0.7)
            and visual_bbox[0] - margin_x
            <= (float(rect.x0) + float(rect.x1)) / 2.0
            <= visual_bbox[2] + margin_x
            and float(rect.y0) <= visual_bbox[1] + 15.0
            and float(rect.y1) >= visual_bbox[3] - 5.0
        ):
            vertical_candidates.append(
                (float(rect.x0) + float(rect.x1)) / 2.0
            )
        if (
            rect_height <= 1.6
            and rect_width >= max(20.0, width * 0.75)
            and visual_bbox[1] - margin_y
            <= (float(rect.y0) + float(rect.y1)) / 2.0
            <= visual_bbox[3] + margin_y
            and float(rect.x0) <= visual_bbox[0] + 20.0
            and float(rect.x1) >= visual_bbox[2] - 5.0
        ):
            horizontal_candidates.append(
                (float(rect.y0) + float(rect.y1)) / 2.0
            )
    vertical_values = _dedupe_coordinates(vertical_candidates)
    horizontal_values = _dedupe_coordinates(horizontal_candidates)
    left = max(
        (item for item in vertical_values if item <= visual_bbox[0] + 1.0),
        default=None,
    )
    right = min(
        (item for item in vertical_values if item >= visual_bbox[2] - 1.0),
        default=None,
    )
    top = max(
        (item for item in horizontal_values if item <= visual_bbox[1] + 2.0),
        default=None,
    )
    bottom = min(
        (item for item in horizontal_values if item >= visual_bbox[3] - 2.0),
        default=None,
    )
    if None in {left, right, top, bottom}:
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_OUTER_GRID_MISSING")
    assert left is not None and right is not None
    assert top is not None and bottom is not None
    vertical = [item for item in vertical_values if left <= item <= right]
    horizontal = [item for item in horizontal_values if top <= item <= bottom]
    if (
        len(vertical) < 3
        or len(horizontal) < 2
        or any(
            values[index] - values[index - 1] < 3.0
            for values in (vertical, horizontal)
            for index in range(1, len(values))
        )
    ):
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_GRID_INCOMPLETE")
    return vertical, horizontal


def _cell_text(
    words: list[tuple[float, float, str, int, int, int]],
) -> str:
    lines: dict[tuple[int, int], list[tuple[float, str]]] = {}
    line_order: dict[tuple[int, int], float] = {}
    for y, x, text, block, line, _index in words:
        key = (block, line)
        lines.setdefault(key, []).append((x, text))
        line_order[key] = min(line_order.get(key, y), y)
    ordered_lines = sorted(lines, key=lambda key: (line_order[key], key))
    return "\n".join(
        " ".join(text for _x, text in sorted(lines[key])).strip()
        for key in ordered_lines
        if any(text.strip() for _x, text in lines[key])
    )


def _dedupe_coordinates(values: Iterable[float]) -> list[float]:
    result: list[float] = []
    for value in sorted(float(item) for item in values):
        if result and abs(value - result[-1]) <= 1.5:
            result[-1] = (result[-1] + value) / 2.0
        else:
            result.append(value)
    return result


def _bbox(value: object) -> tuple[float, float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_BBOX_INVALID")
    result = tuple(float(item) for item in value)
    if result[2] <= result[0] or result[3] <= result[1]:
        raise PdfVisualTableRecoveryError("VISUAL_TABLE_BBOX_INVALID")
    return result  # type: ignore[return-value]


def _point_inside(
    point: tuple[float, float],
    bounds: tuple[float, float, float, float],
) -> bool:
    return (
        bounds[0] - 0.5 <= point[0] <= bounds[2] + 0.5
        and bounds[1] - 0.5 <= point[1] <= bounds[3] + 0.5
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "PdfVisualTableRecoveryError",
    "recover_visual_subcell_table_fragments",
]
