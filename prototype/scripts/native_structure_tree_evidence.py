from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import fitz


SCHEMA_VERSION = "native-structure-node-evidence/v1"
_PAGE_BOUNDS_RE = re.compile(r"^- Bounds: `\[([^\]]+)\]`$")
_BLOCK_RE = re.compile(r"^### `([^`]+)` .* `\[([^\]]+)\]`$")
_MATCH_TRANSLATION = str.maketrans("", "", "，。：；、（）()")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _match_text(value: str) -> str:
    return re.sub(r"\s+", "", value).translate(_MATCH_TRANSLATION).lower()


def _bbox(value: Any) -> list[float]:
    result = [float(item) for item in value]
    if (
        len(result) != 4
        or result[2] <= result[0]
        or result[3] <= result[1]
    ):
        raise ValueError("invalid bbox")
    return result


def _parse_page_markdown(path: Path, page_number: int) -> dict[str, Any]:
    lines = path.read_text(encoding="utf-8").splitlines()
    page_bounds: list[float] | None = None
    blocks: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        bounds_match = _PAGE_BOUNDS_RE.match(lines[index])
        if bounds_match:
            page_bounds = _bbox(
                [item.strip() for item in bounds_match.group(1).split(",")]
            )
        block_match = _BLOCK_RE.match(lines[index])
        if not block_match:
            index += 1
            continue
        source_handle = block_match.group(1)
        block_bbox = _bbox(
            [item.strip() for item in block_match.group(2).split(",")]
        )
        index += 1
        while index < len(lines) and not re.fullmatch(r"`{3,}text", lines[index]):
            index += 1
        if index >= len(lines):
            continue
        fence = lines[index][:-4]
        index += 1
        text_lines: list[str] = []
        while index < len(lines) and lines[index] != fence:
            text_lines.append(lines[index])
            index += 1
        blocks.append(
            {
                "page": page_number,
                "source_handle": source_handle,
                "bbox": block_bbox,
                "text": "\n".join(text_lines).strip(),
            }
        )
        index += 1
    if page_bounds is None:
        raise ValueError(f"page bounds missing: {path}")
    return {"page": page_number, "source_bounds": page_bounds, "blocks": blocks}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"JSONL item is not an object: {path}")
            result.append(value)
    return result


def _native_lines(pdf_path: Path) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = {}
    with fitz.open(pdf_path) as document:
        for page_index in range(document.page_count):
            page = document[page_index]
            lines: list[dict[str, Any]] = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    text = "".join(
                        str(span.get("text") or "") for span in line.get("spans", [])
                    ).strip()
                    if not text:
                        continue
                    lines.append({"text": text, "bbox": _bbox(line["bbox"])})
            result[page_index + 1] = lines
    return result


def _candidate_texts(node: dict[str, Any]) -> list[str]:
    label = str(node.get("label") or "").strip()
    title = str(node.get("title") or "").strip()
    result = []
    if label or title:
        result.append(_match_text(f"{label}{title}"))
    if label:
        result.append(_match_text(label))
    if title:
        result.append(_match_text(title))
    return [item for item in dict.fromkeys(result) if item]


def _unique_line_anchor(
    node: dict[str, Any],
    *,
    lines_by_page: dict[int, list[dict[str, Any]]],
) -> tuple[int, dict[str, Any]] | None:
    for candidate in _candidate_texts(node):
        hits: list[tuple[int, dict[str, Any]]] = []
        for page in node.get("pages", []):
            for line in lines_by_page.get(int(page), []):
                normalized = _match_text(line["text"])
                if (
                    normalized == candidate
                    or normalized.startswith(candidate)
                    or (
                        candidate.startswith(normalized)
                        and len(normalized) >= max(4, int(len(candidate) * 0.7))
                    )
                ):
                    hits.append((int(page), line))
        unique = {
            (page, tuple(line["bbox"]), line["text"]): (page, line)
            for page, line in hits
        }
        if len(unique) == 1:
            return next(iter(unique.values()))
    return None


def _unique_block_anchor(
    node: dict[str, Any],
    *,
    blocks_by_page: dict[int, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    for candidate in _candidate_texts(node):
        hits: list[dict[str, Any]] = []
        for page in node.get("pages", []):
            for block in blocks_by_page.get(int(page), []):
                normalized = _match_text(block["text"])
                if candidate in normalized:
                    hits.append(block)
        unique = {item["source_handle"]: item for item in hits}
        if len(unique) == 1:
            return next(iter(unique.values()))
    return None


def _union_bboxes(values: list[list[float]]) -> list[float]:
    if not values:
        raise ValueError("cannot union empty bboxes")
    return [
        min(value[0] for value in values),
        min(value[1] for value in values),
        max(value[2] for value in values),
        max(value[3] for value in values),
    ]


def _table_region_bbox(
    table: dict[str, Any],
    *,
    row_range: list[int],
    visual_bbox: Any = None,
) -> list[float]:
    if visual_bbox:
        return _bbox(visual_bbox)
    start, end = [int(item) for item in row_range]
    bboxes = [
        _bbox(cell.get("native_locator", {}).get("bbox", []))
        for cell in table.get("cells", [])
        if start <= int(cell.get("row", -1)) < end
        and cell.get("native_locator", {}).get("bbox")
    ]
    return _union_bboxes(bboxes)


def _table_preview_fragment(
    *,
    scope_id: str,
    table: dict[str, Any],
    row_range: list[int],
    source_bounds: list[float],
    visual_bbox: Any = None,
) -> dict[str, Any]:
    start, end = [int(item) for item in row_range]
    cells = []
    for cell in table.get("cells", []):
        row = int(cell.get("row", -1))
        row_span = max(1, int(cell.get("row_span", 1)))
        cell_end = row + row_span
        if cell_end <= start or row >= end:
            continue
        locator = cell.get("native_locator", {})
        if not locator.get("bbox"):
            continue
        clipped_start = max(row, start)
        clipped_end = min(cell_end, end)
        text = str(
            cell.get("raw_text")
            or cell.get("display_value")
            or ""
        )
        cells.append(
            {
                "cell_id": str(cell.get("cell_id") or ""),
                "row": clipped_start - start,
                "column": int(cell.get("column", 0)),
                "row_span": clipped_end - clipped_start,
                "column_span": max(1, int(cell.get("column_span", 1))),
                "text": text,
                "text_sha256": _sha256_bytes(text.encode("utf-8")),
                "bbox": _bbox(locator["bbox"]),
            }
        )
    cells.sort(key=lambda item: (item["row"], item["column"], item["cell_id"]))
    table_token = str(table.get("table_token") or "")
    page = int(table.get("page_number") or 0)
    bbox = _table_region_bbox(
        table,
        row_range=row_range,
        visual_bbox=visual_bbox,
    )
    return {
        "fragment_id": f"table-preview-{_canonical_sha256([scope_id, table_token, row_range])[:24]}",
        "logical_table_scope_id": scope_id,
        "table_token": table_token,
        "page": page,
        "row_range": [start, end],
        "row_count": end - start,
        "column_count": int(table.get("column_count") or 0),
        "bbox": bbox,
        "source_bounds": source_bounds,
        "cells": cells,
    }


def _tree_preorder(tree: dict[str, Any]) -> list[dict[str, Any]]:
    nodes = {str(item["node_id"]): item for item in tree.get("nodes", [])}
    children: dict[str, list[dict[str, Any]]] = {}
    for item in nodes.values():
        parent_id = str(item.get("parent_id") or "__root__")
        children.setdefault(parent_id, []).append(item)
    for siblings in children.values():
        siblings.sort(key=lambda item: int(item.get("order") or 0))
    result: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        result.append(node)
        for child in children.get(str(node["node_id"]), []):
            visit(child)

    for root_id in tree.get("root_ids", []):
        visit(nodes[str(root_id)])
    return result


def _overlap_ratio(bbox: list[float], region: list[float]) -> float:
    left = max(bbox[0], region[0])
    top = max(bbox[1], region[1])
    right = min(bbox[2], region[2])
    bottom = min(bbox[3], region[3])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    area = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    return intersection / area


def _build_source_content(
    *,
    tree: dict[str, Any],
    line_matches: dict[str, tuple[int, dict[str, Any]]],
    block_matches: dict[str, dict[str, Any]],
    node_bindings: list[dict[str, Any]],
    blocks_by_page: dict[int, list[dict[str, Any]]],
    source_map: dict[str, dict[str, Any]],
    lines_by_page: dict[int, list[dict[str, Any]]],
    page_bounds: dict[int, list[float]],
) -> dict[str, list[dict[str, Any]]]:
    ordered_nodes = _tree_preorder(tree)
    bindings = {str(item["node_id"]): item for item in node_bindings}
    primary_handles = {
        node_id: str(match["source_handle"])
        for node_id, match in block_matches.items()
    }
    blocks_by_handle = {
        str(block["source_handle"]): block
        for blocks in blocks_by_page.values()
        for block in blocks
    }
    ordered_blocks = sorted(
        (
            {
                **block,
                "source_order": int(
                    source_map.get(str(block["source_handle"]), {}).get(
                        "source_order", 0
                    )
                ),
            }
            for block in blocks_by_handle.values()
        ),
        key=lambda item: (item["source_order"], item["page"]),
    )
    node_positions = {
        str(node["node_id"]): index for index, node in enumerate(ordered_nodes)
    }
    result: dict[str, list[dict[str, Any]]] = {}
    for node in ordered_nodes:
        node_id = str(node["node_id"])
        primary_handle = primary_handles.get(node_id, "")
        if not primary_handle:
            result[node_id] = []
            continue
        primary_fact = source_map.get(primary_handle, {})
        start_order = int(primary_fact.get("source_order", 0))
        table_regions = [
            anchor
            for anchor in bindings[node_id]["anchors"]
            if anchor["anchor_kind"] == "TABLE_REGION"
        ]
        content: list[dict[str, Any]] = []
        if node_id in line_matches:
            page, title_line = line_matches[node_id]
            start_y = float(title_line["bbox"][1])
            end_page = max(page_bounds) + 1
            end_y = float("inf")
            position = node_positions[node_id]
            for following in ordered_nodes[position + 1 :]:
                following_id = str(following["node_id"])
                following_match = line_matches.get(following_id)
                if not following_match:
                    continue
                candidate_page = int(following_match[0])
                candidate_y = float(following_match[1]["bbox"][1])
                if candidate_page > page or (
                    candidate_page == page and candidate_y > start_y
                ):
                    end_page = candidate_page
                    end_y = candidate_y
                    break
            selected_lines: list[dict[str, Any]] = []
            allowed_pages = {int(value) for value in node.get("pages", [])}
            for candidate_page in sorted(allowed_pages):
                if candidate_page < page or candidate_page > end_page:
                    continue
                lower = start_y if candidate_page == page else float("-inf")
                upper = end_y if candidate_page == end_page else float("inf")
                page_regions = [
                    region
                    for region in table_regions
                    if int(region["page"]) == candidate_page
                ]
                for line in lines_by_page.get(candidate_page, []):
                    top = float(line["bbox"][1])
                    if top < lower - 0.01 or top >= upper - 0.01:
                        continue
                    if any(
                        _overlap_ratio(line["bbox"], region["bbox"]) >= 0.6
                        for region in page_regions
                    ):
                        continue
                    selected_lines.append({**line, "page": candidate_page})
            groups: list[list[dict[str, Any]]] = []
            for line in selected_lines:
                if not groups:
                    groups.append([line])
                    continue
                previous = groups[-1][-1]
                previous_height = float(previous["bbox"][3]) - float(
                    previous["bbox"][1]
                )
                gap = float(line["bbox"][1]) - float(previous["bbox"][3])
                same_page = int(line["page"]) == int(previous["page"])
                if same_page and gap <= max(7.0, previous_height * 0.8):
                    groups[-1].append(line)
                else:
                    groups.append([line])
            for group_index, group in enumerate(groups):
                content_page = int(group[0]["page"])
                text = "\n".join(line["text"] for line in group).strip()
                bbox = _union_bboxes([line["bbox"] for line in group])
                source_handles = sorted(
                    {
                        str(block["source_handle"])
                        for block in blocks_by_page.get(content_page, [])
                        if _overlap_ratio(bbox, block["bbox"]) > 0
                    }
                )
                content.append(
                    {
                        "content_id": f"content-{_canonical_sha256([node_id, content_page, bbox, text])[:24]}",
                        "page": content_page,
                        "bbox": bbox,
                        "source_bounds": page_bounds[content_page],
                        "source_handles": source_handles or [primary_handle],
                        "text": text,
                        "text_sha256": _sha256_bytes(text.encode("utf-8")),
                        "precision": "EXACT_NATIVE_SPAN_RANGE",
                        "is_title": group_index == 0,
                    }
                )
        else:
            end_order = 2**63 - 1
            position = node_positions[node_id]
            for following in ordered_nodes[position + 1 :]:
                following_handle = primary_handles.get(str(following["node_id"]), "")
                if not following_handle:
                    continue
                following_order = int(
                    source_map.get(following_handle, {}).get("source_order", 0)
                )
                if following_order > start_order:
                    end_order = following_order
                    break
            for block in ordered_blocks:
                if not (start_order <= int(block["source_order"]) < end_order):
                    continue
                if int(block["page"]) not in {
                    int(page) for page in node.get("pages", [])
                }:
                    continue
                if block["source_handle"] != primary_handle and any(
                    int(region["page"]) == int(block["page"])
                    and _overlap_ratio(block["bbox"], region["bbox"]) >= 0.6
                    for region in table_regions
                ):
                    continue
                text = str(block["text"] or "").strip()
                if not text:
                    continue
                content.append(
                    {
                        "content_id": f"content-{_canonical_sha256([node_id, block['source_handle']])[:24]}",
                        "page": int(block["page"]),
                        "bbox": block["bbox"],
                        "source_bounds": page_bounds[int(block["page"])],
                        "source_handles": [str(block["source_handle"])],
                        "text": text,
                        "text_sha256": _sha256_bytes(text.encode("utf-8")),
                        "precision": "EXACT_SOURCE_RANGE",
                        "is_title": block["source_handle"] == primary_handle,
                    }
                )
        result[node_id] = content
    return result


def build_evidence_binding(
    *,
    tree: dict[str, Any],
    reference: dict[str, Any],
    workspace_root: Path,
    source_sha256: str,
) -> dict[str, Any]:
    page_records = {
        page: _parse_page_markdown(
            workspace_root / "pages" / f"P{page:04d}.md", page
        )
        for page in range(1, int(reference["source"]["page_count"]) + 1)
    }
    blocks_by_page = {
        page: record["blocks"] for page, record in page_records.items()
    }
    page_bounds = {
        page: record["source_bounds"] for page, record in page_records.items()
    }
    source_map = {
        str(item.get("source_handle") or ""): item
        for item in _read_jsonl(workspace_root / "pages" / "source-map.jsonl")
    }
    lines_by_page = _native_lines(workspace_root / "source" / "document.pdf")
    logical_tables = {
        str(item.get("scope_id") or ""): item
        for item in reference.get("logical_tables", [])
    }
    physical_tables = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in (workspace_root / "tables").glob("T*.json")
    }
    table_previews = []
    for scope_id, logical_table in logical_tables.items():
        for fragment in logical_table.get("fragments", []):
            table_token = str(fragment.get("table_token") or "")
            physical_table = physical_tables.get(table_token)
            if physical_table is None:
                raise ValueError(f"missing logical table fragment: {table_token}")
            page = int(fragment.get("page") or 0)
            table_previews.append(
                _table_preview_fragment(
                    scope_id=scope_id,
                    table=physical_table,
                    row_range=list(fragment.get("row_range") or []),
                    source_bounds=page_bounds[page],
                    visual_bbox=fragment.get("visual_bbox"),
                )
            )

    block_matches: dict[str, dict[str, Any]] = {}
    line_matches: dict[str, tuple[int, dict[str, Any]]] = {}
    for node in tree.get("nodes", []):
        line_match = _unique_line_anchor(node, lines_by_page=lines_by_page)
        if line_match:
            line_matches[str(node["node_id"])] = line_match
        if node.get("title_authority") == "DERIVED_FROM_VISIBLE_TABLE_ROLE":
            continue
        match = _unique_block_anchor(node, blocks_by_page=blocks_by_page)
        if match:
            block_matches[str(node["node_id"])] = match
    shared_handles = Counter(
        item["source_handle"] for item in block_matches.values()
    )

    node_bindings = []
    summary = Counter()
    for node in tree.get("nodes", []):
        node_id = str(node["node_id"])
        anchors: list[dict[str, Any]] = []
        line_match = line_matches.get(node_id)
        block_match = block_matches.get(node_id)
        if line_match:
            page, line = line_match
            source_handle = str(block_match.get("source_handle") if block_match else "")
            source_fact = source_map.get(source_handle, {})
            anchors.append(
                {
                    "anchor_id": f"anchor-{_canonical_sha256([node_id, page, line['bbox'], 'TITLE'])[:24]}",
                    "page": page,
                    "bbox": line["bbox"],
                    "source_bounds": page_bounds[page],
                    "anchor_kind": "TITLE",
                    "precision": "EXACT_NATIVE_SPAN",
                    "source_handle": source_handle,
                    "source_text_sha256": str(
                        source_fact.get("raw_text_sha256")
                        or _sha256_bytes(line["text"].encode("utf-8"))
                    ),
                }
            )
            summary["exact_native_span"] += 1
        elif block_match:
            page = int(block_match["page"])
            source_handle = str(block_match["source_handle"])
            source_fact = source_map.get(source_handle, {})
            precision = (
                "SHARED_BLOCK_APPROXIMATE"
                if shared_handles[source_handle] > 1
                else "EXACT_SOURCE_BLOCK"
            )
            anchors.append(
                {
                    "anchor_id": f"anchor-{_canonical_sha256([node_id, page, block_match['bbox'], 'SOURCE_BLOCK'])[:24]}",
                    "page": page,
                    "bbox": block_match["bbox"],
                    "source_bounds": page_bounds[page],
                    "anchor_kind": "SOURCE_BLOCK",
                    "precision": precision,
                    "source_handle": source_handle,
                    "source_text_sha256": str(
                        source_fact.get("raw_text_sha256") or ""
                    ),
                }
            )
            summary[precision.lower()] += 1

        for content_block in node.get("content_blocks", []):
            if content_block.get("kind") != "LOGICAL_TABLE":
                continue
            scope_id = str(content_block.get("logical_table_scope_id") or "")
            logical_table = logical_tables.get(scope_id)
            if logical_table is None:
                raise ValueError(f"missing logical table: {scope_id}")
            for fragment in logical_table.get("fragments", []):
                table_token = str(fragment.get("table_token") or "")
                physical_table = physical_tables.get(table_token)
                if physical_table is None:
                    raise ValueError(f"missing physical table: {table_token}")
                page = int(fragment["page"])
                region_bbox = _table_region_bbox(
                    physical_table,
                    row_range=list(fragment["row_range"]),
                    visual_bbox=fragment.get("visual_bbox"),
                )
                anchors.append(
                    {
                        "anchor_id": f"anchor-{_canonical_sha256([node_id, page, table_token, fragment['row_range']])[:24]}",
                        "page": page,
                        "bbox": region_bbox,
                        "source_bounds": page_bounds[page],
                        "anchor_kind": "TABLE_REGION",
                        "precision": "EXACT_TABLE_REGION",
                        "logical_table_scope_id": scope_id,
                        "table_token": table_token,
                        "row_range": list(fragment["row_range"]),
                    }
                )
                summary["exact_table_region"] += 1

        if not anchors:
            raise ValueError(f"structure node has no evidence anchor: {node_id}")
        anchors.sort(
            key=lambda item: (
                int(item["page"]),
                0 if item["anchor_kind"] == "TITLE" else 1,
                str(item["anchor_id"]),
            )
        )
        node_bindings.append({"node_id": node_id, "anchors": anchors})

    bindings_by_node = {
        str(item["node_id"]): item for item in node_bindings
    }
    table_regions = [
        (node_id, anchor)
        for node_id, item in bindings_by_node.items()
        for anchor in item["anchors"]
        if anchor["anchor_kind"] == "TABLE_REGION"
    ]
    image_proposals: list[dict[str, Any]] = []
    unbound_images = 0
    placements_path = workspace_root / "assets" / "placements.jsonl"
    placements = _read_jsonl(placements_path) if placements_path.is_file() else []
    for placement in placements:
        page = int(placement.get("page_number") or 0)
        placement_bbox = _bbox(placement.get("bbox", []))
        candidates = sorted(
            (
                (
                    _overlap_ratio(placement_bbox, anchor["bbox"]),
                    node_id,
                    anchor,
                )
                for node_id, anchor in table_regions
                if int(anchor["page"]) == page
            ),
            key=lambda item: (item[0], item[1]),
            reverse=True,
        )
        best = candidates[0] if candidates else (0.0, "", {})
        second_score = candidates[1][0] if len(candidates) > 1 else 0.0
        if best[0] < 0.6 or best[0] - second_score < 0.15:
            unbound_images += 1
            continue
        node_id = str(best[1])
        placement_id = str(placement.get("placement_id") or "")
        asset_id = str(placement.get("asset_id") or "")
        image_anchor = {
            "anchor_id": f"anchor-{_canonical_sha256([node_id, placement_id, placement_bbox])[:24]}",
            "page": page,
            "bbox": placement_bbox,
            "source_bounds": page_bounds[page],
            "anchor_kind": "IMAGE",
            "precision": "EXACT_IMAGE",
            "placement_id": placement_id,
            "asset_id": asset_id,
        }
        bindings_by_node[node_id]["anchors"].append(image_anchor)
        proposal_id = f"proposal-{_canonical_sha256([placement_id, node_id, 'ATTACH_IMAGE_TO_NODE'])[:24]}"
        image_proposals.append(
            {
                "proposal_id": proposal_id,
                "finding_code": "visual_asset_parent_binding_missing",
                "node_id": node_id,
                "object_kind": "IMAGE_PLACEMENT",
                "object_id": placement_id,
                "page": page,
                "bbox": placement_bbox,
                "source_bounds": page_bounds[page],
                "observation": "图片位置完整落在当前节点的已验证表格区域内，且没有竞争表格区域。",
                "recommended_action": "ATTACH_IMAGE_TO_NODE",
                "recommended_target_node_id": node_id,
                "impact": "只补充图片证据归属，不修改原文、标题层级或表格内容。",
                "confidence": "HIGH",
                "default_policy": "AUTO_APPLY_REVERSIBLE",
                "status": "AUTO_APPLIED_PENDING_REVIEW",
                "overlap_ratio": round(float(best[0]), 6),
            }
        )
        summary["exact_image"] += 1

    for item in node_bindings:
        item["anchors"].sort(
            key=lambda anchor: (
                int(anchor["page"]),
                {"TITLE": 0, "SOURCE_BLOCK": 1, "TABLE_REGION": 2, "IMAGE": 3}.get(
                    str(anchor["anchor_kind"]), 9
                ),
                str(anchor["anchor_id"]),
            )
        )

    source_content = _build_source_content(
        tree=tree,
        line_matches=line_matches,
        block_matches=block_matches,
        node_bindings=node_bindings,
        blocks_by_page=blocks_by_page,
        source_map=source_map,
        lines_by_page=lines_by_page,
        page_bounds=page_bounds,
    )
    for item in node_bindings:
        item["source_content"] = source_content.get(str(item["node_id"]), [])

    return {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "tree_sha256": _canonical_sha256(tree),
        "summary": {
            "node_count": len(node_bindings),
            "anchored_node_count": len(node_bindings),
            "exact_native_span_count": summary["exact_native_span"],
            "exact_source_block_count": summary["exact_source_block"],
            "shared_block_approximate_count": summary[
                "shared_block_approximate"
            ],
            "exact_table_region_count": summary["exact_table_region"],
            "exact_image_count": summary["exact_image"],
            "unbound_image_count": unbound_images,
            "content_node_count": sum(
                bool(item.get("source_content")) for item in node_bindings
            ),
            "source_content_block_count": sum(
                len(item.get("source_content", [])) for item in node_bindings
            ),
            "table_preview_fragment_count": len(table_previews),
            "table_preview_cell_count": sum(
                len(item.get("cells", [])) for item in table_previews
            ),
        },
        "nodes": node_bindings,
        "proposals": image_proposals,
        "table_previews": table_previews,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tree = json.loads(args.tree.read_text(encoding="utf-8"))
    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    source_sha256 = str(reference.get("source", {}).get("sha256") or "")
    result = build_evidence_binding(
        tree=tree,
        reference=reference,
        workspace_root=args.workspace,
        source_sha256=source_sha256,
    )
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
