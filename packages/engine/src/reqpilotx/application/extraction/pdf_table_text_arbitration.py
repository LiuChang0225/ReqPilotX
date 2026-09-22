from __future__ import annotations

import re
from dataclasses import dataclass

from reqpilotx.application.extraction.structure_boundary_rules import (
    ALPHABETIC_LIST_PREFIX,
    EXPLICIT_LIST_PREFIX,
    EXPLICIT_SECTION_PREFIX,
    IDEOGRAPHIC_NUMBERED_PREFIX,
    ROOT_HEADING_MAX_NORMALIZED_CHARACTERS,
    ROOT_MEASUREMENT_PREFIX,
    body_appendix_heading,
    explicit_section_label_from_text,
)
from reqpilotx.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)


PDF_TABLE_TEXT_MIN_COVERAGE = 0.98
_PDF_TABLE_TEXT_CELL_BBOX_TOLERANCE_POINTS = 6.0
_EMBEDDED_SINGLE_DOT_ROOT_PREFIX = re.compile(
    r"^[ \t]*(?P<label>[1-9][0-9]{0,2})[.．]"
    r"(?=$|[ \t\r\n]|[\u3400-\u9fff])",
    re.MULTILINE,
)
NATIVE_NUMBER_REFERENCE_TRAILER = re.compile(
    r"(?:工作模式|运行模式|操作模式|"
    r"operating\s+mode|working\s+mode|operation\s+mode|"
    r"章节|条款|第|图|表|"
    r"section|clause|chapter|article|figure|table)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PdfTableTextSelection:
    source_node: SourceNode
    row_index: int
    replaced_cell_ids: tuple[str, ...]


def folded_pdf_table_text_nodes(
    snapshot: SourceIRSnapshot,
) -> tuple[
    dict[str, tuple[SourceNode, ...]],
    dict[tuple[str, int], tuple[PdfTableTextSelection, ...]],
    frozenset[str],
    frozenset[str],
]:
    """Arbitrate immutable PDF glyph text against native table topology.

    Resolver and inspector both consume this public evidence contract.  The
    function never mutates SourceIR and never decides whether a structure patch
    may be written.
    """

    tables_by_page: dict[int, list[SourceNode]] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            node.kind is SourceNodeKind.TABLE
            and locator.format is NativeDocumentFormat.PDF
            and locator.page_number is not None
            and locator.bbox is not None
            and node.table_payload is not None
        ):
            tables_by_page.setdefault(locator.page_number, []).append(node)
    document_flow_table_ids = document_flow_pdf_table_source_ids(
        snapshot,
        tables_by_page=tables_by_page,
    )

    table_level: dict[str, list[SourceNode]] = {}
    row_level: dict[tuple[str, int], list[PdfTableTextSelection]] = {}
    outline_source_ids: set[str] = set()
    ambiguous_source_ids: set[str] = set()
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            node.kind is not SourceNodeKind.PARAGRAPH
            or locator.format is not NativeDocumentFormat.PDF
            or locator.page_number is None
            or locator.bbox is None
            or node.parent_source_node_id is not None
            or not node.raw_text.strip()
        ):
            continue
        candidates = tuple(
            table_node
            for table_node in tables_by_page.get(locator.page_number, ())
            if bbox_coverage(locator.bbox, table_node.native_locator.bbox)
            >= PDF_TABLE_TEXT_MIN_COVERAGE
        )
        if not candidates:
            continue
        selection_candidates = tuple(
            (table_node, selection)
            for table_node in candidates
            if table_node.table_payload is not None
            if (
                selection := _pdf_table_text_selection(
                    source_node=node,
                    table_cells=table_node.table_payload.cells,
                )
            )
            is not None
        )
        most_specific_container = _most_specific_containing_pdf_table(candidates)
        selected = (
            next(
                (
                    candidate
                    for candidate in selection_candidates
                    if most_specific_container is not None
                    and candidate[0].source_node_id
                    == most_specific_container.source_node_id
                ),
                None,
            )
            if most_specific_container is not None
            else None
        )
        if selected is None and len(candidates) == 1:
            selected = _most_specific_pdf_table_selection(selection_candidates)
        if selected is not None:
            table_node, selection = selected
            table = table_node.table_payload
            if table is None:
                continue
            selection_is_full_width = _table_selection_is_full_width(
                selection,
                table_cells=table.cells,
                column_count=table.column_count,
            )
            if (
                table_node.source_node_id in document_flow_table_ids
                and selection_is_full_width
            ):
                # This physical table is a page-layout container.  Its
                # full-width cells carry the ordinary document reading flow;
                # keep those immutable paragraph facts in the Structure
                # stream.  Narrow/multi-column cells remain table evidence.
                if body_appendix_heading(node.raw_text) or (
                    _potential_table_outline_boundary(node.raw_text)
                ):
                    outline_source_ids.add(node.source_node_id)
                continue
            if body_appendix_heading(node.raw_text) or (
                _potential_table_outline_boundary(node.raw_text)
                and selection_is_full_width
            ):
                outline_source_ids.add(node.source_node_id)
                continue
            row_level.setdefault(
                (table_node.source_node_id, selection.row_index),
                [],
            ).append(selection)
            continue
        if most_specific_container is not None:
            if _full_width_table_root_outline_fact(
                node,
                table_node=most_specific_container,
            ):
                outline_source_ids.add(node.source_node_id)
                continue
            table_level.setdefault(
                most_specific_container.source_node_id,
                [],
            ).append(node)
            continue
        ambiguous_source_ids.add(node.source_node_id)

    return (
        {
            key: tuple(
                sorted(nodes, key=lambda item: (item.source_order, item.source_node_id))
            )
            for key, nodes in table_level.items()
        },
        {
            key: tuple(
                sorted(
                    selections,
                    key=lambda item: (
                        item.source_node.source_order,
                        item.source_node.source_node_id,
                    ),
                )
            )
            for key, selections in row_level.items()
        },
        frozenset(outline_source_ids),
        frozenset(ambiguous_source_ids),
    )


def document_flow_pdf_table_source_ids(
    snapshot: SourceIRSnapshot,
    *,
    tables_by_page: dict[int, list[SourceNode]] | None = None,
) -> frozenset[str]:
    """Identify page-layout tables whose full-width cells carry body flow.

    The proof is topology plus a coherent printed outline.  Either three
    dotted section anchors share one root, or three outline anchors enclose at
    least one ordinary prose block.  Every fact must bind to a full-width cell
    of the same smallest native table.  This deliberately avoids document
    names, page numbers and vocabulary.  Ordinary data tables keep their
    overlaid glyph text folded into table rows.
    """

    if tables_by_page is None:
        tables_by_page = {}
        for node in snapshot.nodes:
            locator = node.native_locator
            if (
                node.kind is SourceNodeKind.TABLE
                and locator.format is NativeDocumentFormat.PDF
                and locator.page_number is not None
                and locator.bbox is not None
                and node.table_payload is not None
            ):
                tables_by_page.setdefault(locator.page_number, []).append(node)

    labels_by_table_id: dict[str, list[str]] = {}
    flow_facts_by_table_id: dict[str, list[tuple[int, bool]]] = {}
    for node in snapshot.nodes:
        locator = node.native_locator
        if (
            node.kind is not SourceNodeKind.PARAGRAPH
            or locator.format is not NativeDocumentFormat.PDF
            or locator.page_number is None
            or locator.bbox is None
            or node.parent_source_node_id is not None
            or not node.raw_text.strip()
        ):
            continue
        candidates = tuple(
            table_node
            for table_node in tables_by_page.get(locator.page_number, ())
            if bbox_coverage(locator.bbox, table_node.native_locator.bbox)
            >= PDF_TABLE_TEXT_MIN_COVERAGE
        )
        table_node = _most_specific_containing_pdf_table(candidates)
        if table_node is None or table_node.table_payload is None:
            continue
        selection = _pdf_table_text_selection(
            source_node=node,
            table_cells=table_node.table_payload.cells,
        )
        if selection is None or not _table_selection_is_full_width(
            selection,
            table_cells=table_node.table_payload.cells,
            column_count=table_node.table_payload.column_count,
        ):
            continue
        label = explicit_section_label_from_text(
            node.raw_text,
            native_heading=False,
        )
        if label is not None and "." in label:
            labels_by_table_id.setdefault(table_node.source_node_id, []).append(label)
        flow_facts_by_table_id.setdefault(table_node.source_node_id, []).append(
            (node.source_order, _potential_table_outline_boundary(node.raw_text))
        )

    resolved: set[str] = set()
    for table_id, facts in flow_facts_by_table_id.items():
        labels = labels_by_table_id.get(table_id, ())
        labels_by_root: dict[str, set[str]] = {}
        for label in labels:
            root = label.split(".", 1)[0]
            labels_by_root.setdefault(root, set()).add(label)
        if any(len(root_labels) >= 3 for root_labels in labels_by_root.values()):
            resolved.add(table_id)
            continue
        ordered_facts = sorted(facts)
        anchor_orders = tuple(order for order, is_anchor in ordered_facts if is_anchor)
        body_orders = tuple(order for order, is_anchor in ordered_facts if not is_anchor)
        if (
            len(set(anchor_orders)) >= 3
            and body_orders
            and any(anchor_orders[0] < order < anchor_orders[-1] for order in body_orders)
        ):
            resolved.add(table_id)
    return frozenset(resolved)


def credible_single_dot_root_matches(
    raw_text: str,
) -> tuple[re.Match[str], ...]:
    matches: list[re.Match[str]] = []
    for match in _EMBEDDED_SINGLE_DOT_ROOT_PREFIX.finditer(raw_text):
        line_end = raw_text.find("\n", match.end())
        if line_end < 0:
            line_end = len(raw_text)
        remainder = " ".join(raw_text[match.end() : line_end].split())
        if (
            not remainder
            or len(remainder) > ROOT_HEADING_MAX_NORMALIZED_CHARACTERS
            or remainder[0].isdigit()
            or remainder[0] in ".．+-±≤≥<>"
            or re.search(r"[。！？!?;；]\s*$", remainder) is not None
            or ROOT_MEASUREMENT_PREFIX.match(remainder) is not None
            or embedded_section_marker_is_wrapped_reference(raw_text, match)
        ):
            continue
        matches.append(match)
    return tuple(matches)


def embedded_section_marker_is_wrapped_reference(
    raw_text: str,
    match: re.Match[str],
) -> bool:
    line_start = raw_text.rfind("\n", 0, match.start("label")) + 1
    previous_lines = tuple(
        line.rstrip() for line in raw_text[:line_start].splitlines() if line.strip()
    )
    return bool(
        previous_lines
        and NATIVE_NUMBER_REFERENCE_TRAILER.search(previous_lines[-1]) is not None
    )


def cells_covering_row(
    cells: tuple[TableCellIR, ...],
    row_index: int,
) -> tuple[TableCellIR, ...]:
    return tuple(
        sorted(
            (
                cell
                for cell in cells
                if cell.row <= row_index < cell.row + cell.row_span
            ),
            key=lambda cell: (cell.column, cell.row, cell.cell_id),
        )
    )


def bbox_coverage(
    inner: tuple[float, float, float, float],
    outer: tuple[float, float, float, float] | None,
) -> float:
    if outer is None:
        return 0.0
    inner_width = inner[2] - inner[0]
    inner_height = inner[3] - inner[1]
    if inner_width <= 0.0 or inner_height <= 0.0:
        return 0.0
    overlap_width = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    overlap_height = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    return (overlap_width * overlap_height) / (inner_width * inner_height)


def bbox_area(
    bbox: tuple[float, float, float, float] | None,
) -> float:
    if bbox is None:
        return float("inf")
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def bbox_overlap_area(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    overlap_width = max(
        0.0,
        min(first[2], second[2]) - max(first[0], second[0]),
    )
    overlap_height = max(
        0.0,
        min(first[3], second[3]) - max(first[1], second[1]),
    )
    return overlap_width * overlap_height


def expanded_bbox(
    bbox: tuple[float, float, float, float],
    *,
    margin: float,
) -> tuple[float, float, float, float]:
    return (
        bbox[0] - margin,
        bbox[1] - margin,
        bbox[2] + margin,
        bbox[3] + margin,
    )


def bbox_union_coverage(
    inner: tuple[float, float, float, float],
    outers: tuple[tuple[float, float, float, float], ...],
) -> float:
    """Return exact coverage of one bbox by a union of axis-aligned bboxes."""

    inner_width = inner[2] - inner[0]
    inner_height = inner[3] - inner[1]
    if inner_width <= 0.0 or inner_height <= 0.0:
        return 0.0
    clipped = tuple(
        (
            max(inner[0], outer[0]),
            max(inner[1], outer[1]),
            min(inner[2], outer[2]),
            min(inner[3], outer[3]),
        )
        for outer in outers
        if bbox_overlap_area(inner, outer) > 0.0
    )
    if not clipped:
        return 0.0
    x_boundaries = sorted(
        {coordinate for bbox in clipped for coordinate in (bbox[0], bbox[2])}
    )
    covered_area = 0.0
    for x0, x1 in zip(x_boundaries, x_boundaries[1:]):
        if x1 <= x0:
            continue
        intervals = sorted(
            (bbox[1], bbox[3]) for bbox in clipped if bbox[0] < x1 and bbox[2] > x0
        )
        covered_height = 0.0
        current_start: float | None = None
        current_end: float | None = None
        for y0, y1 in intervals:
            if y1 <= y0:
                continue
            if current_start is None or current_end is None:
                current_start, current_end = y0, y1
            elif y0 > current_end:
                covered_height += current_end - current_start
                current_start, current_end = y0, y1
            else:
                current_end = max(current_end, y1)
        if current_start is not None and current_end is not None:
            covered_height += current_end - current_start
        covered_area += (x1 - x0) * covered_height
    return covered_area / (inner_width * inner_height)


def point_in_bbox(
    x: float,
    y: float,
    bbox: tuple[float, float, float, float],
) -> bool:
    return bbox[0] <= x <= bbox[2] and bbox[1] <= y <= bbox[3]


def _most_specific_pdf_table_selection(
    candidates: tuple[tuple[SourceNode, PdfTableTextSelection], ...],
) -> tuple[SourceNode, PdfTableTextSelection] | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda item: (
            bbox_area(item[0].native_locator.bbox),
            item[0].source_order,
            item[0].source_node_id,
        ),
    )
    selected = ordered[0]
    selected_bbox = selected[0].native_locator.bbox
    if selected_bbox is None:
        return None
    if all(
        candidate.native_locator.bbox is not None
        and bbox_coverage(selected_bbox, candidate.native_locator.bbox)
        >= PDF_TABLE_TEXT_MIN_COVERAGE
        and bbox_area(selected_bbox) < bbox_area(candidate.native_locator.bbox)
        for candidate, _ in ordered[1:]
    ):
        return selected
    return None


def _most_specific_containing_pdf_table(
    candidates: tuple[SourceNode, ...],
) -> SourceNode | None:
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        return None
    ordered = sorted(
        candidates,
        key=lambda item: (
            bbox_area(item.native_locator.bbox),
            item.source_order,
            item.source_node_id,
        ),
    )
    selected = ordered[0]
    selected_bbox = selected.native_locator.bbox
    if selected_bbox is None:
        return None
    if all(
        candidate.native_locator.bbox is not None
        and bbox_coverage(selected_bbox, candidate.native_locator.bbox)
        >= PDF_TABLE_TEXT_MIN_COVERAGE
        and bbox_area(selected_bbox) < bbox_area(candidate.native_locator.bbox)
        for candidate in ordered[1:]
    ):
        return selected
    return None


def _table_selection_is_full_width(
    selection: PdfTableTextSelection,
    *,
    table_cells: tuple[TableCellIR, ...],
    column_count: int,
) -> bool:
    cells_by_id = {cell.cell_id: cell for cell in table_cells}
    selected_cells = tuple(
        cells_by_id[cell_id]
        for cell_id in selection.replaced_cell_ids
        if cell_id in cells_by_id
    )
    return any(
        cell.column == 0 and cell.column_span == column_count for cell in selected_cells
    )


def _potential_table_outline_boundary(raw_text: str) -> bool:
    if explicit_section_label_from_text(raw_text, native_heading=False) is not None:
        return True
    if EXPLICIT_LIST_PREFIX.match(raw_text) is not None:
        return True
    if ALPHABETIC_LIST_PREFIX.match(raw_text) is not None:
        return True
    if IDEOGRAPHIC_NUMBERED_PREFIX.match(raw_text) is not None:
        return True
    if body_appendix_heading(raw_text):
        return True
    match = EXPLICIT_SECTION_PREFIX.match(raw_text)
    return bool(
        match is not None
        and match.group("trailing_dot") is not None
        and raw_text[match.end() :].strip()
    )


def _full_width_table_root_outline_fact(
    source_node: SourceNode,
    *,
    table_node: SourceNode,
) -> bool:
    table = table_node.table_payload
    if table is None:
        return False
    matches = credible_single_dot_root_matches(source_node.raw_text)
    labels = tuple(int(match.group("label")) for match in matches)
    if len(labels) < 2 or any(
        current != previous + 1 for previous, current in zip(labels, labels[1:])
    ):
        return False
    full_width_text = tuple(
        " ".join(cell.raw_text.split())
        for cell in table.cells
        if cell.column == 0
        and cell.column_span == table.column_count
        and cell.raw_text.strip()
    )
    if not full_width_text:
        return False
    for match in matches:
        line_end = source_node.raw_text.find("\n", match.end())
        if line_end < 0:
            line_end = len(source_node.raw_text)
        marker_line = " ".join(
            source_node.raw_text[match.start("label") : line_end].split()
        )
        if not marker_line or not any(
            marker_line in cell_text for cell_text in full_width_text
        ):
            return False
    return True


def _pdf_table_text_selection(
    *,
    source_node: SourceNode,
    table_cells: tuple[TableCellIR, ...],
) -> PdfTableTextSelection | None:
    bbox = source_node.native_locator.bbox
    if bbox is None:
        return None
    center_x = (bbox[0] + bbox[2]) / 2.0
    center_y = (bbox[1] + bbox[3]) / 2.0
    cells_at_center = tuple(
        cell
        for cell in table_cells
        if cell.native_locator.bbox is not None
        and point_in_bbox(center_x, center_y, cell.native_locator.bbox)
    )
    if len(cells_at_center) != 1:
        return None
    row_index = cells_at_center[0].row
    covering_cells = cells_covering_row(table_cells, row_index)
    overlapped_cells = tuple(
        cell
        for cell in covering_cells
        if cell.native_locator.bbox is not None
        and bbox_overlap_area(bbox, cell.native_locator.bbox) > 0.0
    )
    if (
        not overlapped_cells
        or bbox_union_coverage(
            bbox,
            tuple(
                expanded_bbox(
                    cell.native_locator.bbox,
                    margin=_PDF_TABLE_TEXT_CELL_BBOX_TOLERANCE_POINTS,
                )
                for cell in overlapped_cells
                if cell.native_locator.bbox is not None
            ),
        )
        < PDF_TABLE_TEXT_MIN_COVERAGE
    ):
        return None
    return PdfTableTextSelection(
        source_node=source_node,
        row_index=row_index,
        replaced_cell_ids=tuple(cell.cell_id for cell in overlapped_cells),
    )


__all__ = [
    "NATIVE_NUMBER_REFERENCE_TRAILER",
    "PDF_TABLE_TEXT_MIN_COVERAGE",
    "PdfTableTextSelection",
    "bbox_area",
    "bbox_coverage",
    "bbox_overlap_area",
    "bbox_union_coverage",
    "cells_covering_row",
    "credible_single_dot_root_matches",
    "document_flow_pdf_table_source_ids",
    "embedded_section_marker_is_wrapped_reference",
    "expanded_bbox",
    "folded_pdf_table_text_nodes",
    "point_in_bbox",
]
