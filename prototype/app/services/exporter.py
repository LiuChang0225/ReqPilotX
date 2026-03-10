from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
from typing import Any

import xlsxwriter
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from app.services.table_assets import export_blocks_from_requirement, load_snapshot_bytes, render_table_png

_TABLE_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)
_TABLE_FONT_SIZE = 13
_TABLE_CELL_PADDING_X = 12
_TABLE_CELL_PADDING_Y = 8
_TABLE_LINE_SPACING = 4
_TABLE_UNIT_PX = 7
_TABLE_MIN_COLUMN_UNITS = 10
_REQUIREMENT_AREA_TOTAL_WIDTH = 88.0


@dataclass
class _RequirementBlockSplit:
    blocks: list[tuple[str, Any]]
    table_fallback_text_count: int = 0


@dataclass
class _WorkbookFormats:
    header: Any
    meta_fill: Any
    row_even: Any
    row_odd: Any
    paragraph_even: Any
    paragraph_odd: Any
    diff_header: Any
    diff_even: Any
    diff_odd: Any
    meta_label: Any
    meta_value: Any
    decision_formats: dict[str, Any]


def export_compliance_matrix(
    project_name: str,
    customer_name: str,
    requirements: list[dict[str, Any]],
    export_dir: Path,
    *,
    version_label: str = "",
    compare_requirements: list[dict[str, Any]] | None = None,
    compare_version_label: str = "",
) -> tuple[str, str]:
    export_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_version = _slug(version_label) if version_label else ""
    suffix = f"_{safe_version}" if safe_version else ""
    file_name = f"{_slug(project_name)}_compliance_matrix{suffix}_{timestamp}.xlsx"
    file_path = export_dir / file_name

    workbook = xlsxwriter.Workbook(str(file_path), {"in_memory": True})
    formats = _build_formats(workbook)

    ws = workbook.add_worksheet("ComplianceMatrix")
    ws.hide_gridlines(2)
    ws.freeze_panes(3, 0)
    ws.autofilter(2, 0, 2, 4)
    ws.set_column("A:A", 14)
    ws.set_column("B:B", 24)
    ws.set_column("C:C", _REQUIREMENT_AREA_TOTAL_WIDTH)
    ws.set_column("D:D", 18)
    ws.set_column("E:E", 40)
    requirement_area_width_px = _excel_column_width_to_pixels(_REQUIREMENT_AREA_TOTAL_WIDTH)

    ws.write(0, 0, f"Project: {project_name}", formats.meta_fill)
    ws.write(0, 1, f"Customer: {customer_name}", formats.meta_fill)
    ws.set_row(0, 22)

    headers = [
        "Req-ID",
        "Parent Section",
        "Customer Requirement",
        "Final Decision",
        "Schaeffler Comments",
    ]
    ws.write_row(2, 0, headers, formats.header)
    ws.set_row(2, 28)

    current_row = 3
    table_image_count = 0
    table_fallback_text_count = 0
    for block_index, item in enumerate(requirements, start=1):
        current_row, block_table_images, block_table_fallbacks = _append_requirement_block(
            ws=ws,
            row_index=current_row,
            item=item,
            requirement_col=2,
            final_decision_col=3,
            comments_col=4,
            requirement_area_width_px=requirement_area_width_px,
            row_formats=_block_row_formats(formats, block_index),
            decision_format=_decision_format(formats, item.get("final_decision")),
        )
        table_image_count += block_table_images
        table_fallback_text_count += block_table_fallbacks

    meta = workbook.add_worksheet("VersionMeta")
    meta.hide_gridlines(2)
    meta.set_column("A:A", 22)
    meta.set_column("B:B", 52)
    meta_rows = [
        ("Project", project_name),
        ("Customer", customer_name),
        ("ExportedAt", datetime.now().isoformat(timespec="seconds")),
        ("VersionLabel", version_label or ""),
        ("ComparedVersion", compare_version_label or ""),
        ("RequirementCount", len(requirements)),
        (
            "DedupeVersion",
            ",".join(
                sorted(
                    {
                        str(item.get("dedupe_version") or "").strip()
                        for item in requirements
                        if str(item.get("dedupe_version") or "").strip()
                    }
                )
            ),
        ),
        ("TableImageCount", table_image_count),
        ("TableFallbackTextCount", table_fallback_text_count),
    ]
    for row_idx, (label, value) in enumerate(meta_rows):
        meta.write(row_idx, 0, label, formats.meta_label)
        meta.write(row_idx, 1, value, formats.meta_value)

    if compare_requirements:
        _append_diff_sheet(
            workbook=workbook,
            formats=formats,
            current=requirements,
            baseline=compare_requirements,
            baseline_label=compare_version_label or "baseline",
        )

    workbook.close()
    return file_name, str(file_path)


def _build_formats(workbook: xlsxwriter.Workbook) -> _WorkbookFormats:
    base = {
        "border": 1,
        "border_color": "#D9E2F0",
        "text_wrap": True,
        "valign": "top",
        "font_name": "Arial Unicode MS",
        "font_size": 11,
        "font_color": "#1F2937",
    }

    row_even = workbook.add_format({**base, "bg_color": "#F8FAFD"})
    row_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    paragraph_even = workbook.add_format({**base, "bg_color": "#F8FAFD", "align": "left"})
    paragraph_odd = workbook.add_format({**base, "bg_color": "#FFFFFF", "align": "left"})
    header = workbook.add_format(
        {
            **base,
            "bg_color": "#DCE9F8",
            "bold": True,
            "font_color": "#1F2F45",
            "align": "center",
            "valign": "vcenter",
        }
    )
    meta_fill = workbook.add_format(
        {
            **base,
            "bg_color": "#F3F8FF",
            "bold": True,
            "font_color": "#111827",
            "valign": "vcenter",
        }
    )
    meta_label = workbook.add_format(
        {
            **base,
            "bg_color": "#DCE9F8",
            "bold": True,
            "font_color": "#1F2F45",
        }
    )
    meta_value = workbook.add_format(base)
    diff_header = workbook.add_format(
        {
            **base,
            "bg_color": "#DCE9F8",
            "bold": True,
            "align": "center",
            "valign": "vcenter",
        }
    )
    diff_even = workbook.add_format({**base, "bg_color": "#F8FAFD"})
    diff_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    decision_formats = {
        token: workbook.add_format(
            {
                **base,
                "bg_color": color,
            }
        )
        for token, color in {
            "ACCEPT": "#DEF7E8",
            "REJECT": "#FDE5E8",
            "DEVIATION_ACCEPT": "#FFF1D6",
            "NEED_CLARIFICATION": "#E8F1FF",
            "DEFAULT": "#FFFFFF",
        }.items()
    }
    return _WorkbookFormats(
        header=header,
        meta_fill=meta_fill,
        row_even=row_even,
        row_odd=row_odd,
        paragraph_even=paragraph_even,
        paragraph_odd=paragraph_odd,
        diff_header=diff_header,
        diff_even=diff_even,
        diff_odd=diff_odd,
        meta_label=meta_label,
        meta_value=meta_value,
        decision_formats=decision_formats,
    )


def _block_row_formats(formats: _WorkbookFormats, block_index: int) -> dict[str, Any]:
    even = block_index % 2 == 0
    return {
        "meta": formats.row_even if even else formats.row_odd,
        "paragraph": formats.paragraph_even if even else formats.paragraph_odd,
    }


def _decision_format(formats: _WorkbookFormats, value: Any) -> Any:
    token = str(value or "").strip().upper() or "DEFAULT"
    return formats.decision_formats.get(token, formats.decision_formats["DEFAULT"])


def _append_requirement_block(
    *,
    ws,
    row_index: int,
    item: dict[str, Any],
    requirement_col: int,
    final_decision_col: int,
    comments_col: int,
    requirement_area_width_px: int,
    row_formats: dict[str, Any],
    decision_format: Any,
) -> tuple[int, int, int]:
    split = _split_requirement_blocks(_display_requirement_text(item))
    blocks = export_blocks_from_requirement(item) or split.blocks or [("paragraph", "")]

    block_rows = len(blocks)
    start_row = row_index
    end_row = row_index + block_rows - 1

    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=0,
        value=item.get("display_req_code") or item.get("req_code") or "",
        cell_format=row_formats["meta"],
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=1,
        value=_display_parent_section_label(item),
        cell_format=row_formats["meta"],
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=final_decision_col,
        value=item.get("final_decision") or "",
        cell_format=decision_format,
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=comments_col,
        value=item.get("engineer_reason") or "",
        cell_format=row_formats["meta"],
    )

    cursor = start_row
    table_image_count = 0
    table_fallback_text_count = split.table_fallback_text_count
    for kind, payload in blocks:
        if kind == "paragraph":
            text = str(payload or "")
            ws.write(cursor, requirement_col, text, row_formats["paragraph"])
            ws.set_row(cursor, _estimate_paragraph_row_height(text))
            cursor += 1
            continue

        if kind == "table_ref":
            ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
            image_buffer, image_width_px, image_height_px = _table_ref_snapshot(payload, requirement_area_width_px)
            if image_buffer is None:
                fallback_text = _table_ref_to_text(payload)
                ws.write(cursor, requirement_col, fallback_text, row_formats["paragraph"])
                ws.set_row(cursor, _estimate_paragraph_row_height(fallback_text))
                table_fallback_text_count += 1
            else:
                x_scale = min(1.0, requirement_area_width_px / max(image_width_px, 1))
                y_scale = x_scale
                ws.insert_image(
                    cursor,
                    requirement_col,
                    "table.png",
                    {
                        "image_data": image_buffer,
                        "object_position": 1,
                        "x_scale": x_scale,
                        "y_scale": y_scale,
                        "x_offset": 2,
                        "y_offset": 2,
                    },
                )
                ws.set_row(cursor, _pixels_to_row_height_points(int(image_height_px * y_scale) + 8))
                table_image_count += 1
            cursor += 1
            continue

        ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
        try:
            image_buffer, image_width_px, image_height_px = _render_table_image(payload, max_width_px=requirement_area_width_px)
        except Exception:
            ws.write(cursor, requirement_col, _table_rows_to_text(payload), row_formats["paragraph"])
            ws.set_row(cursor, _estimate_paragraph_row_height(_table_rows_to_text(payload)))
            table_fallback_text_count += 1
        else:
            x_scale = min(1.0, requirement_area_width_px / max(image_width_px, 1))
            y_scale = x_scale
            ws.insert_image(
                cursor,
                requirement_col,
                "table.png",
                {
                    "image_data": image_buffer,
                    "object_position": 1,
                    "x_scale": x_scale,
                    "y_scale": y_scale,
                    "x_offset": 2,
                    "y_offset": 2,
                },
            )
            ws.set_row(cursor, _pixels_to_row_height_points(int(image_height_px * y_scale) + 8))
            table_image_count += 1
        cursor += 1

    return end_row + 1, table_image_count, table_fallback_text_count


def _table_ref_snapshot(payload: dict[str, Any], max_width_px: int) -> tuple[BytesIO | None, int, int]:
    snapshot_path = str(payload.get("slice_snapshot_path") or "").strip()
    if snapshot_path:
        try:
            raw = load_snapshot_bytes(snapshot_path)
            buffer = BytesIO(raw)
            image = PILImage.open(BytesIO(raw))
            return buffer, int(image.width), int(image.height)
        except Exception:
            pass

    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers:
        try:
            raw = render_table_png(headers, rows, max_width_px=max_width_px)
            buffer = BytesIO(raw)
            image = PILImage.open(BytesIO(raw))
            return buffer, int(image.width), int(image.height)
        except Exception:
            return None, 0, 0
    return None, 0, 0


def _table_ref_to_text(payload: dict[str, Any]) -> str:
    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers:
        return _table_rows_to_text([headers, *rows])
    return str(payload.get("slice_ai_projection") or "").strip()


def _write_vertical_block(ws, *, start_row: int, end_row: int, col: int, value: Any, cell_format: Any) -> None:
    if end_row > start_row:
        ws.merge_range(start_row, col, end_row, col, value, cell_format)
    else:
        ws.write(start_row, col, value, cell_format)


def _estimate_paragraph_row_height(text: str) -> float:
    content = str(text or "")
    if not content.strip():
        return 22.0
    logical_lines = 0
    for line in content.splitlines() or [""]:
        units = _display_width_units(line)
        logical_lines += max(1, (units // 78) + (1 if units % 78 else 0))
    return max(22.0, min(180.0, 18.0 * logical_lines + 6.0))


def _split_requirement_blocks(text: str) -> _RequirementBlockSplit:
    lines = str(text or "").replace("\r\n", "\n").split("\n")
    blocks: list[tuple[str, Any]] = []
    paragraph: list[str] = []
    table_fallback_text_count = 0

    def flush_paragraph() -> None:
        if not paragraph:
            return
        merged = "\n".join(part for part in paragraph if str(part).strip()).strip()
        paragraph.clear()
        if merged:
            blocks.append(("paragraph", merged))

    index = 0
    while index < len(lines):
        if _starts_markdown_table(lines, index):
            flush_paragraph()
            markdown_mode = _is_markdown_separator_row(_parse_markdown_cells(lines[index + 1]))
            table_lines = [lines[index], lines[index + 1]]
            index += 2
            while index < len(lines) and _looks_like_markdown_table_row(lines[index]):
                table_lines.append(lines[index])
                index += 1
            parsed = _parse_markdown_table(table_lines)
            if not parsed and not markdown_mode:
                parsed = _parse_pipe_table(table_lines)
            if parsed:
                blocks.append(("table", parsed))
                continue
            paragraph.extend(table_lines)
            table_fallback_text_count += 1
            continue

        paragraph.append(lines[index])
        index += 1

    flush_paragraph()
    return _RequirementBlockSplit(blocks=blocks, table_fallback_text_count=table_fallback_text_count)


def _starts_markdown_table(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines):
        return False
    return _looks_like_markdown_table_row(lines[index]) and _looks_like_markdown_table_row(lines[index + 1])


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

    column_counts = Counter(len(row) for row in rows if len(row) >= 2)
    if not column_counts:
        return []
    expected_columns, expected_frequency = max(column_counts.items(), key=lambda item: (item[1], item[0]))
    if expected_frequency < 2:
        return []

    normalized_rows: list[list[str]] = []
    mismatched_rows = 0
    for row in rows:
        if len(row) == expected_columns:
            normalized_rows.append(row)
            continue
        if abs(len(row) - expected_columns) <= 1 and expected_columns >= 3:
            normalized_rows.append((row + [""] * expected_columns)[:expected_columns])
            mismatched_rows += 1
            continue
        mismatched_rows += 1

    if len(normalized_rows) < 2:
        return []
    if mismatched_rows > max(1, len(rows) // 2):
        return []
    return normalized_rows


def _looks_like_markdown_table_row(line: str) -> bool:
    text = str(line or "").strip()
    return text.startswith("|") and len(_parse_markdown_cells(text)) >= 2


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


def _display_requirement_text(item: dict[str, Any]) -> str:
    return str(item.get("raw_text") or item.get("display_text") or item.get("requirement_text") or "").strip()


def _excel_column_width_to_pixels(width: float) -> int:
    return int(width * 7 + 5)


def _pixels_to_row_height_points(pixels: int) -> float:
    return round(max(20, pixels) * 0.75, 2)


def _table_rows_to_text(rows: list[list[str]]) -> str:
    if not rows:
        return ""
    header = "| " + " | ".join(str(cell or "") for cell in rows[0]) + " |"
    separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
    body = ["| " + " | ".join(str(cell or "") for cell in row) + " |" for row in rows[1:]]
    return "\n".join([header, separator, *body]).strip()


@lru_cache(maxsize=8)
def _load_table_font(size: int) -> ImageFont.ImageFont:
    for font_path in _TABLE_FONT_CANDIDATES:
        try:
            if Path(font_path).exists():
                return ImageFont.truetype(font_path, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _render_table_image(rows: list[list[str]], *, max_width_px: int) -> tuple[BytesIO, int, int]:
    normalized_rows = _normalize_table_rows(rows)
    font = _load_table_font(_TABLE_FONT_SIZE)
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
            draw.rectangle(
                [x, y, x + width, y + height],
                fill=fill_color,
                outline="#D9E2F0",
                width=1,
            )
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
    return "\n".join(lines)


def _measure_multiline_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=_TABLE_LINE_SPACING)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _display_parent_section_label(item: dict[str, Any]) -> str:
    return (
        _build_section_label(
            label=item.get("parent_section_label"),
            code=item.get("parent_section_code"),
            title=item.get("parent_section_title"),
        )
        or _build_section_label(
            label=item.get("section_label"),
            code=item.get("section_code"),
            title=item.get("section_title"),
        )
        or _section_label_from_anchor(item.get("source_anchor"))
        or str(item.get("heading_text") or "").strip()
    )


def _build_section_label(*, label: Any, code: Any, title: Any) -> str:
    direct_label = str(label or "").strip()
    if direct_label:
        return direct_label
    code_text = str(code or "").strip()
    title_text = str(title or "").strip()
    return " ".join(part for part in (code_text, title_text) if part)


def _section_label_from_anchor(anchor: Any) -> str:
    text = str(anchor or "")
    marker = "Section "
    index = text.lower().find(marker.lower())
    if index < 0:
        return ""
    fragment = text[index + len(marker) :]
    return fragment.split("|", 1)[0].strip()


def _append_diff_sheet(
    *,
    workbook: xlsxwriter.Workbook,
    formats: _WorkbookFormats,
    current: list[dict[str, Any]],
    baseline: list[dict[str, Any]],
    baseline_label: str,
) -> None:
    sheet = workbook.add_worksheet("DiffVsBaseline")
    sheet.hide_gridlines(2)
    headers = [
        "Req-ID",
        "Diff Type",
        "Current Parent Section",
        f"Baseline Parent Section ({baseline_label})",
        "Current Final Decision",
        f"Baseline Final Decision ({baseline_label})",
        "Current Text",
        f"Baseline Text ({baseline_label})",
    ]
    sheet.write_row(0, 0, headers, formats.diff_header)
    sheet.freeze_panes(1, 0)
    sheet.autofilter(0, 0, 0, 7)

    current_rows = sorted(current, key=_diff_sort_key)
    baseline_rows = sorted(baseline, key=_diff_sort_key)
    current_map = {str(row.get("req_code") or ""): row for row in current_rows if str(row.get("req_code") or "")}
    base_map = {str(row.get("req_code") or ""): row for row in baseline_rows if str(row.get("req_code") or "")}

    ordered_codes: list[str] = []
    seen_codes: set[str] = set()
    for row in current_rows + baseline_rows:
        req_code = str(row.get("req_code") or "")
        if not req_code or req_code in seen_codes:
            continue
        seen_codes.add(req_code)
        ordered_codes.append(req_code)

    row_idx = 1
    for req_code in ordered_codes:
        curr = current_map.get(req_code)
        base = base_map.get(req_code)
        if curr is None and base is not None:
            diff_type = "REMOVED"
        elif curr is not None and base is None:
            diff_type = "ADDED"
        else:
            curr_text = _display_requirement_text(curr)
            base_text = _display_requirement_text(base)
            curr_parent = _display_parent_section_label(curr)
            base_parent = _display_parent_section_label(base)
            curr_decision = str(curr.get("final_decision") or "")
            base_decision = str(base.get("final_decision") or "")
            if curr_text != base_text:
                diff_type = "TEXT_CHANGED"
            elif curr_decision != base_decision:
                diff_type = "DECISION_CHANGED"
            elif curr_parent != base_parent:
                diff_type = "PARENT_SECTION_CHANGED"
            else:
                continue

        curr = curr or {}
        base = base or {}
        row_format = formats.diff_even if row_idx % 2 == 0 else formats.diff_odd
        values = [
            curr.get("display_req_code") or base.get("display_req_code") or req_code,
            diff_type,
            _display_parent_section_label(curr),
            _display_parent_section_label(base),
            curr.get("final_decision", ""),
            base.get("final_decision", ""),
            _truncate_text(_display_requirement_text(curr), 180),
            _truncate_text(_display_requirement_text(base), 180),
        ]
        for col_idx, value in enumerate(values):
            sheet.write(row_idx, col_idx, value, row_format)
        row_idx += 1

    for col_name, width in {
        "A:A": 14,
        "B:B": 26,
        "C:C": 28,
        "D:D": 32,
        "E:E": 22,
        "F:F": 30,
        "G:G": 60,
        "H:H": 60,
    }.items():
        sheet.set_column(col_name, width)


def _diff_sort_key(row: dict[str, Any]) -> tuple[int, str, str]:
    try:
        display_index = int(row.get("display_sort_index") or 0)
    except (TypeError, ValueError):
        display_index = 0
    return (
        display_index if display_index > 0 else 10**9,
        str(row.get("display_req_code") or ""),
        str(row.get("req_code") or ""),
    )


def _truncate_text(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _slug(value: str) -> str:
    return "".join(ch.lower() if ch.isalnum() else "_" for ch in value).strip("_") or "project"
