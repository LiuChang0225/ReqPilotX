from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from io import BytesIO
from pathlib import Path
import re
import sys
from typing import Any

import xlsxwriter
from PIL import Image as PILImage
from PIL import ImageDraw, ImageFont

from app.services.table_assets import load_snapshot_bytes, load_table_font

_TABLE_FONT_SIZE = 13
_TABLE_CELL_PADDING_X = 12
_TABLE_CELL_PADDING_Y = 8
_TABLE_LINE_SPACING = 4
_TABLE_UNIT_PX = 7
_TABLE_MIN_COLUMN_UNITS = 10
_WORKBOOK_FONT_SIZE = 11
_WORKBOOK_TEXT_PADDING_X = 10
_WORKBOOK_TEXT_PADDING_Y = 6
_DEFAULT_ROW_HEIGHT_POINTS = 22.0
_MAX_ROW_HEIGHT_POINTS = 409.5
_MAX_ROW_HEIGHT_PX = int(_MAX_ROW_HEIGHT_POINTS / 0.75)
_MAX_IMAGE_ROW_RENDER_HEIGHT_PX = max(120, _MAX_ROW_HEIGHT_PX - 12)
_MIN_SINGLE_IMAGE_SCALE = 0.55
_WORKBOOK_TEXT_LINE_HEIGHT_POINTS = 18.0
_WORKBOOK_TEXT_ROW_EXTRA_POINTS = 8.0
_CUSTOMER_REQUIREMENT_COLUMN_WIDTH = 130.0
_REQUIREMENT_FIXED_COLUMN_WIDTHS = {
    "A": 14.0,
    "B": 24.0,
    "D": 22.0,
    "E": 18.0,
    "F": 40.0,
}
_BRAND_PRIMARY = "#00893D"
_BRAND_SECONDARY = "#005E46"
_BRAND_TEXT = "#343434"
_BRAND_TEXT_SECONDARY = "#646464"
_BRAND_BORDER = "#D0D0D0"
_BRAND_BACKGROUND = "#F6F6F6"
_BRAND_HEADER_BACKGROUND = "#EBEBEB"
_BRAND_ROW_ALT = "#FCFBFB"
_BRAND_HEADER_TEXT = "#FFFFFF"
_BRAND_LOGO_PATH_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "static" / "schaeffler-logo.png",
    Path.home() / ".codex" / "skills" / "schaeffler-frontend-generator" / "assets" / "logos" / "schaeffler-logo.png",
)


@dataclass
class _RequirementBlockSplit:
    blocks: list[tuple[str, Any]]
    table_fallback_text_count: int = 0


@dataclass
class _PreparedRequirementRow:
    kind: str
    text: str = ""
    row_height: float = _DEFAULT_ROW_HEIGHT_POINTS
    image_buffer: BytesIO | None = None
    x_scale: float = 1.0
    y_scale: float = 1.0


@dataclass
class _WorkbookFormats:
    header: Any
    meta_fill: Any
    brand_bar: Any
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
    source_file_name: str = "",
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
    ws.autofilter(2, 0, 2, 5)
    requirement_area_total_width = _default_requirement_area_width()
    ws.set_column("A:A", _REQUIREMENT_FIXED_COLUMN_WIDTHS["A"])
    ws.set_column("B:B", _REQUIREMENT_FIXED_COLUMN_WIDTHS["B"])
    ws.set_column("C:C", requirement_area_total_width)
    ws.set_column("D:D", _REQUIREMENT_FIXED_COLUMN_WIDTHS["D"])
    ws.set_column("E:E", _REQUIREMENT_FIXED_COLUMN_WIDTHS["E"])
    ws.set_column("F:F", _REQUIREMENT_FIXED_COLUMN_WIDTHS["F"])
    requirement_area_width_px = _excel_column_width_to_pixels(requirement_area_total_width)
    column_widths_px = {
        0: _excel_column_width_to_pixels(_REQUIREMENT_FIXED_COLUMN_WIDTHS["A"]),
        1: _excel_column_width_to_pixels(_REQUIREMENT_FIXED_COLUMN_WIDTHS["B"]),
        2: requirement_area_width_px,
        3: _excel_column_width_to_pixels(_REQUIREMENT_FIXED_COLUMN_WIDTHS["D"]),
        4: _excel_column_width_to_pixels(_REQUIREMENT_FIXED_COLUMN_WIDTHS["E"]),
        5: _excel_column_width_to_pixels(_REQUIREMENT_FIXED_COLUMN_WIDTHS["F"]),
    }

    _write_brand_header(
        ws,
        formats=formats,
        project_name=project_name,
        customer_name=customer_name,
        source_file_name=source_file_name,
    )

    headers = [
        "Req-ID",
        "Section",
        "Customer Requirement",
        "AI Assessment",
        "Final Decision",
        "Schaeffler Comments",
    ]
    ws.write_row(2, 0, headers, formats.header)
    ws.set_row(2, 28)

    current_row = 3
    table_image_count = 0
    table_fallback_text_count = 0
    table_export_warnings: list[str] = []
    for block_index, item in enumerate(requirements, start=1):
        current_row, block_table_images, block_table_fallbacks, block_warnings = _append_requirement_block(
            ws=ws,
            row_index=current_row,
            item=item,
            requirement_col=2,
            ai_assessment_col=3,
            final_decision_col=4,
            comments_col=5,
            requirement_area_width_px=requirement_area_width_px,
            column_widths_px=column_widths_px,
            row_formats=_block_row_formats(formats, block_index),
            ai_assessment_format=_decision_format(formats, _ai_decision_label(item)),
            decision_format=_decision_format(formats, item.get("final_decision") or _ai_decision_label(item)),
        )
        table_image_count += block_table_images
        table_fallback_text_count += block_table_fallbacks
        table_export_warnings.extend(block_warnings)

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
        ("TableExportWarningCount", len(table_export_warnings)),
        ("TableExportWarnings", "\n".join(table_export_warnings)),
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


def _workbook_font_name() -> str:
    """Return the best CJK-capable font name for the current platform."""
    if sys.platform == "win32":
        return "Microsoft YaHei"
    if sys.platform == "darwin":
        return "Arial Unicode MS"
    return "Noto Sans CJK SC"


def _build_formats(workbook: xlsxwriter.Workbook) -> _WorkbookFormats:
    base = {
        "border": 1,
        "border_color": _BRAND_BORDER,
        "text_wrap": True,
        "valign": "top",
        "font_name": _workbook_font_name(),
        "font_size": _WORKBOOK_FONT_SIZE,
        "font_color": _BRAND_TEXT,
    }

    row_even = workbook.add_format({**base, "bg_color": _BRAND_ROW_ALT})
    row_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    paragraph_even = workbook.add_format({**base, "bg_color": _BRAND_ROW_ALT, "align": "left"})
    paragraph_odd = workbook.add_format({**base, "bg_color": "#FFFFFF", "align": "left"})
    header = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_PRIMARY,
            "bold": True,
            "font_color": _BRAND_HEADER_TEXT,
            "align": "center",
            "valign": "vcenter",
        }
    )
    meta_fill = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_BACKGROUND,
            "bold": True,
            "font_color": _BRAND_TEXT,
            "valign": "vcenter",
        }
    )
    brand_bar = workbook.add_format({"bg_color": _BRAND_SECONDARY, "border": 0})
    meta_label = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_HEADER_BACKGROUND,
            "bold": True,
            "font_color": _BRAND_TEXT,
        }
    )
    meta_value = workbook.add_format(base)
    diff_header = workbook.add_format(
        {
            **base,
            "bg_color": _BRAND_PRIMARY,
            "bold": True,
            "font_color": _BRAND_HEADER_TEXT,
            "align": "center",
            "valign": "vcenter",
        }
    )
    diff_even = workbook.add_format({**base, "bg_color": _BRAND_ROW_ALT})
    diff_odd = workbook.add_format({**base, "bg_color": "#FFFFFF"})
    decision_formats = {
        token: workbook.add_format(
            {
                **base,
                "bg_color": color,
            }
        )
        for token, color in {
            "ACCEPT": "#E8F5E9",
            "REJECT": "#FFEBEE",
            "DEVIATION_ACCEPT": "#FFF3E0",
            "NEED_CLARIFICATION": "#E2EBF8",
            "INFO": "#F3E5F5",
            "DEFAULT": "#FFFFFF",
        }.items()
    }
    return _WorkbookFormats(
        header=header,
        meta_fill=meta_fill,
        brand_bar=brand_bar,
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
    ai_assessment_col: int,
    final_decision_col: int,
    comments_col: int,
    requirement_area_width_px: int,
    column_widths_px: dict[int, int],
    row_formats: dict[str, Any],
    ai_assessment_format: Any,
    decision_format: Any,
) -> tuple[int, int, int, list[str]]:
    split = _split_requirement_blocks(_display_requirement_text(item))
    blocks = _build_export_blocks(item, split) or [("paragraph", "")]
    prepared_rows, table_image_count, table_fallback_text_count, warnings = _prepare_requirement_rows(
        blocks=blocks,
        item=item,
        requirement_area_width_px=requirement_area_width_px,
        fallback_text_count=split.table_fallback_text_count,
    )

    block_rows = len(prepared_rows)
    start_row = row_index
    end_row = row_index + block_rows - 1

    ai_decision_value = _ai_decision_label(item)
    final_decision_value = item.get("final_decision") or ai_decision_value

    _ensure_required_block_height(
        prepared_rows,
        required_total_height=max(
            _total_prepared_row_height(prepared_rows),
            _estimate_total_text_height(
                item.get("display_req_code") or item.get("req_code") or "",
                column_widths_px[0],
            ),
            _estimate_total_text_height(_display_parent_section_label(item), column_widths_px[1]),
            _estimate_total_text_height(ai_decision_value, column_widths_px[ai_assessment_col]),
            _estimate_total_text_height(final_decision_value, column_widths_px[final_decision_col]),
            _estimate_total_text_height(item.get("engineer_reason") or "", column_widths_px[comments_col]),
        ),
    )

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
        col=ai_assessment_col,
        value=ai_decision_value,
        cell_format=ai_assessment_format,
    )
    _write_vertical_block(
        ws,
        start_row=start_row,
        end_row=end_row,
        col=final_decision_col,
        value=final_decision_value,
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
    for prepared in prepared_rows:
        ws.set_row(cursor, prepared.row_height)
        if prepared.kind == "paragraph":
            ws.write(cursor, requirement_col, prepared.text, row_formats["paragraph"])
        elif prepared.kind == "image" and prepared.image_buffer is not None:
            ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
            ws.insert_image(
                cursor,
                requirement_col,
                "table.png",
                {
                    "image_data": prepared.image_buffer,
                    "object_position": 2,
                    "x_scale": prepared.x_scale,
                    "y_scale": prepared.y_scale,
                    "x_offset": 2,
                    "y_offset": 2,
                },
            )
        else:
            ws.write_blank(cursor, requirement_col, None, row_formats["paragraph"])
        cursor += 1

    return end_row + 1, table_image_count, table_fallback_text_count, warnings


def _write_brand_header(
    ws,
    *,
    formats: _WorkbookFormats,
    project_name: str,
    customer_name: str,
    source_file_name: str,
) -> None:
    for col in range(3):
        ws.write_blank(0, col, None, formats.meta_fill)
    ws.merge_range(0, 3, 0, 5, "", formats.meta_fill)
    ws.write(0, 0, f"Project: {project_name}", formats.meta_fill)
    ws.write(0, 1, f"Customer: {customer_name}", formats.meta_fill)
    if source_file_name:
        ws.write(0, 2, f"File: {source_file_name}", formats.meta_fill)
    ws.merge_range(1, 0, 1, 5, "", formats.brand_bar)
    ws.set_row(0, 34)
    ws.set_row(1, 7)
    _insert_brand_logo(ws)


@lru_cache(maxsize=1)
def _load_brand_logo_bytes() -> tuple[bytes, int, int] | None:
    for path in _BRAND_LOGO_PATH_CANDIDATES:
        try:
            if not path.exists():
                continue
            raw = path.read_bytes()
            with PILImage.open(BytesIO(raw)) as image:
                return raw, int(image.width), int(image.height)
        except Exception:
            continue
    return None


def _insert_brand_logo(ws) -> None:
    logo_payload = _load_brand_logo_bytes()
    if logo_payload is None:
        return
    raw, width, _ = logo_payload
    target_width_px = 158
    scale = min(1.0, target_width_px / max(width, 1))
    ws.insert_image(
        0,
        3,
        "schaeffler-logo.png",
        {
            "image_data": BytesIO(raw),
            "object_position": 2,
            "x_scale": scale,
            "y_scale": scale,
            "x_offset": 10,
            "y_offset": 6,
        },
    )


def _build_export_blocks(item: dict[str, Any], split: _RequirementBlockSplit) -> list[tuple[str, Any]]:
    table_refs = [
        dict(ref)
        for ref in item.get("table_refs") or []
        if isinstance(ref, dict) and str(ref.get("table_id") or "").strip()
    ]
    table_ref_map = {
        str(ref.get("table_id") or "").strip(): ref
        for ref in table_refs
    }

    content_blocks = item.get("content_blocks")
    if isinstance(content_blocks, list):
        blocks: list[tuple[str, Any]] = []
        used_table_ids: set[str] = set()
        for block_index, block in enumerate(content_blocks):
            if not isinstance(block, dict):
                continue
            kind = str(block.get("kind") or "").strip().lower()
            if kind == "paragraph":
                text = str(block.get("text") or "")
                if text.strip():
                    blocks.append(("paragraph", text))
                continue
            if kind == "table":
                table_id = str(block.get("table_id") or "").strip()
                ref = table_ref_map.get(table_id)
                if ref:
                    used_table_ids.add(table_id)
                    blocks.append(("table_ref", ref))
                continue
            if kind in {"image", "snapshot", "visual"}:
                ref = _snapshot_block_to_table_ref(block, block_index=block_index)
                if ref:
                    used_table_ids.add(str(ref.get("table_id") or ""))
                    blocks.append(("table_ref", ref))
        if blocks:
            for ref in table_refs:
                table_id = str(ref.get("table_id") or "").strip()
                if table_id and table_id not in used_table_ids:
                    blocks.append(("table_ref", ref))
            return blocks

    if table_refs:
        if split.blocks:
            return _merge_split_blocks_with_table_refs(split.blocks, table_refs)
        text = _display_requirement_text(item)
        blocks = [("paragraph", text)] if text else []
        blocks.extend(("table_ref", ref) for ref in table_refs)
        return blocks

    return split.blocks


def _snapshot_block_to_table_ref(block: dict[str, Any], *, block_index: int) -> dict[str, Any] | None:
    slice_snapshot_path = str(
        block.get("slice_snapshot_path")
        or block.get("snapshot_path")
        or block.get("image_path")
        or ""
    ).strip()
    source_snapshot_path = str(block.get("source_snapshot_path") or slice_snapshot_path).strip()
    if not slice_snapshot_path and not source_snapshot_path:
        return None
    table_id = str(block.get("table_id") or block.get("snapshot_id") or f"snapshot_block_{block_index + 1}").strip()
    title = str(block.get("title") or block.get("text") or block.get("caption") or table_id).strip()
    export_variant = str(block.get("export_variant") or "").strip().lower()
    return {
        "table_id": table_id,
        "table_title": title,
        "row_range": [],
        "row_ids": [],
        "inherited_headers": [],
        "slice_rows": [],
        "slice_ai_projection": title,
        "slice_render_mode": "snapshot_only",
        "slice_snapshot_path": slice_snapshot_path or source_snapshot_path,
        "source_snapshot_path": source_snapshot_path or slice_snapshot_path,
        "export_variant": export_variant or ("source" if source_snapshot_path else "slice"),
    }


def _merge_split_blocks_with_table_refs(
    split_blocks: list[tuple[str, Any]],
    table_refs: list[dict[str, Any]],
) -> list[tuple[str, Any]]:
    merged: list[tuple[str, Any]] = []
    ref_iter = iter(table_refs)
    for kind, payload in split_blocks:
        if kind == "paragraph":
            merged.append((kind, payload))
            continue
        if kind == "table":
            ref = next(ref_iter, None)
            if ref is not None:
                merged.append(("table_ref", ref))
            else:
                merged.append((kind, payload))
    merged.extend(("table_ref", ref) for ref in ref_iter)
    return merged


def _prepare_requirement_rows(
    *,
    blocks: list[tuple[str, Any]],
    item: dict[str, Any],
    requirement_area_width_px: int,
    fallback_text_count: int,
) -> tuple[list[_PreparedRequirementRow], int, int, list[str]]:
    prepared_rows: list[_PreparedRequirementRow] = []
    table_image_count = 0
    table_fallback_text_count = fallback_text_count
    warnings: list[str] = []

    for kind, payload in blocks:
        if kind == "paragraph":
            prepared_rows.extend(_prepare_paragraph_rows(str(payload or ""), requirement_area_width_px))
            continue

        if kind == "table_ref":
            image_buffer, image_width_px, image_height_px, warning_text = _table_ref_snapshot(
                payload,
                requirement_area_width_px,
            )
            if warning_text:
                warnings.append(
                    f"{str(item.get('display_req_code') or item.get('req_code') or 'UNKNOWN')} / {str(payload.get('table_id') or '')}: {warning_text}"
                )
            if image_buffer is None:
                fallback_text = _table_ref_to_text(payload)
                prepared_rows.extend(_prepare_paragraph_rows(fallback_text, requirement_area_width_px))
                table_fallback_text_count += 1
            else:
                prepared_rows.extend(
                    _prepare_image_rows(
                        image_buffer=image_buffer,
                        image_width_px=image_width_px,
                        image_height_px=image_height_px,
                        max_width_px=requirement_area_width_px,
                    )
                )
                table_image_count += 1
            continue

        try:
            image_buffer, image_width_px, image_height_px = _render_table_image(payload, max_width_px=requirement_area_width_px)
        except Exception:
            fallback_text = _table_rows_to_text(payload)
            prepared_rows.extend(_prepare_paragraph_rows(fallback_text, requirement_area_width_px))
            table_fallback_text_count += 1
        else:
            prepared_rows.extend(
                _prepare_image_rows(
                    image_buffer=image_buffer,
                    image_width_px=image_width_px,
                    image_height_px=image_height_px,
                    max_width_px=requirement_area_width_px,
                )
            )
            table_image_count += 1

    if not prepared_rows:
        prepared_rows.append(_PreparedRequirementRow(kind="blank"))
    return prepared_rows, table_image_count, table_fallback_text_count, warnings


def _table_ref_snapshot(payload: dict[str, Any], max_width_px: int) -> tuple[BytesIO | None, int, int, str]:
    export_variant = str(payload.get("export_variant") or "").strip().lower()
    if export_variant not in {"slice", "source"}:
        export_variant = "source" if str(payload.get("source_snapshot_path") or "").strip() else "slice"
    if export_variant == "source":
        source_snapshot_path = str(payload.get("source_snapshot_path") or "").strip()
        if source_snapshot_path:
            try:
                raw = load_snapshot_bytes(source_snapshot_path)
                buffer = BytesIO(raw)
                image = PILImage.open(BytesIO(raw))
                return buffer, int(image.width), int(image.height), ""
            except Exception:
                pass
        export_variant = "slice"
        warning_text = "source snapshot missing, fell back to slice"
    else:
        warning_text = ""

    snapshot_path = str(payload.get("slice_snapshot_path") or "").strip()
    if snapshot_path and str(payload.get("slice_render_mode") or "").strip().lower() != "structured":
        try:
            raw = load_snapshot_bytes(snapshot_path)
            buffer = BytesIO(raw)
            image = PILImage.open(BytesIO(raw))
            return buffer, int(image.width), int(image.height), warning_text
        except Exception:
            pass

    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers or rows:
        try:
            buffer, image_width, image_height = _render_table_image(
                [headers, *rows] if headers else rows,
                max_width_px=max_width_px,
                header_row_count=1 if headers else 0,
            )
            return buffer, image_width, image_height, warning_text
        except Exception:
            return None, 0, 0, warning_text
    return None, 0, 0, warning_text


def _table_ref_to_text(payload: dict[str, Any]) -> str:
    headers = [str(cell or "") for cell in payload.get("inherited_headers") or []]
    rows = [
        [str(cell or "") for cell in row]
        for row in payload.get("slice_rows") or []
        if isinstance(row, list)
    ]
    if headers:
        return _table_rows_to_text([headers, *rows], include_header=True)
    if rows:
        return _table_rows_to_text(rows, include_header=False)
    return str(payload.get("slice_ai_projection") or "").strip()


def _write_vertical_block(ws, *, start_row: int, end_row: int, col: int, value: Any, cell_format: Any) -> None:
    if end_row > start_row:
        ws.merge_range(start_row, col, end_row, col, value, cell_format)
    else:
        ws.write(start_row, col, value, cell_format)


def _prepare_paragraph_rows(text: str, max_width_px: int) -> list[_PreparedRequirementRow]:
    wrapped_lines = _wrap_text_to_export_lines(text, max_width_px)
    chunk_size = _max_wrapped_lines_per_row()
    rows: list[_PreparedRequirementRow] = []
    for start in range(0, max(1, len(wrapped_lines)), chunk_size):
        chunk = wrapped_lines[start : start + chunk_size] or [""]
        chunk_text = "\n".join(chunk)
        rows.append(
            _PreparedRequirementRow(
                kind="paragraph",
                text=chunk_text,
                row_height=_estimate_total_text_height(chunk_text, max_width_px, cap_to_excel_row=True),
            )
        )
    return rows or [_PreparedRequirementRow(kind="paragraph", text="", row_height=_DEFAULT_ROW_HEIGHT_POINTS)]


def _prepare_image_rows(
    *,
    image_buffer: BytesIO,
    image_width_px: int,
    image_height_px: int,
    max_width_px: int,
) -> list[_PreparedRequirementRow]:
    width_scale = min(1.0, max_width_px / max(image_width_px, 1))
    fit_scale = min(width_scale, _MAX_IMAGE_ROW_RENDER_HEIGHT_PX / max(image_height_px, 1))
    if fit_scale >= _MIN_SINGLE_IMAGE_SCALE:
        rendered_height_px = max(1, int(round(image_height_px * fit_scale)))
        return [
            _PreparedRequirementRow(
                kind="image",
                row_height=_pixels_to_row_height_points(rendered_height_px + 8),
                image_buffer=image_buffer,
                x_scale=fit_scale,
                y_scale=fit_scale,
            )
        ]

    x_scale = width_scale
    y_scale = width_scale
    rendered_height_px = max(1, int(round(image_height_px * width_scale)))
    if rendered_height_px <= _MAX_IMAGE_ROW_RENDER_HEIGHT_PX:
        return [
            _PreparedRequirementRow(
                kind="image",
                row_height=_pixels_to_row_height_points(rendered_height_px + 8),
                image_buffer=image_buffer,
                x_scale=x_scale,
                y_scale=y_scale,
            )
        ]

    raw = image_buffer.getvalue()
    with PILImage.open(BytesIO(raw)) as image:
        segment_source_height = max(1, int(_MAX_IMAGE_ROW_RENDER_HEIGHT_PX / max(y_scale, 0.01)))
        rows: list[_PreparedRequirementRow] = []
        for top in range(0, image.height, segment_source_height):
            bottom = min(image.height, top + segment_source_height)
            segment = image.crop((0, top, image.width, bottom))
            segment_buffer = BytesIO()
            segment.save(segment_buffer, format="PNG")
            segment_buffer.seek(0)
            segment_render_height_px = max(1, int(round(segment.height * y_scale)))
            rows.append(
                _PreparedRequirementRow(
                    kind="image",
                    row_height=_pixels_to_row_height_points(segment_render_height_px + 8),
                    image_buffer=segment_buffer,
                    x_scale=x_scale,
                    y_scale=y_scale,
                )
            )
    return rows or [
        _PreparedRequirementRow(
            kind="image",
            row_height=_DEFAULT_ROW_HEIGHT_POINTS,
            image_buffer=image_buffer,
            x_scale=x_scale,
            y_scale=y_scale,
        )
    ]


def _ensure_required_block_height(
    rows: list[_PreparedRequirementRow],
    *,
    required_total_height: float,
) -> None:
    total_height = _total_prepared_row_height(rows)
    if total_height >= required_total_height:
        return

    while total_height + 0.1 < required_total_height:
        adjustable = [
            row
            for row in rows
            if row.kind in {"paragraph", "blank"} and row.row_height < _MAX_ROW_HEIGHT_POINTS - 0.1
        ]
        if not adjustable:
            adjustable = [row for row in rows if row.row_height < _MAX_ROW_HEIGHT_POINTS - 0.1]
        if not adjustable:
            extra_row_height = min(
                _MAX_ROW_HEIGHT_POINTS,
                max(_DEFAULT_ROW_HEIGHT_POINTS, required_total_height - total_height),
            )
            rows.append(_PreparedRequirementRow(kind="blank", row_height=extra_row_height))
            total_height += extra_row_height
            continue

        remaining = required_total_height - total_height
        per_row = remaining / len(adjustable)
        added = 0.0
        for row in adjustable:
            capacity = _MAX_ROW_HEIGHT_POINTS - row.row_height
            delta = min(capacity, per_row)
            if delta <= 0:
                continue
            row.row_height += delta
            added += delta
        total_height += added
        if added <= 0.1:
            rows.append(_PreparedRequirementRow(kind="blank", row_height=min(_MAX_ROW_HEIGHT_POINTS, remaining)))
            total_height += min(_MAX_ROW_HEIGHT_POINTS, remaining)


def _total_prepared_row_height(rows: list[_PreparedRequirementRow]) -> float:
    return sum(row.row_height for row in rows)


def _wrap_text_to_export_lines(text: str, max_width_px: int) -> list[str]:
    content = str(text or "")
    if not content:
        return [""]
    max_units = max(8, (max_width_px - (_WORKBOOK_TEXT_PADDING_X * 2)) // _TABLE_UNIT_PX)
    wrapped = _wrap_text_to_units(content, max_units)
    return wrapped.split("\n") or [wrapped]


@lru_cache(maxsize=1)
def _max_wrapped_lines_per_row() -> int:
    usable_height = max(36.0, _MAX_ROW_HEIGHT_POINTS - _WORKBOOK_TEXT_ROW_EXTRA_POINTS)
    return max(1, int(usable_height // _WORKBOOK_TEXT_LINE_HEIGHT_POINTS))


def _estimate_total_text_height(
    text: Any,
    max_width_px: int,
    *,
    cap_to_excel_row: bool = False,
) -> float:
    content = str(text or "")
    if not content.strip():
        return _DEFAULT_ROW_HEIGHT_POINTS
    wrapped_lines = _wrap_text_to_export_lines(content, max_width_px)
    line_count = max(1, len(wrapped_lines))
    height_points = round(
        _WORKBOOK_TEXT_ROW_EXTRA_POINTS + (line_count * _WORKBOOK_TEXT_LINE_HEIGHT_POINTS),
        2,
    )
    if cap_to_excel_row:
        return max(_DEFAULT_ROW_HEIGHT_POINTS, min(_MAX_ROW_HEIGHT_POINTS, height_points))
    return max(_DEFAULT_ROW_HEIGHT_POINTS, height_points)


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


def _ai_decision_label(item: dict[str, Any]) -> str:
    """Resolve the AI assessment label for a requirement row."""
    analysis_label = str(item.get("analysis_label") or "").strip().upper()
    if analysis_label == "INFO":
        return "INFO"
    assessment_mode = str(item.get("assessment_mode") or "").strip().lower()
    if assessment_mode in {"screening_failed", "analysis_failed"}:
        return assessment_mode.upper()
    ai_decision = str(item.get("ai_decision") or "").strip().upper()
    if ai_decision and ai_decision != "REQUIREMENT":
        return ai_decision
    if ai_decision == "REQUIREMENT":
        return "REQUIREMENT" if str(item.get("analysis_basis") or "").strip() else ""
    return ""


def _excel_column_width_to_pixels(width: float) -> int:
    return int(width * 7 + 5)


def _pixels_to_excel_column_width(pixels: int) -> float:
    return round(max(1, pixels - 5) / 7.0, 2)


def _default_requirement_area_width() -> float:
    return _CUSTOMER_REQUIREMENT_COLUMN_WIDTH


def _pixels_to_row_height_points(pixels: int) -> float:
    return round(min(_MAX_ROW_HEIGHT_PX, max(20, pixels)) * 0.75, 2)


def _table_rows_to_text(rows: list[list[str]], *, include_header: bool = True) -> str:
    if not rows:
        return ""
    if include_header:
        header = "| " + " | ".join(str(cell or "") for cell in rows[0]) + " |"
        separator = "| " + " | ".join("---" for _ in rows[0]) + " |"
        body = ["| " + " | ".join(str(cell or "") for cell in row) + " |" for row in rows[1:]]
        return "\n".join([header, separator, *body]).strip()
    return "\n".join("| " + " | ".join(str(cell or "") for cell in row) + " |" for row in rows).strip()


def _render_table_image(
    rows: list[list[str]],
    *,
    max_width_px: int,
    header_row_count: int = 1,
) -> tuple[BytesIO, int, int]:
    normalized_rows = _normalize_table_rows(rows)
    font = load_table_font(_TABLE_FONT_SIZE)
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
        fill_color = _BRAND_HEADER_BACKGROUND if row_index < max(0, int(header_row_count or 0)) else "#FFFFFF"
        for col_index, content in enumerate(row):
            width = column_widths[col_index]
            height = row_heights[row_index]
            draw.rectangle(
                [x, y, x + width, y + height],
                fill=fill_color,
                outline=_BRAND_BORDER,
                width=1,
            )
            draw.multiline_text(
                (x + _TABLE_CELL_PADDING_X, y + _TABLE_CELL_PADDING_Y),
                content,
                fill=_BRAND_TEXT,
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
        "Current Section",
        f"Baseline Section ({baseline_label})",
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
