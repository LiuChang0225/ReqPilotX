from __future__ import annotations

import hashlib
import re
from contextlib import redirect_stderr, redirect_stdout
from difflib import SequenceMatcher
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

try:
    import fitz
except ImportError:  # pragma: no cover - optional runtime dependency
    fitz = None


_TABLE_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
_TABLE_FONT_SIZE = 13
_TEXT_FONT_SIZE = 14
_TABLE_CELL_PADDING_X = 12
_TABLE_CELL_PADDING_Y = 8
_TABLE_LINE_SPACING = 4
_TABLE_UNIT_PX = 7
_TABLE_MIN_COLUMN_UNITS = 10
_SNAPSHOT_MAX_WIDTH_PX = 920
_TABLE_EXPORT_REASON_PRIORITY = (
    "含图片",
    "结构低置信",
    "跨页续表",
    "复杂合并",
    "切片可读性差",
    "原文更忠实",
)


def enrich_entries_with_table_assets(
    entries: list[dict[str, Any]],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] = {}
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for row in entries:
        enriched_row, row_assets = enrich_entry_with_table_assets(
            row,
            document_id=document_id,
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
        )
        enriched.append(enriched_row)
        assets.extend(row_assets)
    return enriched, assets


def enrich_entry_with_table_assets(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str = "",
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None = None,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    copied = dict(row)
    existing_table_refs = _normalize_table_refs(copied.get("table_refs"))
    text = _entry_source_text(copied)
    if not text:
        copied["table_refs"] = []
        copied["content_blocks"] = _normalize_content_blocks(copied.get("content_blocks"))
        copied["has_table_asset"] = False
        copied["table_render_mode"] = ""
        return copied, []

    blocks = _split_text_blocks(text)
    if not any(block["kind"] == "table" for block in blocks):
        copied["table_refs"] = []
        copied["content_blocks"] = _paragraph_only_blocks(blocks)
        copied["has_table_asset"] = False
        copied["table_render_mode"] = ""
        return copied, []

    table_refs: list[dict[str, Any]] = []
    content_blocks: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    source_page_floor_offsets: dict[int, float] = {}

    for block_index, block in enumerate(blocks):
        if block["kind"] == "paragraph":
            text_value = str(block.get("text") or "").strip()
            if text_value:
                content_blocks.append({"kind": "paragraph", "text": text_value})
            continue

        table_text = str(block.get("text") or "").strip()
        if not table_text:
            continue
        before_context_text = _neighbor_block_text(blocks, block_index, offset=-1)
        after_context_text = _neighbor_block_text(blocks, block_index, offset=1)
        table_id = _build_table_id(copied, table_text, block_index)
        headers, body_rows, render_mode = _extract_structured_table(block.get("lines") or [])
        snapshot_path = _write_snapshot(
            table_id=table_id,
            snapshot_root=snapshot_root,
            headers=headers,
            rows=body_rows,
            render_mode=render_mode,
            text=table_text,
        )
        notes = _collect_block_notes(blocks, block_index)
        table_title = _table_title_for_row(copied)
        source_snapshot_path, source_snapshot_meta = _write_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=_coerce_int(copied.get("source_page")),
            source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=body_rows,
            notes=notes,
            before_context_text=before_context_text,
            after_context_text=after_context_text,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
            source_page_floor_offsets=source_page_floor_offsets,
        )
        ai_projection = _build_ai_projection(
            table_id=table_id,
            title=table_title,
            headers=headers,
            rows=body_rows,
            notes=notes,
            render_mode=render_mode,
        )
        asset = {
            "table_id": table_id,
            "document_id": document_id,
            "section_instance_key": str(copied.get("section_instance_key") or copied.get("section_key") or ""),
            "page_span": {
                "start": _coerce_int(copied.get("source_page")),
                "end": _coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
            },
            "table_title": table_title,
            "headers": headers,
            "rows": body_rows,
            "group_labels": [],
            "notes": notes,
            "render_mode": render_mode,
            "snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path) if source_snapshot_path else "",
            "ai_projection": ai_projection,
            "confidence": 0.88 if render_mode == "structured" else 0.42,
            "source_entry_id": str(copied.get("entry_id") or copied.get("req_code") or ""),
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
        }
        table_ref = {
            "table_id": table_id,
            "row_range": [1, len(body_rows)] if body_rows else [],
            "row_ids": [f"r{index + 1}" for index in range(len(body_rows))],
            "inherited_headers": headers,
            "slice_rows": body_rows,
            "inherited_group_title": "",
            "inherited_notes": notes,
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path) if source_snapshot_path else "",
            "slice_render_key": table_id,
            "slice_render_mode": render_mode,
            "source_page": _coerce_int(copied.get("source_page")),
            "source_page_end": _coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
            "structure_confidence": float(asset["confidence"]),
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
        }
        existing_ref = _match_existing_table_ref(existing_table_refs, table_id=table_id, index=len(table_refs))
        if existing_ref:
            table_ref["export_variant"] = str(existing_ref.get("export_variant") or "").strip().lower()
        table_ref = _normalize_table_ref(table_ref)
        assets.append(asset)
        table_refs.append(table_ref)
        content_blocks.append({"kind": "table", "table_id": table_id})

    copied["table_refs"] = table_refs
    copied["content_blocks"] = content_blocks
    copied["has_table_asset"] = bool(table_refs)
    copied["table_render_mode"] = _collapse_render_mode(table_refs)
    return copied, assets


def prepare_table_ref_urls(
    row: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    copied = dict(row)
    refs = []
    for item in _normalize_table_refs(copied.get("table_refs")):
        ref = dict(item)
        table_id = str(ref.get("table_id") or "").strip()
        if table_id:
            ref["table_asset_url"] = (
                f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}"
            )
            slice_version = _snapshot_version_token(ref.get("slice_snapshot_path"))
            ref["slice_snapshot_url"] = (
                f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/snapshot?v={slice_version}"
            )
            if str(ref.get("source_snapshot_path") or "").strip():
                source_version = _snapshot_version_token(ref.get("source_snapshot_path"))
                ref["source_snapshot_url"] = (
                    f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/source-snapshot?v={source_version}"
                )
        refs.append(ref)
    copied["table_refs"] = refs
    copied["has_table_asset"] = bool(refs)
    copied["table_render_mode"] = str(copied.get("table_render_mode") or _collapse_render_mode(refs))
    copied["content_blocks"] = _normalize_content_blocks(copied.get("content_blocks"))
    return copied


def prepare_table_asset_urls(
    asset: dict[str, Any],
    *,
    project_id: str,
    document_id: str,
) -> dict[str, Any]:
    copied = dict(asset)
    table_id = str(copied.get("table_id") or "").strip()
    if table_id:
        copied["asset_url"] = f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}"
        snapshot_version = _snapshot_version_token(copied.get("snapshot_path"))
        copied["snapshot_url"] = (
            f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/snapshot?v={snapshot_version}"
        )
        if str(copied.get("source_snapshot_path") or "").strip():
            source_version = _snapshot_version_token(copied.get("source_snapshot_path"))
            copied["source_snapshot_url"] = (
                f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/source-snapshot?v={source_version}"
            )
    return copied


def export_blocks_from_requirement(item: dict[str, Any]) -> list[tuple[str, Any]]:
    table_ref_map = {
        str(ref.get("table_id") or "").strip(): ref
        for ref in _normalize_table_refs(item.get("table_refs"))
        if str(ref.get("table_id") or "").strip()
    }
    content_blocks = _normalize_content_blocks(item.get("content_blocks"))
    if not content_blocks:
        return []

    blocks: list[tuple[str, Any]] = []
    for block in content_blocks:
        kind = str(block.get("kind") or "").strip().lower()
        if kind == "paragraph":
            text = str(block.get("text") or "").strip()
            if text:
                blocks.append(("paragraph", text))
            continue
        if kind != "table":
            continue
        table_id = str(block.get("table_id") or "").strip()
        ref = table_ref_map.get(table_id)
        if ref:
            blocks.append(("table_ref", ref))
    return blocks


def load_snapshot_bytes(snapshot_path: str) -> bytes:
    path = Path(str(snapshot_path or "").strip())
    return path.read_bytes()


def render_table_png(headers: list[str], rows: list[list[str]], *, max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX) -> bytes:
    image_bytes, _, _ = _render_table_image([headers, *rows], max_width_px=max_width_px)
    return image_bytes.getvalue()


def render_text_snapshot_png(text: str, *, max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX) -> bytes:
    image_bytes, _, _ = _render_text_snapshot(text, max_width_px=max_width_px)
    return image_bytes.getvalue()


def _entry_source_text(row: dict[str, Any]) -> str:
    return str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "").strip()


def _split_text_blocks(text: str) -> list[dict[str, Any]]:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    blocks: list[dict[str, Any]] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        merged = "\n".join(part for part in paragraph if str(part).strip()).strip()
        paragraph.clear()
        if merged:
            blocks.append({"kind": "paragraph", "text": merged})

    index = 0
    while index < len(lines):
        if _starts_table_block(lines, index):
            flush_paragraph()
            table_lines = [lines[index]]
            index += 1
            while index < len(lines) and _looks_like_markdown_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            table_text = "\n".join(line for line in table_lines if str(line).strip()).strip()
            blocks.append({"kind": "table", "text": table_text, "lines": table_lines})
            continue
        paragraph.append(lines[index])
        index += 1
    flush_paragraph()
    return blocks


def _starts_table_block(lines: list[str], index: int) -> bool:
    if index >= len(lines):
        return False
    return _looks_like_markdown_table_row(lines[index])


def _extract_structured_table(lines: list[str]) -> tuple[list[str], list[list[str]], str]:
    parsed = _parse_markdown_table(lines)
    if not parsed:
        parsed = _parse_pipe_table(lines)
    if not parsed:
        parsed = _parse_single_row_pipe_table(lines)
    if not parsed:
        return [], [], "snapshot_only"
    header = parsed[0]
    body = parsed[1:] if len(parsed) > 1 else []
    return header, body, "structured"


def _parse_markdown_table(lines: list[str]) -> list[list[str]]:
    rows = [_parse_markdown_cells(line) for line in lines if _parse_markdown_cells(line)]
    if len(rows) < 3:
        return []
    header = rows[0]
    separator = rows[1]
    if len(header) < 2 or len(separator) != len(header) or not _is_markdown_separator_row(separator):
        return []
    if any(len(row) != len(header) for row in rows[2:]):
        return []
    return [header, *rows[2:]]


def _parse_pipe_table(lines: list[str]) -> list[list[str]]:
    rows = [_parse_markdown_cells(line) for line in lines if _looks_like_markdown_table_row(line)]
    if len(rows) < 2:
        return []
    expected_columns = max(len(row) for row in rows)
    if expected_columns < 2:
        return []
    normalized: list[list[str]] = []
    mismatched = 0
    for row in rows:
        if len(row) == expected_columns:
            normalized.append(row)
            continue
        if abs(len(row) - expected_columns) <= 1 and expected_columns >= 3:
            normalized.append((row + [""] * expected_columns)[:expected_columns])
            mismatched += 1
            continue
        mismatched += 1
    if len(normalized) < 2 or mismatched > max(1, len(rows) // 2):
        return []
    return normalized


def _parse_single_row_pipe_table(lines: list[str]) -> list[list[str]]:
    rows = [_parse_markdown_cells(line) for line in lines if _looks_like_markdown_table_row(line)]
    if len(rows) != 1:
        return []
    row = rows[0]
    if len(row) < 2:
        return []
    headers = _infer_headers_for_single_row(row)
    return [headers, row] if headers else []


def _looks_like_markdown_table_row(line: str) -> bool:
    text = str(line or "").strip()
    if not text or "|" not in text:
        return False
    return len(_parse_markdown_cells(text)) >= 2


def _parse_markdown_cells(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    placeholder = "__ESCAPED_PIPE__"
    return [cell.replace(placeholder, "|").strip() for cell in text.replace("\\|", placeholder).split("|")]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", str(cell or "").strip()) for cell in cells)


def _infer_headers_for_single_row(cells: list[str]) -> list[str]:
    if not cells:
        return []
    cell_count = len(cells)
    first = str(cells[0] or "").strip()
    normalized_tokens = [str(cell or "").strip().upper() for cell in cells]
    looks_like_matrix_row = bool(re.fullmatch(r"\d+[A-Za-z（）()\-]*", first)) or any(
        token in {"DV", "PV", "DV/PV"} for token in normalized_tokens
    )
    if looks_like_matrix_row:
        if cell_count == 10:
            return ["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期", "备注"]
        if cell_count == 9:
            return ["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "责任方", "样件", "周期"]
        if cell_count == 8:
            return ["序号", "项目", "参考标准", "试验方法 / 要求", "DV/PV", "责任方", "样件", "周期"]
        if cell_count == 7:
            return ["序号", "项目", "参考标准", "试验方法", "要求", "DV/PV", "样件 / 备注"]
        if cell_count == 6:
            return ["序号", "项目", "试验方法", "要求", "DV/PV", "样件 / 备注"]
    return [f"Col{index + 1}" for index in range(cell_count)]


def _build_table_id(row: dict[str, Any], table_text: str, block_index: int) -> str:
    seed = "|".join(
        [
            str(row.get("entry_id") or row.get("req_code") or ""),
            str(row.get("source_page") or ""),
            str(row.get("source_anchor") or ""),
            str(block_index),
            table_text,
        ]
    )
    return f"tbl_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:12]}"


def _table_title_for_row(row: dict[str, Any]) -> str:
    for key in ("table_title", "parent_section_label", "heading_text", "section_title"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _collect_block_notes(blocks: list[dict[str, Any]], block_index: int) -> list[str]:
    notes: list[str] = []
    if block_index > 0:
        prev_text = str(blocks[block_index - 1].get("text") or "").strip()
        if prev_text:
            notes.append(prev_text)
    if block_index + 1 < len(blocks):
        next_text = str(blocks[block_index + 1].get("text") or "").strip()
        if next_text:
            notes.append(next_text)
    return notes[:2]


def _neighbor_block_text(blocks: list[dict[str, Any]], block_index: int, *, offset: int) -> str:
    neighbor_index = block_index + offset
    if neighbor_index < 0 or neighbor_index >= len(blocks):
        return ""
    return str(blocks[neighbor_index].get("text") or "").strip()


def _build_ai_projection(
    *,
    table_id: str,
    title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
    render_mode: str,
) -> str:
    lines = [f"table_id: {table_id}", f"render_mode: {render_mode}"]
    if title:
        lines.append(f"title: {title}")
    if headers:
        lines.append("headers:")
        for header in headers:
            lines.append(f"  - {header}")
    if rows:
        lines.append("rows:")
        for row_index, cells in enumerate(rows, start=1):
            lines.append(f"  - row_id: r{row_index}")
            if headers and len(headers) == len(cells):
                lines.append("    cells:")
                for header, cell in zip(headers, cells):
                    lines.append(f"      {header}: {cell}")
            else:
                lines.append("    values:")
                for cell in cells:
                    lines.append(f"      - {cell}")
    if notes:
        lines.append("notes:")
        for note in notes:
            lines.append(f"  - {note}")
    return "\n".join(lines).strip()


def _write_snapshot(
    *,
    table_id: str,
    snapshot_root: Path,
    headers: list[str],
    rows: list[list[str]],
    render_mode: str,
    text: str,
) -> Path:
    snapshot_root.mkdir(parents=True, exist_ok=True)
    target = snapshot_root / f"{table_id}.png"
    if render_mode == "structured" and headers:
        image_bytes = render_table_png(headers, rows)
    else:
        image_bytes = render_text_snapshot_png(text)
    target.write_bytes(image_bytes)
    return target


def _write_source_snapshot(
    *,
    snapshot_root: Path,
    source_file_path: str,
    source_page: int,
    source_page_end: int,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
    before_context_text: str,
    after_context_text: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_page_floor_offsets: dict[int, float] | None,
) -> tuple[Path | None, dict[str, Any]]:
    source_path = Path(str(source_file_path or "").strip())
    if fitz is None or not source_path.exists() or source_path.suffix.lower() != ".pdf":
        return None, {}
    if source_page <= 0:
        return None, {}
    page_end = source_page_end if source_page_end >= source_page else source_page
    table_signature = _build_source_table_signature(
        table_text=table_text,
        table_title=table_title,
        headers=headers,
        rows=rows,
        notes=notes,
    )
    context_signature = hashlib.sha1(
        "|".join(
            [
                "source-crop-v16",
                _normalize_match_text(before_context_text),
                _normalize_match_text(after_context_text),
                str(round(float((source_page_floor_offsets or {}).get(source_page, 0.0)), 2)),
            ]
        ).encode("utf-8")
    ).hexdigest()[:10]
    snapshot_signature = f"{table_signature}_{context_signature}"
    cache_key = (str(source_path.resolve()), source_page, page_end, snapshot_signature)
    if source_snapshot_cache is not None and cache_key in source_snapshot_cache:
        cached = source_snapshot_cache[cache_key]
        return cached, {}

    snapshot_root.mkdir(parents=True, exist_ok=True)
    source_seed = "|".join([cache_key[0], str(source_page), str(page_end), snapshot_signature])
    target = snapshot_root / f"src_{hashlib.sha1(source_seed.encode('utf-8')).hexdigest()[:16]}.png"
    if target.exists():
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = target
        return target, {}

    try:
        rendered, render_meta = _render_pdf_page_span_snapshot(
            source_path,
            page_start=source_page,
            page_end=page_end,
            max_width_px=_SNAPSHOT_MAX_WIDTH_PX,
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
            before_context_text=before_context_text,
            after_context_text=after_context_text,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
            source_page_floor_offsets=source_page_floor_offsets,
        )
        target.write_bytes(
            rendered
        )
    except Exception:
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = None
        return None, {}

    if source_snapshot_cache is not None:
        source_snapshot_cache[cache_key] = target
    return target, render_meta


def _paragraph_only_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("kind") or "") != "paragraph":
            continue
        text = str(block.get("text") or "").strip()
        if text:
            output.append({"kind": "paragraph", "text": text})
    return output


def _normalize_table_refs(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        refs.append(_normalize_table_ref(item))
    return refs


def _normalize_table_ref(value: dict[str, Any]) -> dict[str, Any]:
    ref = dict(value)
    recommendation = _recommend_table_export_variant(ref)
    recommended_variant = recommendation["recommended_export_variant"]
    export_variant = _normalize_export_variant(ref.get("export_variant"))
    if not export_variant:
        export_variant = recommended_variant
    ref["recommended_export_variant"] = recommended_variant
    ref["export_variant_reasons"] = recommendation["export_variant_reasons"]
    ref["export_variant_score"] = recommendation["export_variant_score"]
    ref["export_variant"] = export_variant
    ref["export_variant_overridden"] = export_variant != recommended_variant
    return ref


def _normalize_export_variant(value: Any) -> str:
    token = str(value or "").strip().lower()
    return token if token in {"slice", "source"} else ""


def _match_existing_table_ref(
    refs: list[dict[str, Any]],
    *,
    table_id: str,
    index: int,
) -> dict[str, Any] | None:
    for item in refs:
        if str(item.get("table_id") or "").strip() == str(table_id or "").strip():
            return item
    if 0 <= index < len(refs):
        return refs[index]
    return None


def _recommend_table_export_variant(ref: dict[str, Any]) -> dict[str, Any]:
    headers = [str(cell or "").strip() for cell in ref.get("inherited_headers") or ref.get("headers") or []]
    rows = [
        [str(cell or "").strip() for cell in row]
        for row in ref.get("slice_rows") or ref.get("rows") or []
        if isinstance(row, list)
    ]
    notes = [str(item or "").strip() for item in ref.get("inherited_notes") or ref.get("notes") or [] if str(item or "").strip()]
    render_mode = str(ref.get("slice_render_mode") or ref.get("render_mode") or "").strip().lower()
    source_snapshot_path = str(ref.get("source_snapshot_path") or "").strip()
    source_page = _coerce_int(ref.get("source_page"))
    source_page_end = _coerce_int(ref.get("source_page_end")) or source_page
    structure_confidence = float(ref.get("structure_confidence") or ref.get("confidence") or 0.0)
    image_block_count = _coerce_int(ref.get("source_image_block_count"))

    reasons: list[str] = []
    score = 0

    if image_block_count > 0:
        reasons.append("含图片")
        score = max(score, 95)

    if render_mode != "structured":
        reasons.append("结构低置信")
        score = max(score, 85)
    elif 0 < structure_confidence < 0.55:
        reasons.append("结构低置信")
        score = max(score, 72)

    if source_page_end > source_page > 0:
        reasons.append("跨页续表")
        score = max(score, 68)

    if _looks_like_complex_merged_table(rows, notes):
        reasons.append("复杂合并")
        score += 22

    if _looks_like_poor_slice_readability(headers, rows):
        reasons.append("切片可读性差")
        score += 28

    if source_snapshot_path and reasons and any(reason in {"跨页续表", "复杂合并", "切片可读性差", "含图片"} for reason in reasons):
        reasons.append("原文更忠实")
        score += 10

    reasons = _prioritize_export_reasons(reasons)
    recommended_variant = "source" if source_snapshot_path and score >= 60 else "slice"
    if not source_snapshot_path:
        recommended_variant = "slice"
    return {
        "recommended_export_variant": recommended_variant,
        "export_variant_reasons": reasons,
        "export_variant_score": max(0, min(100, int(score))),
    }


def _prioritize_export_reasons(reasons: list[str]) -> list[str]:
    ordered: list[str] = []
    for label in _TABLE_EXPORT_REASON_PRIORITY:
        if label in reasons and label not in ordered:
            ordered.append(label)
    return ordered[:3]


def _looks_like_complex_merged_table(rows: list[list[str]], notes: list[str]) -> bool:
    if len(rows) < 2:
        return False
    normalized_rows = [_normalize_row_cells(row) for row in rows if isinstance(row, list)]
    if len(normalized_rows) < 2:
        return False
    shared_prefix = _shared_cell_prefix_length(normalized_rows)
    shared_suffix = _shared_cell_suffix_length(normalized_rows)
    if shared_prefix >= 2 or shared_suffix >= 2:
        return True
    joined_notes = " ".join(notes)
    return len(joined_notes) >= 80 or len(notes) >= 2


def _looks_like_poor_slice_readability(headers: list[str], rows: list[list[str]]) -> bool:
    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if col_count < 6:
        return False
    long_detail_columns = 0
    max_detail_length = 0
    for index in range(col_count):
        header = headers[index] if index < len(headers) else ""
        if not _is_detail_header(header):
            continue
        column_values = [row[index] for row in rows if index < len(row) and str(row[index]).strip()]
        if not column_values:
            continue
        column_max = max(_estimate_cell_visual_length(value) for value in column_values)
        max_detail_length = max(max_detail_length, column_max)
        if column_max >= 72:
            long_detail_columns += 1
    return (col_count >= 8 and long_detail_columns >= 2) or max_detail_length >= 120


def _is_detail_header(label: str) -> bool:
    value = str(label or "").strip().lower()
    return bool(
        re.search(
            r"(要求|描述|内容|说明|备注|方法|条件|requirements?|description|detail|method|procedure|测试条件|试验内容|试验方法)",
            value,
            flags=re.I,
        )
    )


def _estimate_cell_visual_length(text: str) -> int:
    score = 0
    for ch in str(text or ""):
        score += 2 if re.match(r"[\u4e00-\u9fff]", ch) else 1
    return score


def _normalize_content_blocks(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "paragraph":
            text = str(item.get("text") or "").strip()
            if text:
                blocks.append({"kind": "paragraph", "text": text})
            continue
        if kind == "table":
            table_id = str(item.get("table_id") or "").strip()
            if table_id:
                blocks.append({"kind": "table", "table_id": table_id})
    return blocks


def _collapse_render_mode(table_refs: list[dict[str, Any]]) -> str:
    modes = {str(item.get("slice_render_mode") or "").strip() for item in table_refs if item}
    modes.discard("")
    if not modes:
        return ""
    if len(modes) == 1:
        return next(iter(modes))
    return "mixed"


def _snapshot_version_token(path_value: Any) -> str:
    path = Path(str(path_value or "").strip())
    if not path.name:
        return "na"
    return re.sub(r"[^0-9A-Za-z_.-]+", "", path.stem) or "na"


def _coerce_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _render_table_image(rows: list[list[str]], *, max_width_px: int) -> tuple[BytesIO, int, int]:
    normalized_rows = _normalize_table_rows(rows)
    font = _load_font(_TABLE_FONT_SIZE)
    probe = ImageDraw.Draw(PILImage.new("RGB", (1, 1), "white"))
    column_units = _fit_table_column_units(normalized_rows, max_width_px=max_width_px)
    column_widths = [units * _TABLE_UNIT_PX + (_TABLE_CELL_PADDING_X * 2) for units in column_units]

    wrapped_rows: list[list[str]] = []
    row_heights: list[int] = []
    for row in normalized_rows:
        wrapped_row: list[str] = []
        row_height = 0
        for col_index, value in enumerate(row):
            content = _wrap_text_to_units(str(value or ""), max(4, column_units[col_index] - 2))
            wrapped_row.append(content)
            _, text_height = _measure_multiline_text(probe, content or " ", font)
            row_height = max(row_height, text_height + (_TABLE_CELL_PADDING_Y * 2))
        wrapped_rows.append(wrapped_row)
        row_heights.append(max(28, row_height))

    image_width = sum(column_widths) + 1
    image_height = sum(row_heights) + 1
    image = PILImage.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)

    y = 0
    for row_index, row in enumerate(wrapped_rows):
        x = 0
        fill_color = "#DCE9F8" if row_index == 0 else "#FFFFFF"
        for col_index, content in enumerate(row):
            width = column_widths[col_index]
            height = row_heights[row_index]
            draw.rectangle([x, y, x + width, y + height], fill=fill_color, outline="#D9E2F0", width=1)
            draw.multiline_text(
                (x + _TABLE_CELL_PADDING_X, y + _TABLE_CELL_PADDING_Y),
                content,
                fill="#1F2937",
                font=font,
                spacing=_TABLE_LINE_SPACING,
            )
            x += width
        y += row_heights[row_index]

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output, image_width, image_height


def _render_text_snapshot(text: str, *, max_width_px: int) -> tuple[BytesIO, int, int]:
    font = _load_font(_TEXT_FONT_SIZE)
    probe = ImageDraw.Draw(PILImage.new("RGB", (1, 1), "white"))
    max_units = max(48, (max_width_px - 48) // _TABLE_UNIT_PX)
    wrapped = _wrap_text_to_units(str(text or "").strip(), max_units)
    text_width, text_height = _measure_multiline_text(probe, wrapped or " ", font)
    image_width = min(max_width_px, max(320, text_width + 40))
    image_height = max(80, text_height + 36)
    image = PILImage.new("RGB", (image_width, image_height), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, image_width - 1, image_height - 1], outline="#D9E2F0", fill="#FFFFFF", width=1)
    draw.multiline_text((20, 18), wrapped, fill="#1F2937", font=font, spacing=6)
    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output, image_width, image_height


def _normalize_table_rows(rows: list[list[str]]) -> list[list[str]]:
    max_cols = max(len(row) for row in rows)
    normalized = [list(row) + [""] * (max_cols - len(row)) for row in rows]
    return [[str(cell or "") for cell in row] for row in normalized]


def _build_source_table_signature(
    *,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
) -> str:
    fragments = [
        fragment
        for fragment, _ in _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
        )
    ]
    seed = "|".join(fragments) or _normalize_match_text(table_text or table_title) or "table"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _build_table_match_fragments(
    *,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
) -> list[tuple[str, int]]:
    seen: set[str] = set()
    fragments: list[tuple[str, int]] = []

    def add(value: str, weight: int) -> None:
        normalized = _normalize_match_text(value)
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        fragments.append((normalized, weight))

    add(table_title, 14)
    raw_lines = [line.strip() for line in str(table_text or "").splitlines() if line.strip()]
    for line in raw_lines[:4]:
        add(line, 6 if line == raw_lines[0] else 3)
    for header in headers[:12]:
        add(header, 4)
    for row in rows[:4]:
        joined = " ".join(str(cell or "").strip() for cell in row if str(cell or "").strip())
        add(joined, 5)
        for cell in row[:8]:
            add(str(cell or ""), 2)
    for note in notes[:2]:
        add(note, 1)
    return fragments


def _normalize_match_text(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", str(value or "").lower())
    return cleaned.strip()


def _render_pdf_page_span_snapshot(
    source_path: Path,
    *,
    page_start: int,
    page_end: int,
    max_width_px: int,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
    before_context_text: str,
    after_context_text: str,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_page_floor_offsets: dict[int, float] | None,
) -> tuple[bytes, dict[str, Any]]:
    if fitz is None:  # pragma: no cover - exercised only when dependency missing
        raise RuntimeError("PyMuPDF is not available")
    with fitz.open(str(source_path)) as document:
        if document.page_count <= 0:
            raise ValueError("source pdf has no pages")
        start_index = max(0, page_start - 1)
        if start_index >= document.page_count:
            raise ValueError("source page is out of range")
        end_index = min(document.page_count - 1, max(start_index, page_end - 1))
        page_indices = list(range(start_index, end_index + 1))
        match_fragments = _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
        )
        before_anchor_candidates = _build_context_anchor_fragments(before_context_text, from_end=True)
        after_anchor_candidates = _build_context_anchor_fragments(after_context_text, from_end=False)
        start_anchor, end_anchor = _find_context_anchor_window(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            before_candidates=before_anchor_candidates,
            after_candidates=after_anchor_candidates,
            detection_cache=source_line_detection_cache,
        )
        crop_plan = _build_pdf_crop_plan(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            match_fragments=match_fragments,
            headers=headers,
            slice_rows=rows,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            detection_cache=source_table_detection_cache,
            source_page_floor_offsets=source_page_floor_offsets,
        )
        if source_page_floor_offsets is not None:
            for page_index, bbox in crop_plan:
                if bbox is None:
                    continue
                page_number = page_index + 1
                source_page_floor_offsets[page_number] = max(
                    float(source_page_floor_offsets.get(page_number, 0.0)),
                    float(bbox[3]),
                )
        render_meta = _summarize_pdf_crop_plan_features(document, crop_plan)
        images = _render_pdf_crop_plan(document, crop_plan, max_width_px=max_width_px)

    return _compose_vertical_images(images, max_width_px=max_width_px), render_meta


def _build_pdf_crop_plan(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    match_fragments: list[tuple[str, int]],
    headers: list[str],
    slice_rows: list[list[str]],
    start_anchor: dict[str, Any] | None,
    end_anchor: dict[str, Any] | None,
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_page_floor_offsets: dict[int, float] | None,
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    effective_start_page = int(start_anchor.get("page_index")) if start_anchor else page_indices[0]
    effective_end_page = int(end_anchor.get("page_index")) if end_anchor else page_indices[-1]
    for page_index in page_indices:
        if page_index < effective_start_page or page_index > effective_end_page:
            continue
        page = document.load_page(page_index)
        candidates = _load_pdf_table_candidates(
            document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=detection_cache,
        )
        chosen = _select_best_pdf_table_candidate(candidates, match_fragments)
        bbox = _compose_page_crop_bbox(
            page,
            chosen=chosen,
            start_anchor=start_anchor if page_index == effective_start_page else None,
            end_anchor=end_anchor if page_index == effective_end_page else None,
            headers=headers,
            slice_rows=slice_rows,
            page_floor=float((source_page_floor_offsets or {}).get(page_index + 1, 0.0)),
        )
        plan.append((page_index, bbox))
    return plan


def _load_pdf_table_candidates(
    document,
    *,
    source_key: str,
    page_index: int,
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    cache_key = (source_key, page_index)
    if detection_cache is not None and cache_key in detection_cache:
        return detection_cache[cache_key]

    page = document.load_page(page_index)
    candidates: list[dict[str, Any]] = []
    try:
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            tables = page.find_tables()
        table_items = list(getattr(tables, "tables", []) or [])
    except Exception:
        table_items = []

    for table in table_items:
        bbox = _expand_pdf_crop_bbox(page.rect, getattr(table, "bbox", None))
        if bbox is None:
            continue
        extracted = getattr(table, "extract", lambda: [])()
        text = _flatten_table_candidate_text(extracted)
        area = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
        row_boxes = [tuple(float(value) for value in row.bbox) for row in getattr(table, "rows", []) if getattr(row, "bbox", None)]
        candidates.append(
            {
                "bbox": bbox,
                "text": text,
                "area": area,
                "rows_text": extracted if isinstance(extracted, list) else [],
                "row_bboxes": row_boxes,
            }
        )

    if detection_cache is not None:
        detection_cache[cache_key] = candidates
    return candidates


def _build_context_anchor_fragments(text: str, *, from_end: bool) -> list[tuple[str, int]]:
    raw_lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not raw_lines:
        return []
    selected = list(reversed(raw_lines[-3:])) if from_end else raw_lines[:3]
    seen: set[str] = set()
    candidates: list[tuple[str, int]] = []
    for index, line in enumerate(selected):
        variants = [line, _strip_anchor_line_prefix(line)]
        label_variant = _extract_anchor_label_prefix(line)
        if label_variant:
            variants.append(label_variant)
        for variant in variants:
            normalized = _normalize_match_text(variant)
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            weight = 18 if variant == label_variant else 12 - index * 3
            candidates.append((normalized, max(3, weight)))
    return candidates


def _strip_anchor_line_prefix(line: str) -> str:
    return re.sub(r"^\s*[0-9一二三四五六七八九十IVXivx\.\-、()（）]+\s*", "", str(line or "")).strip()


def _extract_anchor_label_prefix(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    match = re.match(r"^([\u4e00-\u9fffA-Za-z ]{1,12})\s*[：:]", text)
    if not match:
        return ""
    return match.group(1).strip()


def _find_context_anchor_window(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    before_candidates: list[tuple[str, int]],
    after_candidates: list[tuple[str, int]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    start_anchor = _find_context_anchor(
        document,
        source_key=source_key,
        page_indices=page_indices,
        candidates=before_candidates,
        detection_cache=detection_cache,
        prefer_last=True,
    )

    if start_anchor:
        filtered_pages = [page for page in page_indices if page >= int(start_anchor["page_index"])]
    else:
        filtered_pages = list(page_indices)

    end_anchor = _find_context_anchor(
        document,
        source_key=source_key,
        page_indices=filtered_pages,
        candidates=after_candidates,
        detection_cache=detection_cache,
        min_y_on_first_page=float(start_anchor["bbox"][1]) if start_anchor else None,
        first_page_only=int(start_anchor["page_index"]) if start_anchor else None,
    )
    return start_anchor, end_anchor


def _find_context_anchor(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    candidates: list[tuple[str, int]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    min_y_on_first_page: float | None = None,
    first_page_only: int | None = None,
    prefer_last: bool = False,
) -> dict[str, Any] | None:
    if not candidates or not page_indices:
        return None
    best: dict[str, Any] | None = None
    for page_index in page_indices:
        lines = _load_pdf_page_line_records(
            document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=detection_cache,
        )
        if not lines:
            continue
        page_match = _best_context_anchor_in_page(
            lines,
            candidates,
            min_y=min_y_on_first_page if first_page_only == page_index else None,
            prefer_last=prefer_last,
        )
        if not page_match:
            continue
        candidate = {
            "page_index": page_index,
            "bbox": tuple(page_match["bbox"]),
            "score": int(page_match["score"]),
            "text": str(page_match["text"]),
        }
        if best is None:
            best = candidate
            continue
        if prefer_last:
            if int(candidate["score"]) > int(best["score"]):
                best = candidate
                continue
            if int(candidate["score"]) < int(best["score"]):
                continue
            if int(candidate["page_index"]) > int(best["page_index"]):
                best = candidate
                continue
            if int(candidate["page_index"]) < int(best["page_index"]):
                continue
            if candidate["bbox"][1] > best["bbox"][1]:
                best = candidate
            continue
        if int(candidate["page_index"]) < int(best["page_index"]):
            best = candidate
            continue
        if int(candidate["page_index"]) == int(best["page_index"]):
            if int(candidate["score"]) > int(best["score"]):
                best = candidate
                continue
            if prefer_last:
                if candidate["bbox"][1] > best["bbox"][1]:
                    best = candidate
            elif candidate["bbox"][1] < best["bbox"][1]:
                best = candidate
    return best


def _load_pdf_page_line_records(
    document,
    *,
    source_key: str,
    page_index: int,
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    cache_key = (source_key, page_index)
    if detection_cache is not None and cache_key in detection_cache:
        return detection_cache[cache_key]

    page = document.load_page(page_index)
    try:
        raw = page.get_text("dict")
    except Exception:
        raw = {"blocks": []}

    lines: list[dict[str, Any]] = []
    for block in raw.get("blocks", []):
        if int(block.get("type", 0)) != 0:
            continue
        for line in block.get("lines", []):
            spans = [span for span in line.get("spans", []) if str(span.get("text") or "").strip()]
            if not spans:
                continue
            text = "".join(str(span.get("text") or "") for span in spans).strip()
            if not text:
                continue
            bbox = line.get("bbox") or spans[0].get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                line_bbox = tuple(float(value) for value in bbox[:4])
            except (TypeError, ValueError):
                continue
            lines.append(
                {
                    "text": text,
                    "norm": _normalize_match_text(text),
                    "bbox": line_bbox,
                }
            )

    if detection_cache is not None:
        detection_cache[cache_key] = lines
    return lines


def _best_context_anchor_in_page(
    line_records: list[dict[str, Any]],
    candidates: list[tuple[str, int]],
    *,
    min_y: float | None = None,
    prefer_last: bool = False,
) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    for line in line_records:
        bbox = tuple(line.get("bbox") or ())
        if len(bbox) < 4:
            continue
        if min_y is not None and float(bbox[1]) <= float(min_y):
            continue
        score = 0
        norm = str(line.get("norm") or "")
        if not norm:
            continue
        for fragment, weight in candidates:
            if not fragment:
                continue
            if fragment == norm:
                score += int(weight) + 4
            elif fragment in norm:
                score += int(weight)
        if score <= 0:
            continue
        candidate = {
            "text": str(line.get("text") or ""),
            "bbox": bbox,
            "score": score,
        }
        if best is None or int(candidate["score"]) > int(best["score"]):
            best = candidate
            continue
        if int(candidate["score"]) == int(best["score"]):
            if prefer_last and bbox[1] > best["bbox"][1]:
                best = candidate
            elif not prefer_last and bbox[1] < best["bbox"][1]:
                best = candidate
    return best


def _compose_page_crop_bbox(
    page,
    *,
    chosen: dict[str, Any] | None,
    start_anchor: dict[str, Any] | None,
    end_anchor: dict[str, Any] | None,
    headers: list[str],
    slice_rows: list[list[str]],
    page_floor: float = 0.0,
) -> tuple[float, float, float, float]:
    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    if chosen:
        chosen_bbox = tuple(float(value) for value in chosen["bbox"])
        left = max(0.0, chosen_bbox[0])
        right = min(page_width, chosen_bbox[2])
        top = max(0.0, chosen_bbox[1] - 8.0)
        bottom = min(page_height, chosen_bbox[3] + 8.0)
        row_slice_bbox = _match_row_slice_bbox(chosen, headers=headers, slice_rows=slice_rows)
        applied_row_slice = row_slice_bbox is not None
        if row_slice_bbox is not None:
            top = max(0.0, row_slice_bbox[1] - 8.0)
            bottom = min(page_height, row_slice_bbox[3] + 8.0)
    else:
        left, top, right, bottom = 24.0, 24.0, max(24.0, page_width - 24.0), max(24.0, page_height - 24.0)
        applied_row_slice = False

    if start_anchor and not applied_row_slice:
        anchor_bbox = tuple(float(value) for value in start_anchor["bbox"])
        left = max(0.0, min(left, anchor_bbox[0] - 12.0))
        right = min(page_width, max(right, anchor_bbox[2] + 12.0))
        top = max(0.0, anchor_bbox[1] - 12.0)

    if end_anchor and not applied_row_slice:
        anchor_bbox = tuple(float(value) for value in end_anchor["bbox"])
        candidate_bottom = max(0.0, anchor_bbox[1] - 12.0)
        if candidate_bottom > top + 20.0:
            bottom = min(page_height, candidate_bottom)

    if page_floor > 0.0:
        top = max(top, min(page_height, page_floor + 6.0))

    if bottom <= top + 20.0:
        bottom = min(page_height, top + 20.0)
    if right <= left + 20.0:
        right = min(page_width, left + 20.0)
    return (left, top, right, bottom)


def _match_row_slice_bbox(
    candidate: dict[str, Any],
    *,
    headers: list[str],
    slice_rows: list[list[str]],
) -> tuple[float, float, float, float] | None:
    target_rows = [row for row in slice_rows if isinstance(row, list) and row]
    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if not target_rows or len(candidate_rows) != len(candidate_boxes):
        return None
    if not _should_apply_row_slice_bbox(target_rows):
        return None

    matches = None
    section_number = _shared_row_slice_section_number(target_rows)
    if _should_use_embedded_section_span(headers, section_number):
        section_start = _find_candidate_section_start(candidate_rows, section_number)
        if section_start is not None:
            section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)
            if _should_use_section_span_bbox(
                candidate_rows,
                target_rows,
                section_start=section_start,
                section_stop=section_stop,
            ):
                matches = (section_start, max(section_start, section_stop - 1))

    if matches is None:
        matches = _find_row_slice_match_indices(candidate_rows, target_rows)
    if not matches:
        return None
    start_index, end_index = matches
    first_data_index = _detect_first_data_row_index(candidate_rows)
    if first_data_index is None:
        return None

    header_index = max(0, first_data_index - 1)
    if _looks_like_row_slice_first_item(target_rows[0]) == "1":
        crop_start_index = 0
    else:
        crop_start_index = start_index

    top_box = candidate_boxes[crop_start_index]
    bottom_box = candidate_boxes[end_index]
    if not top_box or not bottom_box:
        return None

    candidate_bbox = tuple(float(value) for value in candidate["bbox"])
    return (
        candidate_bbox[0],
        float(top_box[1]),
        candidate_bbox[2],
        float(bottom_box[3]),
    )


def _find_row_slice_match_indices(candidate_rows: list[list[Any]], target_rows: list[list[str]]) -> tuple[int, int] | None:
    normalized_target_rows = [_normalize_row_cells(row) for row in target_rows]
    if not all(normalized_target_rows):
        return None
    candidate_cells = [_normalize_row_cells(row if isinstance(row, list) else []) for row in candidate_rows]
    shared_prefix = _shared_cell_prefix_length(normalized_target_rows)
    shared_suffix = _shared_cell_suffix_length(normalized_target_rows)
    expected_rows = [
        _build_row_match_tokens(row, shared_prefix=shared_prefix, shared_suffix=shared_suffix)
        for row in normalized_target_rows
    ]
    if not all(expected_rows):
        return None

    section_number = _shared_row_slice_section_number(target_rows)
    section_start = None
    section_stop = len(candidate_cells)
    if section_number:
        section_start = _find_candidate_section_start(candidate_rows, section_number)
        if section_start is not None:
            section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)

    search_start = section_start if section_start is not None else 0
    first_index, first_score = _best_row_match_index(
        candidate_cells,
        expected_rows[0],
        start=search_start,
        stop=section_stop,
    )
    if first_index is None and section_start is not None:
        first_index = section_start
        first_score = _minimum_row_match_score(expected_rows[0])
    if first_index is None or first_score < _minimum_row_match_score(expected_rows[0]):
        return None

    if len(expected_rows) == 1:
        return first_index, first_index

    max_span = max(len(expected_rows) + 2, int(len(expected_rows) * 1.5))
    last_index, last_score = _best_row_match_index(
        candidate_cells,
        expected_rows[-1],
        start=first_index,
        stop=min(section_stop, first_index + max_span),
    )
    if last_index is None and section_start is not None:
        last_index = max(first_index, section_stop - 1)
        last_score = _minimum_row_match_score(expected_rows[-1])
    if last_index is None or last_score < _minimum_row_match_score(expected_rows[-1]) or last_index < first_index:
        return None

    current_index = first_index
    matched_count = 2
    for expected in expected_rows[1:-1]:
        next_index, next_score = _best_row_match_index(
            candidate_cells,
            expected,
            start=current_index,
            stop=min(last_index + 1, section_stop),
        )
        if next_index is None or next_score < _minimum_row_match_score(expected):
            continue
        current_index = next_index
        matched_count += 1

    required_matches = 2 if len(expected_rows) <= 3 else max(2, len(expected_rows) // 2)
    if matched_count < required_matches:
        return None
    return first_index, last_index


def _shared_row_slice_section_number(target_rows: list[list[str]]) -> str:
    if not target_rows:
        return ""
    first_value = _looks_like_row_slice_first_item(target_rows[0])
    if not first_value:
        return ""
    for row in target_rows[1:]:
        value = _looks_like_row_slice_first_item(row)
        if value and value != first_value:
            return ""
    return first_value


def _find_candidate_section_start(candidate_rows: list[list[Any]], section_number: str) -> int | None:
    for index, row in enumerate(candidate_rows):
        if _candidate_row_section_number(row) == section_number:
            return index
    return None


def _find_candidate_section_stop(candidate_rows: list[list[Any]], start_index: int, section_number: str) -> int:
    for index in range(start_index + 1, len(candidate_rows)):
        candidate_number = _candidate_row_section_number(candidate_rows[index])
        if candidate_number and candidate_number != section_number:
            return index
    return len(candidate_rows)


def _candidate_row_section_number(row: list[Any]) -> str:
    if not isinstance(row, list):
        return ""
    for cell in row[:3]:
        raw = str(cell or "").strip()
        if not raw:
            continue
        digits = re.sub(r"[^0-9]+", "", raw)
        if digits and digits == raw:
            return digits
        normalized = _normalize_match_text(raw)
        if normalized.isdigit():
            return normalized
        break
    return ""


def _should_use_section_span_bbox(
    candidate_rows: list[list[Any]],
    target_rows: list[list[str]],
    *,
    section_start: int,
    section_stop: int,
) -> bool:
    if len(target_rows) <= 1 or section_stop - section_start <= 1:
        return False

    # Some appendix tables repeat the section number only on the first visual row.
    # When the extracted slice contains the continuation rows, crop the full section span
    # instead of trying to re-match each continuation row independently.
    continuation_rows = candidate_rows[section_start + 1 : section_stop]
    if not continuation_rows:
        return False

    blank_leading_count = 0
    for row in continuation_rows:
        if not _candidate_row_section_number(row):
            blank_leading_count += 1

    minimum_continuations = max(1, len(target_rows) - 1)
    return blank_leading_count >= minimum_continuations


def _should_use_embedded_section_span(headers: list[str], section_number: str) -> bool:
    if not headers or not section_number:
        return False
    return _normalize_match_text(str(headers[0] or "")) == _normalize_match_text(section_number)


def _should_apply_row_slice_bbox(target_rows: list[list[str]]) -> bool:
    if len(target_rows) <= 1:
        return True
    first_values: list[int] = []
    for row in target_rows:
        value = _looks_like_row_slice_first_item(row)
        if not value:
            return True
        try:
            first_values.append(int(value))
        except ValueError:
            return True
    if len(first_values) < 4:
        return True
    expected = list(range(first_values[0], first_values[0] + len(first_values)))
    return first_values != expected


def _normalize_row_cells(row: list[Any]) -> list[str]:
    tokens: list[str] = []
    for cell in row:
        normalized = _normalize_match_text(str(cell or ""))
        if normalized:
            tokens.append(normalized)
    return tokens


def _shared_cell_prefix_length(rows: list[list[str]]) -> int:
    if len(rows) < 2:
        return 0
    limit = min(len(row) for row in rows)
    count = 0
    for index in range(limit):
        expected = rows[0][index]
        if not expected or any(other[index] != expected for other in rows[1:]):
            break
        count += 1
    return count


def _shared_cell_suffix_length(rows: list[list[str]]) -> int:
    if len(rows) < 2:
        return 0
    limit = min(len(row) for row in rows)
    count = 0
    for offset in range(1, limit + 1):
        expected = rows[0][-offset]
        if not expected or any(other[-offset] != expected for other in rows[1:]):
            break
        count += 1
    return count


def _build_row_match_tokens(row: list[str], *, shared_prefix: int, shared_suffix: int) -> list[str]:
    if not row:
        return []
    start = min(shared_prefix, len(row))
    end = len(row) - shared_suffix if shared_suffix and len(row) - shared_suffix > start else len(row)
    trimmed = row[start:end]
    informative = [token for token in trimmed if len(token) >= 3]
    if informative:
        return informative[:6]
    fallback = [token for token in row if len(token) >= 3]
    if fallback:
        return fallback[:6]
    return row[:3]


def _best_row_match_index(
    candidate_rows: list[list[str]],
    expected_tokens: list[str],
    *,
    start: int = 0,
    stop: int | None = None,
    reverse: bool = False,
) -> tuple[int | None, int]:
    if not expected_tokens or not candidate_rows:
        return None, 0
    upper = len(candidate_rows) if stop is None else min(len(candidate_rows), max(start, stop))
    indices = range(upper - 1, start - 1, -1) if reverse else range(start, upper)
    best_index: int | None = None
    best_score = 0
    for index in indices:
        score = _row_match_score(candidate_rows[index], expected_tokens)
        if score > best_score:
            best_index = index
            best_score = score
    return best_index, best_score


def _row_match_score(actual_tokens: list[str], expected_tokens: list[str]) -> int:
    if not actual_tokens or not expected_tokens:
        return 0
    actual_text = "|".join(actual_tokens)
    score = 0
    matched = 0
    for expected in expected_tokens:
        token_score = _best_token_similarity(actual_tokens, actual_text, expected)
        if token_score > 0:
            matched += 1
        score += token_score
    if matched == len(expected_tokens):
        score += 10
    elif matched >= max(1, len(expected_tokens) - 1):
        score += 4
    return score


def _best_token_similarity(actual_tokens: list[str], actual_text: str, expected: str) -> int:
    if not expected:
        return 0
    if expected in actual_text:
        return 28 if len(expected) >= 8 else 24

    best = 0
    for actual in actual_tokens:
        if not actual:
            continue
        if expected == actual:
            best = max(best, 28)
            continue
        if len(expected) >= 4 and (expected in actual or actual in expected):
            best = max(best, 22)
            continue
        ratio = SequenceMatcher(None, expected, actual).ratio()
        if ratio >= 0.9:
            best = max(best, 20)
        elif ratio >= 0.8:
            best = max(best, 16)
        elif ratio >= 0.7:
            best = max(best, 12)
        elif ratio >= 0.6:
            best = max(best, 8)
    if best == 0 and len(expected) >= 6:
        ratio = SequenceMatcher(None, expected, actual_text).ratio()
        if ratio >= 0.8:
            best = 14
        elif ratio >= 0.7:
            best = 10
        elif ratio >= 0.6:
            best = 6
    return best


def _minimum_row_match_score(expected_tokens: list[str]) -> int:
    token_count = len(expected_tokens)
    if token_count >= 4:
        return 36
    if token_count == 3:
        return 28
    if token_count == 2:
        return 20
    return 14


def _detect_first_data_row_index(candidate_rows: list[list[Any]]) -> int | None:
    for index, row in enumerate(candidate_rows):
        if not isinstance(row, list) or len(row) < 2:
            continue
        first = _normalize_match_text(str(row[0] or ""))
        second = _normalize_match_text(str(row[1] or ""))
        if first.isdigit() and second:
            return index
    return None


def _looks_like_row_slice_first_item(row: list[str]) -> str:
    if not row:
        return ""
    return re.sub(r"[^0-9]+", "", str(row[0] or ""))


def _expand_pdf_crop_bbox(page_rect, bbox: Any, padding: float = 12.0) -> tuple[float, float, float, float] | None:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return None
    try:
        x0, y0, x1, y1 = [float(value) for value in bbox[:4]]
    except (TypeError, ValueError):
        return None
    page_width = float(page_rect.width)
    page_height = float(page_rect.height)
    left = max(0.0, x0 - padding)
    top = max(0.0, y0 - padding)
    right = min(page_width, x1 + padding)
    bottom = min(page_height, y1 + padding)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _flatten_table_candidate_text(rows: Any) -> str:
    if not isinstance(rows, list):
        return ""
    lines: list[str] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        parts = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
        if parts:
            lines.append(" | ".join(parts))
    return "\n".join(lines)


def _select_best_pdf_table_candidate(
    candidates: list[dict[str, Any]],
    match_fragments: list[tuple[str, int]],
) -> dict[str, Any] | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    ranked = sorted(
        candidates,
        key=lambda item: (
            _score_pdf_table_candidate(str(item.get("text") or ""), match_fragments),
            float(item.get("area") or 0.0),
        ),
        reverse=True,
    )
    best = ranked[0]
    best_score = _score_pdf_table_candidate(str(best.get("text") or ""), match_fragments)
    if best_score <= 0:
        return max(candidates, key=lambda item: float(item.get("area") or 0.0))
    return best


def _score_pdf_table_candidate(candidate_text: str, match_fragments: list[tuple[str, int]]) -> int:
    normalized_candidate = _normalize_match_text(candidate_text)
    if not normalized_candidate:
        return 0
    score = 0
    for fragment, weight in match_fragments:
        if fragment and fragment in normalized_candidate:
            score += weight
    return score


def _render_pdf_crop_plan(document, plan: list[tuple[int, tuple[float, float, float, float] | None]], *, max_width_px: int) -> list[PILImage.Image]:
    if not plan:
        return []
    widest_points = max(
        float(document.load_page(page_index).rect.width) if bbox is None else max(1.0, bbox[2] - bbox[0])
        for page_index, bbox in plan
    )
    scale = max(1.0, min(3.0, max_width_px / max(widest_points, 1.0)))

    images: list[PILImage.Image] = []
    for page_index, bbox in plan:
        page = document.load_page(page_index)
        clip = fitz.Rect(*bbox) if bbox is not None else None
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
        images.append(PILImage.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples))
    return images


def _summarize_pdf_crop_plan_features(
    document,
    plan: list[tuple[int, tuple[float, float, float, float] | None]],
) -> dict[str, Any]:
    image_block_count = 0
    page_span_count = len(plan)
    for page_index, bbox in plan:
        page = document.load_page(page_index)
        clip = fitz.Rect(*bbox) if bbox is not None else page.rect
        try:
            raw = page.get_text("dict")
        except Exception:
            raw = {"blocks": []}
        for block in raw.get("blocks", []):
            if int(block.get("type", 0)) != 1:
                continue
            block_bbox = block.get("bbox")
            if not isinstance(block_bbox, (list, tuple)) or len(block_bbox) < 4:
                continue
            try:
                block_rect = fitz.Rect(*[float(value) for value in block_bbox[:4]])
            except (TypeError, ValueError):
                continue
            if clip.intersects(block_rect):
                image_block_count += 1
    return {
        "image_block_count": image_block_count,
        "page_span_count": page_span_count,
    }


def _compose_vertical_images(images: list[PILImage.Image], *, max_width_px: int) -> bytes:
    if not images:
        raise ValueError("no source images to compose")
    canvas_width = max(image.width for image in images)
    gap = 18 if len(images) > 1 else 0
    canvas_height = sum(image.height for image in images) + gap * max(0, len(images) - 1)
    canvas = PILImage.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)

    cursor_y = 0
    for index, image in enumerate(images):
        cursor_x = max(0, (canvas_width - image.width) // 2)
        canvas.paste(image, (cursor_x, cursor_y))
        cursor_y += image.height
        if gap and index < len(images) - 1:
            divider_y = cursor_y + gap // 2
            draw.line((24, divider_y, canvas_width - 24, divider_y), fill="#D9E2F0", width=2)
            cursor_y += gap

    if canvas.width > max_width_px:
        resampling = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS")
        scaled_width = max_width_px
        scaled_height = max(1, int(canvas.height * (scaled_width / canvas.width)))
        canvas = canvas.resize((scaled_width, scaled_height), resample=resampling)

    output = BytesIO()
    canvas.save(output, format="PNG")
    output.seek(0)
    return output.getvalue()


def _fit_table_column_units(rows: list[list[str]], *, max_width_px: int) -> list[int]:
    col_count = len(rows[0])
    desired_units = []
    for col_index in range(col_count):
        max_units = _TABLE_MIN_COLUMN_UNITS
        for row in rows:
            max_units = max(max_units, _display_width_units(str(row[col_index] or "")))
        desired_units.append(max_units + 2)

    total_budget_units = max(
        col_count * _TABLE_MIN_COLUMN_UNITS,
        max(1, (max_width_px - (col_count * _TABLE_CELL_PADDING_X * 2)) // _TABLE_UNIT_PX),
    )
    if sum(desired_units) <= total_budget_units:
        return desired_units

    scale = total_budget_units / sum(desired_units)
    widths = [max(_TABLE_MIN_COLUMN_UNITS, int(units * scale)) for units in desired_units]
    while sum(widths) > total_budget_units:
        widest = max(range(len(widths)), key=widths.__getitem__)
        if widths[widest] <= _TABLE_MIN_COLUMN_UNITS:
            break
        widths[widest] -= 1
    while sum(widths) < total_budget_units:
        widest = max(range(len(widths)), key=lambda idx: desired_units[idx] - widths[idx])
        widths[widest] += 1
    return widths


def _display_width_units(text: str) -> int:
    units = 0
    for ch in str(text or ""):
        units += 2 if ord(ch) > 127 else 1
    return max(units, 1)


def _wrap_text_to_units(text: str, max_units: int) -> str:
    lines = []
    for raw_line in str(text or "").splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        current: list[str] = []
        current_units = 0
        for ch in raw_line:
            ch_units = 2 if ord(ch) > 127 else 1
            if current and current_units + ch_units > max_units:
                lines.append("".join(current).rstrip())
                current = [ch]
                current_units = ch_units
                continue
            current.append(ch)
            current_units += ch_units
        lines.append("".join(current).rstrip())
    return "\n".join(lines).rstrip()


def _measure_multiline_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=_TABLE_LINE_SPACING)
    return max(1, int(bbox[2] - bbox[0])), max(1, int(bbox[3] - bbox[1]))


def _load_font(size: int) -> ImageFont.ImageFont:
    for font_path in _TABLE_FONT_CANDIDATES:
        try:
            if Path(font_path).exists():
                return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()
