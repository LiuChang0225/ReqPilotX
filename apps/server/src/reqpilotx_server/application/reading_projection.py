"""Deterministic, requirement-free projections for single-document reading."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable

from reqpilotx_server.domain.extraction.source_ir import (
    NativeDocumentFormat,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
)


_MAP_NODE_LIMIT = 800
_PREVIEW_CHARACTERS = 180
_HEADING_PATTERN = re.compile(
    r"^(?:"
    r"(?:\d{1,3}(?:\.\d{1,3}){0,6})(?:[\s.、:：]|$)|"
    r"第[一二三四五六七八九十百千\d]+[章节篇部分][\s:：、]?|"
    r"(?:appendix|annex|attachment)\s+[A-Z0-9一二三四五六七八九十]+|"
    r"(?:附录|附件)[\sA-Z0-9一二三四五六七八九十]*"
    r")",
    re.IGNORECASE,
)
_APPENDIX_PATTERN = re.compile(r"^(?:附录|appendix\b|annex\b)", re.IGNORECASE)
_ATTACHMENT_PATTERN = re.compile(r"^(?:附件|attachment\b)", re.IGNORECASE)
_BODY_KINDS = {
    SourceNodeKind.PARAGRAPH,
    SourceNodeKind.LIST,
    SourceNodeKind.LIST_ITEM,
    SourceNodeKind.TABLE,
    SourceNodeKind.IMAGE,
    SourceNodeKind.FIGURE,
    SourceNodeKind.NOTE,
}
_SELECTABLE_KINDS = {
    SourceNodeKind.HEADING,
    SourceNodeKind.PARAGRAPH,
    SourceNodeKind.LIST,
    SourceNodeKind.LIST_ITEM,
    SourceNodeKind.TABLE,
    SourceNodeKind.IMAGE,
    SourceNodeKind.FIGURE,
    SourceNodeKind.NOTE,
    SourceNodeKind.TOC_ENTRY,
    SourceNodeKind.ATTACHMENT,
}


def build_reading_projection(
    source_ir: SourceIRSnapshot,
    *,
    native_structure_page_numbers: frozenset[int] | None = None,
) -> dict[str, Any]:
    """Build overview, reading map, and bounded selection metadata.

    This projection consumes only SourceIR facts.  It does not import or infer
    StructureTree, semantic classes, requirements, compliance, or priorities.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    page_count = _page_count(source_ir)
    selectable_nodes = tuple(
        _selectable_metadata(node)
        for node in source_ir.nodes
        if node.kind in _SELECTABLE_KINDS
        and (node.raw_text or node.table_payload is not None or node.asset_ref)
    )
    map_payload, candidates = _build_structure_map(
        source_ir,
        page_count=page_count,
        native_structure_page_numbers=native_structure_page_numbers,
    )
    risks = _risk_regions(source_ir)
    coverage = _coverage_payload(
        source_ir,
        page_count=page_count,
        native_structure_page_numbers=native_structure_page_numbers,
    )
    body_pages = sorted(
        {
            page
            for node in source_ir.nodes
            if node.kind in _BODY_KINDS
            and (page := _node_page(node)) is not None
            and (
                native_structure_page_numbers is None
                or page in native_structure_page_numbers
            )
        }
    )
    toc_candidates = [
        _overview_candidate(item)
        for item in candidates
        if item["kind"] == "TOC_ENTRY"
    ]
    section_candidates = [
        _overview_candidate(item)
        for item in candidates
        if item["kind"] == "SECTION"
    ]
    appendix_candidates = [
        _range_from_candidate(item, reason="APPENDIX_TITLE_CANDIDATE")
        for item in candidates
        if item["kind"] == "APPENDIX"
    ]
    attachment_candidates = [
        _range_from_candidate(item, reason="ATTACHMENT_TITLE_OR_NODE")
        for item in candidates
        if item["kind"] == "ATTACHMENT"
    ]
    document_format = _document_format(source_ir)
    overview = {
        "schema_version": "document-reading-overview/v1",
        "document_type_candidate": _document_type_candidate(
            source_ir,
            document_format=document_format,
        ),
        "document_format": document_format,
        "page_count": page_count,
        "node_count": len(source_ir.nodes),
        "table_count": sum(
            node.kind is SourceNodeKind.TABLE for node in source_ir.nodes
        ),
        "image_count": sum(
            node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            for node in source_ir.nodes
        ),
        "heading_count": len(section_candidates),
        "toc_candidates": toc_candidates,
        "section_candidates": section_candidates,
        "body_candidate_ranges": _contiguous_page_ranges(
            body_pages,
            reason="BODY_CONTENT_OBSERVED",
        ),
        "appendix_candidate_ranges": appendix_candidates,
        "attachment_suspect_ranges": attachment_candidates,
        "risk_regions": risks,
        "coverage": coverage,
    }
    return {
        "overview": overview,
        "structure_map": map_payload,
        "selectable_nodes": list(selectable_nodes),
    }


def build_reading_source_page(
    source_ir: SourceIRSnapshot,
    *,
    page_number: int,
) -> dict[str, Any]:
    """Return exact, current-page text needed for source-bound selection."""

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(page_number, int) or isinstance(page_number, bool) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    page_count = _page_count(source_ir)
    if page_count and page_number > page_count:
        raise ValueError("page_number exceeds the captured document")
    nodes = [
        {
            **_selectable_metadata(node),
            "text": node.raw_text,
        }
        for node in source_ir.nodes
        if node.kind in _SELECTABLE_KINDS
        and _node_page(node) == page_number
        and bool(node.raw_text)
    ]
    return {
        "schema_version": "reading-source-page/v1",
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "page_number": page_number,
        "page_count": page_count,
        "nodes": nodes,
    }


def source_slice_for_ref(
    source_ir: SourceIRSnapshot,
    *,
    source_node_id: str,
    start_offset: int,
    end_offset: int,
) -> str:
    """Rebuild an exact non-empty source slice; never trust client text."""

    node = next(
        (item for item in source_ir.nodes if item.source_node_id == source_node_id),
        None,
    )
    if node is None or node.kind not in _SELECTABLE_KINDS or not node.raw_text:
        raise ValueError("source reference does not identify selectable current text")
    if (
        not isinstance(start_offset, int)
        or isinstance(start_offset, bool)
        or not isinstance(end_offset, int)
        or isinstance(end_offset, bool)
        or start_offset < 0
        or end_offset <= start_offset
        or end_offset > len(node.raw_text)
    ):
        raise ValueError("source reference offsets are outside the current source node")
    selected = node.raw_text[start_offset:end_offset]
    if not selected.strip():
        raise ValueError("source reference resolves only to whitespace")
    return selected


def _build_structure_map(
    source_ir: SourceIRSnapshot,
    *,
    page_count: int,
    native_structure_page_numbers: frozenset[int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for node in source_ir.nodes:
        page = _node_page(node)
        if (
            native_structure_page_numbers is not None
            and page not in native_structure_page_numbers
        ):
            continue
        candidate = _map_candidate(node)
        if candidate is not None:
            candidates.append(candidate)
    limited = _with_inferred_page_ends(
        candidates[:_MAP_NODE_LIMIT],
        page_count=page_count,
    )
    map_id_by_source = {
        item["source_node_id"]: _stable_id(
            "reading-map-node",
            source_ir.source_ir_id,
            item["source_node_id"],
        )
        for item in limited
    }
    stack: list[tuple[int, str]] = []
    nodes: list[dict[str, Any]] = []
    for item in limited:
        level = int(item["level_hint"])
        while stack and stack[-1][0] >= level:
            stack.pop()
        explicit_parent = map_id_by_source.get(str(item.get("parent_source_node_id") or ""))
        parent_map_node_id = explicit_parent or (stack[-1][1] if stack else None)
        map_node_id = map_id_by_source[item["source_node_id"]]
        nodes.append(
            {
                "map_node_id": map_node_id,
                "source_node_id": item["source_node_id"],
                "parent_map_node_id": parent_map_node_id,
                "kind": item["kind"],
                "title": item["title"],
                "level_hint": level,
                "source_order": item["source_order"],
                "page_start": item["page_start"],
                "page_end": item["page_end"],
                "confidence": item["confidence"],
                "reason_codes": item["reason_codes"],
                "evidence_status": "SOURCE_BOUND",
            }
        )
        stack.append((level, map_node_id))
    generation_status = "READY" if nodes else "PARTIAL"
    visual_structure_not_projected = bool(
        native_structure_page_numbers is not None
        and any(
            page not in native_structure_page_numbers
            for page in range(1, page_count + 1)
        )
    )
    coverage_status = (
        "PARTIAL"
        if len(candidates) > len(limited)
        or source_ir.status is not SourceIRStatus.COMPLETE
        or not nodes
        or visual_structure_not_projected
        else "COMPLETE"
    )
    reason_codes: list[str] = []
    if len(candidates) > len(limited):
        reason_codes.append("MAP_NODE_LIMIT_REACHED")
    if not nodes:
        reason_codes.append("NO_SOURCE_BOUND_STRUCTURE_CLUE")
    if visual_structure_not_projected:
        reason_codes.append("VISUAL_STRUCTURE_NOT_PROJECTED")
    return (
        {
            "schema_version": "reading-structure-map/v1",
            "map_id": _stable_id("reading-structure-map", source_ir.source_ir_id),
            "status": generation_status if coverage_status == "COMPLETE" else "PARTIAL",
            "generation_status": generation_status,
            "coverage_status": coverage_status,
            "page_count": page_count,
            "reason_codes": reason_codes,
            "nodes": nodes,
        },
        limited,
    )


def _map_candidate(node: SourceNode) -> dict[str, Any] | None:
    title = _normalized_title(node.raw_text)
    if not title:
        return None
    if node.kind is SourceNodeKind.TOC_ENTRY:
        kind, confidence, reason = "TOC_ENTRY", 0.98, "SOURCE_NODE_TOC_ENTRY"
    elif node.kind is SourceNodeKind.ATTACHMENT:
        kind, confidence, reason = "ATTACHMENT", 0.98, "SOURCE_NODE_ATTACHMENT"
    elif node.kind is SourceNodeKind.HEADING:
        kind, confidence, reason = "SECTION", 0.98, "SOURCE_NODE_HEADING"
    elif (
        node.kind in {SourceNodeKind.PARAGRAPH, SourceNodeKind.LIST_ITEM}
        and len(title) <= 180
        and "\n" not in node.raw_text.strip()
        and _HEADING_PATTERN.search(title)
    ):
        kind, confidence, reason = "SECTION", 0.68, "NUMBERED_SHORT_TEXT_CANDIDATE"
    else:
        return None
    if _APPENDIX_PATTERN.search(title):
        kind, reason = "APPENDIX", "APPENDIX_TITLE_CANDIDATE"
    elif _ATTACHMENT_PATTERN.search(title):
        kind, reason = "ATTACHMENT", "ATTACHMENT_TITLE_CANDIDATE"
    page = _node_page(node)
    if page is None:
        return None
    return {
        "source_node_id": node.source_node_id,
        "parent_source_node_id": node.parent_source_node_id,
        "kind": kind,
        "title": title,
        "level_hint": _level_hint(title, node.kind),
        "source_order": node.source_order,
        "page_start": page,
        "confidence": confidence,
        "reason_codes": [reason],
    }


def _risk_regions(source_ir: SourceIRSnapshot) -> list[dict[str, Any]]:
    risks: list[dict[str, Any]] = []
    for diagnostic in source_ir.diagnostics:
        page = (
            diagnostic.native_locator.page_number
            if diagnostic.native_locator is not None
            else None
        )
        risks.append(
            {
                "kind": "SOURCE_DIAGNOSTIC",
                "page_number": page,
                "source_node_id": diagnostic.source_node_id,
                "confidence": (
                    0.95
                    if diagnostic.severity is SourceDiagnosticSeverity.ERROR
                    else 0.8
                    if diagnostic.severity is SourceDiagnosticSeverity.WARNING
                    else 0.65
                ),
                "reason_code": diagnostic.code,
            }
        )
    images_by_page: dict[int, list[SourceNode]] = defaultdict(list)
    tables_by_page: dict[int, list[SourceNode]] = defaultdict(list)
    for node in source_ir.nodes:
        page = _node_page(node)
        if page is None:
            continue
        if node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}:
            images_by_page[page].append(node)
        elif node.kind is SourceNodeKind.TABLE:
            tables_by_page[page].append(node)
    for page, nodes in sorted(images_by_page.items()):
        if len(nodes) >= 3:
            risks.append(
                {
                    "kind": "DRAWING_DENSE_PAGE_CANDIDATE",
                    "page_number": page,
                    "source_node_id": nodes[0].source_node_id,
                    "confidence": 0.65,
                    "reason_code": "THREE_OR_MORE_SOURCE_IMAGE_NODES",
                }
            )
    for page in sorted(tables_by_page):
        next_page = page + 1
        if next_page not in tables_by_page:
            continue
        pair = _matching_table_pair(tables_by_page[page], tables_by_page[next_page])
        if pair is not None:
            risks.append(
                {
                    "kind": "CROSS_PAGE_TABLE_CANDIDATE",
                    "page_number": page,
                    "source_node_id": pair[0].source_node_id,
                    "confidence": 0.7,
                    "reason_code": "ADJACENT_TABLE_HEADER_AND_COLUMN_MATCH",
                }
            )
    return risks


def _matching_table_pair(
    left_nodes: Iterable[SourceNode],
    right_nodes: Iterable[SourceNode],
) -> tuple[SourceNode, SourceNode] | None:
    for left in left_nodes:
        for right in right_nodes:
            left_table = left.table_payload
            right_table = right.table_payload
            if left_table is None or right_table is None:
                continue
            if left_table.column_count != right_table.column_count:
                continue
            left_header = _table_header_signature(left)
            right_header = _table_header_signature(right)
            if left_header and left_header == right_header:
                return left, right
    return None


def _table_header_signature(node: SourceNode) -> tuple[str, ...]:
    table = node.table_payload
    if table is None or not table.cells:
        return ()
    header_rows = {region.row for region in table.header_regions}
    if not header_rows:
        header_rows = {0}
    values = [
        " ".join(cell.raw_text.split()).casefold()
        for cell in table.cells
        if cell.row in header_rows and cell.raw_text.strip()
    ]
    return tuple(values)


def _coverage_payload(
    source_ir: SourceIRSnapshot,
    *,
    page_count: int,
    native_structure_page_numbers: frozenset[int] | None = None,
) -> dict[str, Any]:
    if native_structure_page_numbers is None:
        native_pages = list(range(1, page_count + 1))
        withheld_pages: list[int] = []
    else:
        native_pages = [
            page
            for page in range(1, page_count + 1)
            if page in native_structure_page_numbers
        ]
        withheld_pages = [
            page
            for page in range(1, page_count + 1)
            if page not in native_structure_page_numbers
        ]
    evaluated = _contiguous_page_ranges(
        native_pages,
        reason="SOURCE_IR_SCANNED",
    )
    unevaluated = _contiguous_page_ranges(
        withheld_pages,
        reason="VISUAL_STRUCTURE_NOT_PROJECTED",
    )
    reason_codes: list[str] = []
    if source_ir.status is not SourceIRStatus.COMPLETE:
        reason_codes.append("SOURCE_IR_PARTIAL")
    if not page_count:
        reason_codes.append("PAGE_RANGE_NOT_AVAILABLE_FOR_FORMAT")
    if withheld_pages:
        reason_codes.append("VISUAL_STRUCTURE_NOT_PROJECTED")
    status = (
        "COMPLETE"
        if source_ir.status is SourceIRStatus.COMPLETE
        and page_count
        and not withheld_pages
        else "PARTIAL"
    )
    return {
        "status": status,
        "evaluated_ranges": evaluated,
        "unevaluated_ranges": unevaluated,
        "blocked_ranges": [],
        "reason_codes": reason_codes,
    }


def _selectable_metadata(node: SourceNode) -> dict[str, Any]:
    text = node.raw_text
    preview = " ".join(text.split())[:_PREVIEW_CHARACTERS]
    return {
        "source_node_id": node.source_node_id,
        "kind": node.kind.value.upper(),
        "source_order": node.source_order,
        "page_number": _node_page(node),
        "text_length": len(text),
        "preview": preview,
    }


def _overview_candidate(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item["title"],
        "page_start": item["page_start"],
        "page_end": item.get("page_end", item["page_start"]),
        "source_node_id": item["source_node_id"],
        "confidence": item["confidence"],
        "reason_codes": item["reason_codes"],
    }


def _range_from_candidate(item: dict[str, Any], *, reason: str) -> dict[str, Any]:
    return {
        "page_start": item["page_start"],
        "page_end": item.get("page_end", item["page_start"]),
        "source_node_ids": [item["source_node_id"]],
        "reason_codes": [reason],
    }


def _contiguous_page_ranges(pages: list[int], *, reason: str) -> list[dict[str, Any]]:
    if not pages:
        return []
    ranges: list[dict[str, Any]] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(
            {"page_start": start, "page_end": previous, "reason_codes": [reason]}
        )
        start = previous = page
    ranges.append({"page_start": start, "page_end": previous, "reason_codes": [reason]})
    return ranges


def _node_page(node: SourceNode) -> int | None:
    return node.native_locator.page_number


def _page_count(source_ir: SourceIRSnapshot) -> int:
    pages = [
        page
        for node in source_ir.nodes
        if (page := _node_page(node)) is not None
    ]
    pages.extend(
        asset.native_locator.page_number
        for asset in source_ir.assets
        if asset.native_locator is not None
        and asset.native_locator.page_number is not None
    )
    pages.extend(snapshot.page_number for snapshot in source_ir.visual_text_snapshots)
    return max(pages, default=0)


def _document_format(source_ir: SourceIRSnapshot) -> str:
    for node in source_ir.nodes:
        return node.native_locator.format.value
    for asset in source_ir.assets:
        if asset.native_locator is not None:
            return asset.native_locator.format.value
    if source_ir.source_conversion is not None:
        return source_ir.source_conversion.original_format
    return NativeDocumentFormat.PDF.value if _page_count(source_ir) else "unknown"


def _document_type_candidate(
    source_ir: SourceIRSnapshot,
    *,
    document_format: str,
) -> dict[str, Any]:
    sample = "\n".join(
        node.raw_text
        for node in source_ir.nodes[:80]
        if node.raw_text.strip()
    )[:8_000]
    patterns = (
        (
            r"(?:技术规范|技术要求|technical\s+specification|requirements?\s+specification)",
            "技术规范",
            "DOCUMENT_TYPE_TECHNICAL_SPECIFICATION_CUE",
        ),
        (
            r"(?:招标|投标|询价|request\s+for\s+(?:proposal|quotation)|invitation\s+to\s+tender)",
            "采购/招投标文件",
            "DOCUMENT_TYPE_PROCUREMENT_CUE",
        ),
        (
            r"(?:合同|协议|\bcontract\b|\bagreement\b)",
            "合同/协议",
            "DOCUMENT_TYPE_CONTRACT_CUE",
        ),
        (
            r"(?:操作手册|用户手册|说明书|user\s+manual|instruction\s+manual)",
            "手册/说明书",
            "DOCUMENT_TYPE_MANUAL_CUE",
        ),
        (
            r"(?:企业标准|行业标准|国家标准|international\s+standard|\bstandard\b)",
            "标准",
            "DOCUMENT_TYPE_STANDARD_CUE",
        ),
    )
    for pattern, label, reason_code in patterns:
        if re.search(pattern, sample, re.IGNORECASE):
            return {
                "label": label,
                "confidence": 0.72,
                "reason_codes": [reason_code, "SOURCE_TEXT_CUE_ONLY"],
            }
    return {
        "label": "类型待判定",
        "confidence": 0.0,
        "reason_codes": [
            "INSUFFICIENT_DOCUMENT_TYPE_EVIDENCE",
            f"SOURCE_FORMAT_{document_format.upper()}",
        ],
    }


def _with_inferred_page_ends(
    candidates: list[dict[str, Any]],
    *,
    page_count: int,
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for index, candidate in enumerate(candidates):
        current = dict(candidate)
        page_start = int(current["page_start"])
        page_end = page_start
        if current["kind"] != "TOC_ENTRY":
            for following in candidates[index + 1 :]:
                if following["kind"] == "TOC_ENTRY":
                    continue
                if int(following["level_hint"]) > int(current["level_hint"]):
                    continue
                following_page = int(following["page_start"])
                page_end = (
                    max(page_start, following_page - 1)
                    if following_page > page_start
                    else page_start
                )
                break
            else:
                page_end = max(page_start, page_count)
        current["page_end"] = page_end
        enriched.append(current)
    return enriched


def _normalized_title(value: str) -> str:
    return " ".join(str(value or "").split())[:500]


def _level_hint(title: str, kind: SourceNodeKind) -> int:
    if kind is SourceNodeKind.TOC_ENTRY:
        return 1
    match = re.match(r"^(\d{1,3}(?:\.\d{1,3}){0,6})", title)
    if match:
        return min(7, match.group(1).count(".") + 1)
    if _APPENDIX_PATTERN.search(title) or _ATTACHMENT_PATTERN.search(title):
        return 1
    return 1


def _stable_id(prefix: str, *parts: str) -> str:
    payload = json.dumps(
        [prefix, *parts],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:32]}"


__all__ = [
    "build_reading_projection",
    "build_reading_source_page",
    "source_slice_for_ref",
]
