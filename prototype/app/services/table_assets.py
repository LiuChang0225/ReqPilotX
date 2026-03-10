from __future__ import annotations

import hashlib
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont


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


def enrich_entries_with_table_assets(
    entries: list[dict[str, Any]],
    *,
    document_id: str,
    snapshot_root: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    enriched: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    for row in entries:
        enriched_row, row_assets = enrich_entry_with_table_assets(
            row,
            document_id=document_id,
            snapshot_root=snapshot_root,
        )
        enriched.append(enriched_row)
        assets.extend(row_assets)
    return enriched, assets


def enrich_entry_with_table_assets(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    copied = dict(row)
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

    for block_index, block in enumerate(blocks):
        if block["kind"] == "paragraph":
            text_value = str(block.get("text") or "").strip()
            if text_value:
                content_blocks.append({"kind": "paragraph", "text": text_value})
            continue

        table_text = str(block.get("text") or "").strip()
        if not table_text:
            continue
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
            "ai_projection": ai_projection,
            "confidence": 0.88 if render_mode == "structured" else 0.42,
            "source_entry_id": str(copied.get("entry_id") or copied.get("req_code") or ""),
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
            "slice_render_key": table_id,
            "slice_render_mode": render_mode,
        }
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
            ref["slice_snapshot_url"] = (
                f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/snapshot"
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
        copied["snapshot_url"] = (
            f"/api/projects/{project_id}/sor-documents/{document_id}/table-assets/{table_id}/snapshot"
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
            table_lines = [lines[index], lines[index + 1]]
            index += 2
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
    if index + 1 >= len(lines):
        return False
    return _looks_like_markdown_table_row(lines[index]) and _looks_like_markdown_table_row(lines[index + 1])


def _extract_structured_table(lines: list[str]) -> tuple[list[str], list[list[str]], str]:
    parsed = _parse_markdown_table(lines)
    if not parsed:
        parsed = _parse_pipe_table(lines)
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
    return [dict(item) for item in value if isinstance(item, dict)]


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
