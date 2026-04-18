from __future__ import annotations

import hashlib
import re
from contextlib import redirect_stderr, redirect_stdout
from difflib import SequenceMatcher
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

from PIL import Image as PILImage
from PIL import ImageChops, ImageDraw, ImageFont

from app.services.appendix_scopes import is_test_outline_scope_value, normalize_scope_id
from app.services.complex_table_mode import hydrate_complex_row_metadata, is_complex_table_row

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
_SNAPSHOT_MAX_HEIGHT_PX = 1480
_COMPLEX_FULL_PAGE_SNAPSHOT_MAX_WIDTH_PX = 2160
_MERGED_REQUIREMENT_COMPLEX_FULL_PAGE_SNAPSHOT_MAX_WIDTH_PX = 3200
_SOURCE_SNAPSHOT_SIGNATURE_VERSION = "source-crop-v57"
_EMBEDDED_ANCHOR_LINE_SPLIT_RE = re.compile(
    r"(?<!\d)(?:\d+(?:\.\d+)+(?:\s*[）)\]】:：.\-．、]\s*|\s+)|\d{1,2}\s*[）)\]】:：.\-．、]\s*)(?=[A-Za-z\u4e00-\u9fff])"
)
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
    reusable_header_contexts: list[dict[str, Any]] = []
    for index, row in enumerate(entries):
        previous_text = _entry_anchor_context_text(entries[index - 1], from_end=True) if index > 0 else ""
        next_text = _entry_anchor_context_text(entries[index + 1], from_end=False) if index + 1 < len(entries) else ""
        inherited_header = _find_recent_reusable_table_header(row, reusable_header_contexts)
        enriched_row, row_assets = enrich_entry_with_table_assets(
            row,
            document_id=document_id,
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
            previous_entry_text=previous_text,
            next_entry_text=next_text,
            inherited_header=inherited_header,
        )
        enriched_row = _refresh_enriched_entry_text_fields(enriched_row)
        enriched_row = hydrate_complex_row_metadata(enriched_row)
        enriched.append(enriched_row)
        assets.extend(row_assets)
        _remember_reusable_table_headers(reusable_header_contexts, row, row_assets)
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
    previous_entry_text: str = "",
    next_entry_text: str = "",
    inherited_header: list[str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    copied = hydrate_complex_row_metadata(dict(row))
    if str(copied.get("entry_kind") or "").strip().upper() == "HEADING":
        copied["table_refs"] = []
        copied["content_blocks"] = _paragraph_only_blocks(_split_text_blocks(_entry_source_text(copied)))
        copied["has_table_asset"] = False
        copied["table_render_mode"] = ""
        return copied, []
    if is_complex_table_row(copied):
        return _enrich_complex_table_row(
            copied,
            document_id=document_id,
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
        )
    existing_table_refs = _normalize_table_refs(copied.get("table_refs"))
    text = _entry_source_text(copied)
    if not text:
        copied["table_refs"] = []
        copied["content_blocks"] = _normalize_content_blocks(copied.get("content_blocks"))
        copied["has_table_asset"] = False
        copied["table_render_mode"] = ""
        return copied, []

    blocks = _split_text_blocks(text)
    row_table_title = _table_title_for_row(copied)
    if not any(block["kind"] == "table" for block in blocks):
        title = row_table_title
        recovered = None
        if _looks_like_source_table_candidate_text(text, title=title):
            recovered = _recover_table_from_pdf_source(
                source_file_path=source_file_path,
                source_page=_coerce_int(copied.get("source_page")),
                source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                table_text=text,
                table_title=title,
                current_headers=[],
                current_rows=[],
                notes=[],
                before_context_text=previous_entry_text,
                after_context_text=next_entry_text,
                source_table_detection_cache=source_table_detection_cache,
                source_line_detection_cache=source_line_detection_cache,
            )
        if recovered and _recovered_table_looks_useful(recovered[0], recovered[1]):
            headers, body_rows, recovered_notes = recovered
            lead_paragraphs, tail_paragraphs = _split_text_around_recovered_table(
                text,
                headers=headers,
                rows=body_rows,
                title=title,
            )
            tail_paragraphs = _drop_trailing_table_context_paragraphs(
                tail_paragraphs,
                next_entry_text=next_entry_text,
            )
            table_id = _build_table_id(copied, text, 0)
            snapshot_path = _write_snapshot(
                table_id=table_id,
                snapshot_root=snapshot_root,
                headers=headers,
                rows=body_rows,
                render_mode="structured",
                text=text,
            )
            source_snapshot_path, source_snapshot_meta = _write_source_snapshot(
                snapshot_root=snapshot_root,
                source_file_path=source_file_path,
                source_page=_coerce_int(copied.get("source_page")),
                source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                table_text=text,
                table_title=title,
                headers=headers,
                rows=body_rows,
                notes=recovered_notes,
                before_context_text=previous_entry_text,
                after_context_text=next_entry_text,
                source_snapshot_cache=source_snapshot_cache,
                source_table_detection_cache=source_table_detection_cache,
                source_line_detection_cache=source_line_detection_cache,
                source_page_floor_offsets={},
                prefer_clause_window=_should_prefer_source_clause_window(
                    table_title=title,
                    render_mode="structured",
                    headers=headers,
                    rows=body_rows,
                    source_page=_coerce_int(copied.get("source_page")),
                    source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                    notes=recovered_notes,
                ),
            )
            resolved_source_page = _coerce_int(source_snapshot_meta.get("resolved_page_start")) or _coerce_int(copied.get("source_page"))
            resolved_source_page_end = _coerce_int(source_snapshot_meta.get("resolved_page_end")) or _coerce_int(copied.get("source_page_end")) or resolved_source_page
            ai_projection = _build_ai_projection(
                table_id=table_id,
                title=title,
                headers=headers,
                rows=body_rows,
                notes=recovered_notes,
                render_mode="structured",
            )
            ref = _normalize_table_ref(
                {
                    "table_id": table_id,
                    "table_title": title,
                    "row_range": [1, len(body_rows)] if body_rows else [],
                    "row_ids": [f"r{index + 1}" for index in range(len(body_rows))],
                    "inherited_headers": headers,
                    "slice_rows": body_rows,
                    "inherited_group_title": "",
                    "inherited_notes": recovered_notes,
                    "slice_ai_projection": ai_projection,
                    "slice_snapshot_path": str(snapshot_path),
                    "source_snapshot_path": str(source_snapshot_path) if source_snapshot_path else "",
                    "slice_render_key": table_id,
                    "slice_render_mode": "structured",
                    "source_page": resolved_source_page,
                    "source_page_end": resolved_source_page_end,
                    "structure_confidence": 0.9,
                    "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
                }
            )
            asset = {
                "table_id": table_id,
                "document_id": document_id,
                "section_instance_key": str(copied.get("section_instance_key") or copied.get("section_key") or ""),
                "page_span": {
                    "start": resolved_source_page,
                    "end": resolved_source_page_end,
                },
                "table_title": title,
                "headers": headers,
                "rows": body_rows,
                "group_labels": [],
                "notes": recovered_notes,
                "render_mode": "structured",
                "snapshot_path": str(snapshot_path),
                "source_snapshot_path": str(source_snapshot_path) if source_snapshot_path else "",
                "ai_projection": ai_projection,
                "confidence": 0.9,
                "source_entry_id": str(copied.get("entry_id") or copied.get("req_code") or ""),
                "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
            }
            content_blocks = [{"kind": "paragraph", "text": part} for part in lead_paragraphs]
            content_blocks.append({"kind": "table", "table_id": table_id})
            content_blocks.extend({"kind": "paragraph", "text": part} for part in tail_paragraphs)
            content_blocks, table_refs, dropped_ids = _prune_duplicate_shallow_snapshot_tables(content_blocks, [ref])
            content_blocks, table_refs, extra_drop_ids = _prune_redundant_lead_source_tables(content_blocks, table_refs)
            dropped_ids |= extra_drop_ids
            content_blocks = _ensure_leading_table_title_block(
                content_blocks,
                row=copied,
                table_title=title,
            )
            copied["table_refs"] = table_refs
            copied["content_blocks"] = content_blocks
            copied["has_table_asset"] = bool(table_refs)
            copied["table_render_mode"] = "structured" if table_refs else ""
            return copied, [] if table_id in dropped_ids else [asset]
        visual_ref, visual_asset = _build_visual_snapshot_ref(
            copied,
            document_id=document_id,
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
            previous_entry_text=previous_entry_text,
            next_entry_text=next_entry_text,
        )
        table_refs = [visual_ref] if visual_ref else []
        paragraph_blocks = _paragraph_only_blocks(blocks)
        if visual_ref:
            paragraph_blocks.append({"kind": "table", "table_id": str(visual_ref.get("table_id") or "")})
            paragraph_blocks, table_refs, dropped_ids = _prune_duplicate_shallow_snapshot_tables(paragraph_blocks, table_refs)
            paragraph_blocks, table_refs, extra_drop_ids = _prune_redundant_lead_source_tables(paragraph_blocks, table_refs)
            dropped_ids |= extra_drop_ids
            paragraph_blocks = _ensure_leading_table_title_block(
                paragraph_blocks,
                row=copied,
                table_title=title,
            )
        else:
            dropped_ids = set()
        copied["table_refs"] = table_refs
        copied["content_blocks"] = paragraph_blocks
        copied["has_table_asset"] = bool(table_refs)
        copied["table_render_mode"] = str(table_refs[0].get("slice_render_mode") or "") if table_refs else ""
        return copied, [visual_asset] if visual_asset and str(visual_ref.get("table_id") or "") not in dropped_ids else []

    table_refs: list[dict[str, Any]] = []
    content_blocks: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    source_page_floor_offsets: dict[int, float] = {}

    for block_index, block in enumerate(blocks):
        if block["kind"] == "paragraph":
            text_value = _clean_paragraph_block_text(str(block.get("text") or "").strip())
            if _should_skip_trailing_table_context_block(blocks, block_index, next_entry_text=next_entry_text):
                continue
            if text_value:
                content_blocks.append({"kind": "paragraph", "text": text_value})
            continue

        table_text = str(block.get("text") or "").strip()
        if not table_text:
            continue
        before_context_text = _neighbor_block_text(blocks, block_index, offset=-1) or previous_entry_text
        after_block_text = _neighbor_block_text(blocks, block_index, offset=1)
        trailing_context_text = _trailing_table_context_text(
            blocks,
            block_index,
            next_entry_text=next_entry_text,
        )
        table_id = _build_table_id(copied, table_text, block_index)
        notes = _collect_block_notes(blocks, block_index)
        if trailing_context_text:
            notes = [note for note in notes if str(note or "").strip() != trailing_context_text]
        prefer_clause_window = _should_prefer_source_clause_window(
            table_title=row_table_title,
            render_mode=str(block.get("render_mode") or "").strip().lower(),
            headers=block.get("header_cells") or [],
            rows=[],
            source_page=_coerce_int(copied.get("source_page")),
            source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
            notes=notes,
            trailing_context_text=trailing_context_text,
        )
        after_context_text = (
            next_entry_text
            if trailing_context_text or (prefer_clause_window and after_block_text)
            else (after_block_text or next_entry_text)
        )
        table_title = _resolve_table_block_title(
            blocks=blocks,
            block_index=block_index,
            fallback_title=row_table_title,
        )
        block_header = _resolve_table_block_header(
            lines=block.get("lines") or [],
            explicit_header=block.get("header_cells") or [],
            inherited_header=inherited_header or [],
            notes=notes,
            row=copied,
            table_title=table_title,
        )
        headers, body_rows, render_mode = _extract_structured_table(
            block.get("lines") or [],
            explicit_header=block_header,
        )
        recovered = None
        source_structure_mismatch = False
        source_column_count_hint = 0
        parsed_headers = list(headers)
        parsed_rows = [list(row) for row in body_rows]
        if _looks_like_source_table_candidate_text(table_text, title=table_title):
            recovered = _recover_table_from_pdf_source(
                source_file_path=source_file_path,
                source_page=_coerce_int(copied.get("source_page")),
                source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                table_text=table_text,
                table_title=table_title,
                current_headers=headers,
                current_rows=body_rows,
                notes=notes,
                before_context_text=before_context_text,
                after_context_text=after_context_text,
                source_table_detection_cache=source_table_detection_cache,
                source_line_detection_cache=source_line_detection_cache,
            )
        if recovered:
            recovered_headers, recovered_rows, _ = recovered
            if _recovered_source_indicates_column_mismatch(
                current_headers=parsed_headers,
                current_rows=parsed_rows,
                recovered_headers=recovered_headers,
                recovered_rows=recovered_rows,
            ):
                source_structure_mismatch = True
                source_column_count_hint = max(
                    len(recovered_headers),
                    max((len(row) for row in recovered_rows), default=0),
                )
        if recovered and _recovered_table_looks_useful(recovered[0], recovered[1]):
            recovered_headers, recovered_rows, recovered_notes = recovered
            if _should_replace_with_recovered_table(
                current_render_mode=render_mode,
                current_headers=headers,
                current_rows=body_rows,
                recovered_headers=recovered_headers,
                recovered_rows=recovered_rows,
            ):
                headers = recovered_headers
                body_rows = recovered_rows
                if recovered_notes:
                    notes = recovered_notes
                render_mode = "structured"
        resolved_row_source_page = _coerce_int(copied.get("source_page"))
        resolved_row_source_page_end = _coerce_int(copied.get("source_page_end")) or resolved_row_source_page
        if render_mode == "structured" and headers and body_rows:
            body_rows, resolved_row_source_page, resolved_row_source_page_end = _extend_structured_table_rows_from_source(
                source_file_path=source_file_path,
                source_page=resolved_row_source_page,
                source_page_end=resolved_row_source_page_end,
                table_text=table_text,
                table_title=table_title,
                headers=headers,
                rows=body_rows,
                notes=notes,
                source_table_detection_cache=source_table_detection_cache,
            )
        snapshot_path = _write_snapshot(
            table_id=table_id,
            snapshot_root=snapshot_root,
            headers=headers,
            rows=body_rows,
            render_mode=render_mode,
            text=table_text,
        )
        prefer_clause_window = _should_prefer_source_clause_window(
            table_title=table_title,
            render_mode=render_mode,
            headers=headers,
            rows=body_rows,
            source_page=resolved_row_source_page,
            source_page_end=resolved_row_source_page_end,
            notes=notes,
            trailing_context_text=trailing_context_text,
        )
        after_context_text = (
            next_entry_text
            if trailing_context_text or (prefer_clause_window and after_block_text)
            else (after_block_text or next_entry_text)
        )
        source_snapshot_path, source_snapshot_meta = _write_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=resolved_row_source_page,
            source_page_end=resolved_row_source_page_end,
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
            prefer_clause_window=prefer_clause_window,
        )
        resolved_source_page = _coerce_int(source_snapshot_meta.get("resolved_page_start")) or resolved_row_source_page
        resolved_source_page_end = _coerce_int(source_snapshot_meta.get("resolved_page_end")) or resolved_row_source_page_end
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
                "start": resolved_source_page,
                "end": resolved_source_page_end,
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
            "table_title": table_title,
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
            "source_page": resolved_source_page,
            "source_page_end": resolved_source_page_end,
            "structure_confidence": float(asset["confidence"]),
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
            "source_structure_mismatch": source_structure_mismatch,
            "source_column_count_hint": source_column_count_hint,
        }
        existing_ref = _match_existing_table_ref(existing_table_refs, table_id=table_id, index=len(table_refs))
        if existing_ref:
            table_ref["export_variant"] = str(existing_ref.get("export_variant") or "").strip().lower()
        table_ref = _normalize_table_ref(table_ref)
        assets.append(asset)
        table_refs.append(table_ref)
        content_blocks.append({"kind": "table", "table_id": table_id})

    content_blocks, table_refs, dropped_ids = _prune_duplicate_shallow_snapshot_tables(content_blocks, table_refs)
    content_blocks, table_refs, extra_drop_ids = _prune_redundant_lead_source_tables(content_blocks, table_refs)
    dropped_ids |= extra_drop_ids
    content_blocks = _ensure_leading_table_title_block(
        content_blocks,
        row=copied,
        table_title=row_table_title,
    )
    if dropped_ids:
        assets = [asset for asset in assets if str(asset.get("table_id") or "").strip() not in dropped_ids]
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


def current_source_snapshot_build_token() -> str:
    return _SOURCE_SNAPSHOT_SIGNATURE_VERSION


def _build_visual_snapshot_ref(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    previous_entry_text: str,
    next_entry_text: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    text = _entry_source_text(row).strip()
    title = _table_title_for_row(row)
    if not text or not _should_generate_visual_snapshot(text, title=title):
        return None, None
    first_line = text.splitlines()[0].strip()
    visual_rows = _derive_visual_snapshot_slice_rows(text, title=title)
    prefer_clause_window = _should_prefer_source_clause_window(
        table_title=first_line,
        render_mode="snapshot_only",
        headers=[],
        rows=visual_rows,
        source_page=_coerce_int(row.get("source_page")),
        source_page_end=_coerce_int(row.get("source_page_end")) or _coerce_int(row.get("source_page")),
        notes=[],
        trailing_context_text=next_entry_text,
    )
    table_id = _build_table_id(row, f"visual::{text}", 0)
    snapshot_path = _write_snapshot(
        table_id=table_id,
        snapshot_root=snapshot_root / "preview",
        headers=[],
        rows=[],
        render_mode="snapshot_only",
        text=first_line,
    )
    source_snapshot_path, source_snapshot_meta = _write_source_snapshot(
        snapshot_root=snapshot_root,
        source_file_path=source_file_path,
        source_page=_coerce_int(row.get("source_page")),
        source_page_end=_coerce_int(row.get("source_page_end")) or _coerce_int(row.get("source_page")),
        table_text=text,
        table_title=first_line,
        headers=[],
        rows=visual_rows,
        notes=[],
        before_context_text=previous_entry_text,
        after_context_text=next_entry_text,
        source_snapshot_cache=source_snapshot_cache,
        source_table_detection_cache=source_table_detection_cache,
        source_line_detection_cache=source_line_detection_cache,
        source_page_floor_offsets={},
        prefer_clause_window=prefer_clause_window,
    )
    if not source_snapshot_path:
        return None, None
    resolved_source_page = _coerce_int(source_snapshot_meta.get("resolved_page_start")) or _coerce_int(row.get("source_page"))
    resolved_source_page_end = _coerce_int(source_snapshot_meta.get("resolved_page_end")) or _coerce_int(row.get("source_page_end")) or resolved_source_page
    image_block_count = int(source_snapshot_meta.get("image_block_count") or 0)
    if not _should_keep_visual_snapshot(text, title=first_line, image_block_count=image_block_count):
        return None, None
    ai_projection = _build_ai_projection(
        table_id=table_id,
        title=first_line,
        headers=[],
        rows=[],
        notes=[],
        render_mode="snapshot_only",
    )
    asset = {
        "table_id": table_id,
        "document_id": document_id,
        "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
        "page_span": {
            "start": resolved_source_page,
            "end": resolved_source_page_end,
        },
        "table_title": title or first_line,
        "headers": [],
        "rows": visual_rows,
        "group_labels": [],
        "notes": [],
        "render_mode": "snapshot_only",
        "snapshot_path": str(snapshot_path),
        "source_snapshot_path": str(source_snapshot_path),
        "ai_projection": ai_projection,
        "confidence": 0.45,
        "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
        "source_image_block_count": image_block_count,
    }
    ref = _normalize_table_ref(
        {
            "table_id": table_id,
            "row_range": [],
            "row_ids": [],
            "table_title": title or first_line,
            "inherited_headers": [],
            "slice_rows": visual_rows,
            "inherited_group_title": "",
            "inherited_notes": [],
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path),
            "slice_render_key": table_id,
            "slice_render_mode": "snapshot_only",
            "source_page": resolved_source_page,
            "source_page_end": resolved_source_page_end,
            "structure_confidence": 0.45,
            "source_image_block_count": image_block_count,
        }
    )
    return ref, asset


def _derive_visual_snapshot_slice_rows(text: str, *, title: str = "") -> list[list[str]]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    candidates = []
    if lines:
        candidates.append(lines[0])
    if title:
        candidates.append(str(title).strip())

    for candidate in candidates:
        match = re.match(r"^\s*(\d{1,3})(?:\s*[）)]\s*|\s+)(.+)$", candidate)
        if not match:
            continue
        number = str(match.group(1) or "").strip()
        label = str(match.group(2) or "").strip()
        if not number or not label or len(label) > 120:
            continue
        return [[number, label]]
    return []


def normalize_preserved_entry_table_assets(row: dict[str, Any]) -> dict[str, Any]:
    copied = hydrate_complex_row_metadata(dict(row))
    refs = _normalize_table_refs(copied.get("table_refs"))
    if bool(copied.get("complex_table_mode")):
        snapshot_mode = str(copied.get("complex_snapshot_mode") or "").strip().lower()
        if snapshot_mode:
            for ref in refs:
                if isinstance(ref, dict) and bool(ref.get("complex_table_mode")):
                    ref.setdefault("complex_snapshot_mode", snapshot_mode)
    copied["table_refs"] = refs
    copied["content_blocks"] = _normalize_content_blocks(copied.get("content_blocks"))
    copied["has_table_asset"] = bool(refs)
    copied["table_render_mode"] = str(copied.get("table_render_mode") or _collapse_render_mode(refs))
    return hydrate_complex_row_metadata(copied)


def _complex_row_split_level(row: dict[str, Any], table_ref: dict[str, Any] | None = None) -> str:
    level = str(
        (table_ref or {}).get("complex_split_level")
        or row.get("complex_split_level")
        or ""
    ).strip()
    if level:
        return level
    role = str(row.get("complex_role") or "").strip().lower()
    return "zone" if role == "zone_summary" else "group"


def _enrich_complex_table_row(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    copied = normalize_preserved_entry_table_assets(row)
    zone_id = str(copied.get("complex_zone_id") or "").strip()
    family = str(copied.get("complex_family") or "").strip()
    role = str(copied.get("complex_role") or "").strip().lower()
    split_level = _complex_row_split_level(copied)
    label = str(
        copied.get("complex_snapshot_title")
        or copied.get("complex_group_label")
        or copied.get("parent_section_title")
        or copied.get("section_title")
        or copied.get("display_text")
        or "复杂跨页表"
    ).splitlines()[0].strip()
    shadow_signatures = _normalize_string_list(copied.get("complex_shadow_signatures"))
    existing_ref = next(
        (
            ref
            for ref in copied.get("table_refs") or []
            if isinstance(ref, dict) and bool(ref.get("complex_table_mode"))
        ),
        None,
    )
    if existing_ref and str(existing_ref.get("source_snapshot_path") or "").strip():
        table_id = str(existing_ref.get("table_id") or "").strip() or _build_table_id(
            copied,
            f"complex::{zone_id}::{role}::{label}",
            0,
        )
        ai_projection = existing_ref.get("slice_ai_projection")
        if not isinstance(ai_projection, dict):
            ai_projection = _build_ai_projection(
                table_id=table_id,
                title=label or "复杂跨页表",
                headers=[],
                rows=[],
                notes=_normalize_string_list(copied.get("complex_warnings")),
                render_mode="snapshot_only",
            )
        asset = _build_complex_table_asset(
            row=copied,
            document_id=document_id,
            table_ref=dict(existing_ref, table_id=table_id, complex_shadow_signatures=shadow_signatures),
            label=label or "复杂跨页表",
            ai_projection=ai_projection,
        )
        return copied, [asset]

    source_page = _coerce_int(copied.get("source_page"))
    source_page_end = _coerce_int(copied.get("source_page_end")) or source_page
    if source_page <= 0 or not source_file_path:
        return copied, []

    after_label_hint = str(copied.get("complex_after_label_hint") or "").strip()
    snapshot_mode = str(copied.get("complex_snapshot_mode") or "anchor_window").strip().lower()
    table_id = _build_table_id(copied, f"complex::{zone_id}::{role}::{label}", 0)
    snapshot_path = _write_snapshot(
        table_id=table_id,
        snapshot_root=snapshot_root,
        headers=[],
        rows=[],
        render_mode="snapshot_only",
        text=label or "复杂跨页表",
    )

    if snapshot_mode == "full_page_span":
        source_snapshot_path, source_snapshot_meta = _write_full_page_span_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=source_page,
            source_page_end=source_page_end,
            cache_token="full-page-span",
            cache_key_hint=f"{zone_id}:{role}:{label}",
            source_snapshot_cache=source_snapshot_cache,
            crop_to_content=True,
            max_height_px=None,
            max_width_px=_complex_full_page_snapshot_width_px(copied),
        )
    else:
        source_snapshot_path, source_snapshot_meta = _write_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=source_page,
            source_page_end=source_page_end,
            table_text=str(copied.get("raw_text") or copied.get("display_text") or label).strip(),
            table_title=label,
            headers=[],
            rows=[],
            notes=_normalize_string_list(copied.get("complex_warnings")),
            before_context_text="",
            after_context_text=after_label_hint,
            source_snapshot_cache=source_snapshot_cache,
            source_table_detection_cache=source_table_detection_cache,
            source_line_detection_cache=source_line_detection_cache,
            source_page_floor_offsets={},
            prefer_clause_window=True,
            allow_next_clause_stop=False,
        )
    if not source_snapshot_path:
        return copied, []

    ai_projection = _build_ai_projection(
        table_id=table_id,
        title=label or "复杂跨页表",
        headers=[],
        rows=[],
        notes=_normalize_string_list(copied.get("complex_warnings")),
        render_mode="snapshot_only",
    )
    complex_ref = _normalize_table_ref(
        {
            "table_id": table_id,
            "row_range": [],
            "row_ids": [],
            "table_title": label or "复杂跨页表",
            "inherited_headers": [],
            "slice_rows": [],
            "inherited_group_title": "",
            "inherited_notes": _normalize_string_list(copied.get("complex_warnings")),
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path),
            "slice_render_key": table_id,
            "slice_render_mode": "snapshot_only",
            "source_page": source_page,
            "source_page_end": source_page_end,
            "structure_confidence": 0.99,
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
            "complex_table_mode": True,
            "complex_zone_id": zone_id,
            "complex_family": family,
            "complex_split_level": split_level,
            "complex_group_label": str(copied.get("complex_group_label") or ""),
            "complex_snapshot_mode": snapshot_mode,
            "complex_warnings": _normalize_string_list(copied.get("complex_warnings")),
            "complex_shadow_signatures": shadow_signatures,
            "export_variant_locked": True,
            "export_variant": "source",
            "recommended_export_variant": "source",
        }
    )
    copied["table_refs"] = [complex_ref]
    copied["content_blocks"] = [
        {
            "kind": "paragraph",
            "text": str(copied.get("display_text") or copied.get("requirement_text") or label).strip(),
        },
        {"kind": "table", "table_id": table_id},
    ]
    copied["has_table_asset"] = True
    copied["table_render_mode"] = "snapshot_only"
    copied = hydrate_complex_row_metadata(copied)
    asset = _build_complex_table_asset(
        row=copied,
        document_id=document_id,
        table_ref=complex_ref,
        label=label or "复杂跨页表",
        ai_projection=ai_projection,
        source_image_block_count=int(source_snapshot_meta.get("image_block_count") or 0),
    )
    return copied, [asset]


def _should_generate_visual_snapshot(text: str, *, title: str = "") -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 1:
        return False
    first_line = lines[0]
    if _looks_like_local_clause_title(first_line):
        return _has_strong_visual_snapshot_cue(text, title=first_line)

    title_value = str(title or "").strip()
    if _looks_like_attachment_table_title(title_value):
        compact_value = re.sub(r"\s+", "", str(text or ""))
        if len(compact_value) >= 220:
            return False
        compact_items = re.findall(r"[A-Za-z\u4e00-\u9fff0-9][A-Za-z\u4e00-\u9fff0-9\-_/（）()]{2,32}", text)
        if len(compact_items) >= 8:
            return True
        if len(text) >= 120:
            return True
    return False


def _build_complex_table_asset(
    *,
    row: dict[str, Any],
    document_id: str,
    table_ref: dict[str, Any],
    label: str,
    ai_projection: dict[str, Any],
    source_image_block_count: int | None = None,
) -> dict[str, Any]:
    source_page = _coerce_int(table_ref.get("source_page") or row.get("source_page"))
    source_page_end = _coerce_int(table_ref.get("source_page_end") or row.get("source_page_end")) or source_page
    return {
        "table_id": str(table_ref.get("table_id") or "").strip(),
        "document_id": document_id,
        "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
        "page_span": {"start": source_page, "end": source_page_end},
        "table_title": label or "复杂跨页表",
        "headers": [],
        "rows": [],
        "group_labels": [str(row.get("complex_group_label") or "")] if str(row.get("complex_group_label") or "").strip() else [],
        "notes": _normalize_string_list(row.get("complex_warnings")),
        "render_mode": "snapshot_only",
        "snapshot_path": str(table_ref.get("slice_snapshot_path") or ""),
        "source_snapshot_path": str(table_ref.get("source_snapshot_path") or ""),
        "ai_projection": ai_projection,
        "confidence": float(table_ref.get("structure_confidence") or 0.99),
        "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
        "source_image_block_count": int(
            source_image_block_count
            if source_image_block_count is not None
            else (table_ref.get("source_image_block_count") or 0)
        ),
        "complex_table_mode": True,
        "complex_zone_id": str(row.get("complex_zone_id") or ""),
        "complex_family": str(row.get("complex_family") or ""),
        "complex_split_level": _complex_row_split_level(row, table_ref),
        "complex_shadow_signatures": _normalize_string_list(table_ref.get("complex_shadow_signatures")),
    }


def _should_keep_visual_snapshot(text: str, *, title: str, image_block_count: int) -> bool:
    if image_block_count > 0:
        return True
    return _has_strong_visual_snapshot_cue(text, title=title)


def _has_strong_visual_snapshot_cue(text: str, *, title: str = "") -> bool:
    value = f"{title}\n{text}".strip()
    compact_value = re.sub(r"\s+", "", value)
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if re.search(r"(?:环境适应性|环境要求)", value) and (
        re.search(r"IP\d{2}", value, re.I)
        or value.count("℃") >= 2
        or any(keyword in value for keyword in ("涉水", "冰冻", "温度梯度", "温度循环", "温度冲击"))
    ):
        if len(lines) <= 4 and len(compact_value) <= 180:
            return True
    if re.search(r"序号测试(?:类别|项目)测试(?:小类|工况|顺序)", compact_value, re.I):
        return True
    return any(
        keyword in value
        for keyword in (
            "如下图",
            "框图",
            "流程",
            "状态机",
            "下表",
            "图示",
            "示意图",
            "安全目标",
            "位移控制要求",
            "通讯矩阵",
        )
    )


def _looks_like_source_table_candidate_text(text: str, *, title: str = "") -> bool:
    lines = [str(line or "").strip() for line in str(text or "").splitlines() if str(line or "").strip()]
    if not lines:
        return False
    if any(_looks_like_markdown_table_row(line) for line in lines):
        return True
    if any(_match_explicit_header_line(line) for line in lines[:3]):
        return True

    joined = "\n".join(lines[:6])
    if re.search(
        r"(?:交付物清单|附件名称|项目代号|车型代号|手工件到样时间|硬模件到样时间|量纲|频率（Hz）|振幅（mm）|质心最大位移|总成自由度|解耦率|安全目标|故障容错时间间隔|ftti|工作产出保密性级别|confidentiality level|rasic definition|常见不安全密码算法|附件目录)",
        joined,
        re.I,
    ):
        return True
    compact_joined = re.sub(r"\s+", "", joined)
    if re.search(r"序号测试(?:类别|项目)测试(?:小类|工况|顺序)", compact_joined, re.I):
        return True

    data_like_lines = 0
    for line in lines[1:4]:
        token_count = len([part for part in re.split(r"\s+", line) if part])
        digit_count = sum(ch.isdigit() for ch in line)
        if token_count >= 5 and digit_count >= 6:
            data_like_lines += 1
    if data_like_lines >= 1 and re.search(r"(?:车型|代号|时间|量纲|项目)", joined):
        return True

    title_value = str(title or "").strip()
    return bool(title_value and re.search(r"(?:目录|清单|矩阵|表格|列表)", title_value))


def _looks_like_attachment_table_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            r"(?:附件|附录|appendix|annex|算法|保密|rasic|definition|定义|目录|清单|评估表|试验大纲|规范)",
            value,
            re.I,
        )
    )


def _looks_like_attachment_heading_anchor(title: str) -> bool:
    value = str(title or "").strip()
    if not value or len(value) > 72:
        return False
    if _looks_like_generic_compact_band_header(value):
        return False
    if re.search(r"[，。！？!?；;：:]", value):
        return False
    if value.count(" ") > 6 and "/" not in value:
        return False
    return bool(
        re.search(
            r"(?:附件|附录|appendix|annex|算法|保密|rasic|definition|目录|清单|评估表|试验大纲|规范)",
            value,
            re.I,
        )
    )


def _looks_like_local_clause_title(text: str) -> bool:
    return bool(re.match(r"^\s*\d+(?:\.\d+)*\s*[）)]\s*.+$", str(text or "").strip()))


def _extract_table_title_from_source_anchor(anchor: str) -> str:
    source = str(anchor or "").strip()
    if not source:
        return ""
    patterns = (
        r"(?:^|\|\s*)Clause\s+([^|]+)",
        r"(?:^|\|\s*)Section\s+([^|]+)",
        r"(?:^|\|\s*)Heading\s+([^|]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if not match:
            continue
        candidate = str(match.group(1) or "").strip()
        if candidate:
            return candidate
    return ""


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


def _snapshot_file_is_usable(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        if path.stat().st_size <= 0:
            return False
    except OSError:
        return False

    try:
        with PILImage.open(path) as image:
            rgb = image.convert("RGB")
        diff = ImageChops.difference(rgb, PILImage.new("RGB", rgb.size, "white"))
        bbox = diff.getbbox()
        return bbox is not None and rgb.width > 1 and rgb.height > 1
    except Exception:
        return False


def _write_png_atomically(target: Path, image_bytes: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_target = target.with_name(f".{target.name}.tmp")
    temp_target.write_bytes(image_bytes)
    temp_target.replace(target)
    return target


def render_table_png(
    headers: list[str],
    rows: list[list[str]],
    *,
    max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX,
    include_header: bool = True,
) -> bytes:
    table_rows = [headers, *rows] if include_header and headers else rows
    image_bytes, _, _ = _render_table_image(
        table_rows,
        max_width_px=max_width_px,
        header_row_count=1 if include_header and headers else 0,
    )
    return image_bytes.getvalue()


def render_text_snapshot_png(text: str, *, max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX) -> bytes:
    image_bytes, _, _ = _render_text_snapshot(text, max_width_px=max_width_px)
    return image_bytes.getvalue()


def _entry_source_text(row: dict[str, Any]) -> str:
    return str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "").strip()


def _entry_anchor_context_text(row: dict[str, Any], *, from_end: bool) -> str:
    if str(row.get("entry_kind") or "").strip().upper() == "HEADING":
        heading_context = _heading_anchor_context_text(row)
        if heading_context:
            return heading_context
    blocks = _normalize_content_blocks(row.get("content_blocks"))
    paragraph_texts = [
        _clean_paragraph_block_text(str(block.get("text") or "").strip())
        for block in blocks
        if str(block.get("kind") or "").strip().lower() == "paragraph"
    ]
    paragraph_texts = [text for text in paragraph_texts if text]
    if paragraph_texts:
        return paragraph_texts[-1] if from_end else paragraph_texts[0]

    table_refs = _normalize_table_refs(row.get("table_refs"))
    if table_refs:
        ref = table_refs[-1] if from_end else table_refs[0]
        title = str(ref.get("table_title") or "").strip()
        if title:
            return title
    return _entry_source_text(row)


def _heading_anchor_context_text(row: dict[str, Any]) -> str:
    title = str(
        row.get("section_title")
        or row.get("display_text")
        or row.get("requirement_text")
        or row.get("raw_text")
        or ""
    ).strip()
    code = str(row.get("section_code") or "").strip()
    if not code:
        return title
    title_normalized = _normalize_match_text(title)
    code_normalized = _normalize_match_text(code)
    if title_normalized.startswith(code_normalized):
        return title
    return f"{code} {title}".strip()


def _refresh_enriched_entry_text_fields(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    materialized = _materialize_requirement_text(copied)
    if not materialized:
        return copied
    original_raw = str(copied.get("raw_text") or "").strip()
    copied["display_text"] = materialized
    copied["requirement_text"] = materialized
    copied["raw_text"] = original_raw or materialized
    copied["text_length"] = len(materialized)
    return copied


def _materialize_requirement_text(row: dict[str, Any]) -> str:
    blocks = _normalize_content_blocks(row.get("content_blocks"))
    if not blocks:
        return _clean_paragraph_block_text(_entry_source_text(row))

    table_ref_map = {
        str(ref.get("table_id") or "").strip(): ref
        for ref in _normalize_table_refs(row.get("table_refs"))
        if str(ref.get("table_id") or "").strip()
    }
    materialized_parts: list[str] = []
    for block_index, block in enumerate(blocks):
        kind = str(block.get("kind") or "").strip().lower()
        if kind == "paragraph":
            text = _clean_paragraph_block_text(str(block.get("text") or "").strip())
            if text:
                _append_materialized_requirement_part(materialized_parts, text)
            continue
        if kind != "table":
            continue
        table_id = str(block.get("table_id") or "").strip()
        table_ref = table_ref_map.get(table_id)
        table_text = _materialize_table_block_text(table_ref)
        if _should_skip_materialized_table_text(
            blocks,
            block_index=block_index,
            table_ref=table_ref,
            table_text=table_text,
        ):
            continue
        if table_text:
            _append_materialized_requirement_part(materialized_parts, table_text)
    if not materialized_parts:
        return _clean_paragraph_block_text(_entry_source_text(row))
    return "\n".join(materialized_parts).strip()


def _append_materialized_requirement_part(parts: list[str], text: str) -> None:
    value = str(text or "").strip()
    if not value:
        return
    normalized = _normalize_match_text(value)
    if parts and normalized and _normalize_match_text(parts[-1]) == normalized:
        return
    parts.append(value)


def _should_skip_materialized_table_text(
    blocks: list[dict[str, Any]],
    *,
    block_index: int,
    table_ref: dict[str, Any] | None,
    table_text: str,
) -> bool:
    if not table_text or not isinstance(table_ref, dict):
        return False
    if "|" in table_text:
        return False
    preferred_variant = _normalize_export_variant(
        table_ref.get("recommended_export_variant") or table_ref.get("export_variant")
    )
    render_mode = str(table_ref.get("slice_render_mode") or table_ref.get("render_mode") or "").strip().lower()
    has_source_snapshot = bool(str(table_ref.get("source_snapshot_path") or "").strip())
    if preferred_variant != "source" and render_mode != "snapshot_only" and not has_source_snapshot:
        return False
    if block_index <= 0:
        return False
    previous_block = blocks[block_index - 1]
    if str(previous_block.get("kind") or "").strip().lower() != "paragraph":
        return False
    previous_text = _clean_paragraph_block_text(str(previous_block.get("text") or "").strip())
    if not previous_text:
        return False
    previous_norm = _normalize_match_text(previous_text)
    table_norm = _normalize_match_text(table_text)
    if not previous_norm or not table_norm:
        return False
    if table_norm in previous_norm or previous_norm in table_norm:
        return True
    previous_label = next((line.strip() for line in previous_text.splitlines() if line.strip()), previous_text)
    previous_lines = [line.strip() for line in previous_text.splitlines() if line.strip()]
    table_label_like = "/" not in table_text and (
        _looks_like_local_clause_heading(table_text)
        or _looks_like_headingish_single_cell(table_text)
        or _looks_like_plain_heading_label(table_text)
        or _looks_like_attachment_heading_anchor(table_text)
    )
    return (
        table_label_like
        and (
            _looks_like_local_clause_heading(previous_label)
            or _looks_like_headingish_single_cell(previous_label)
            or _looks_like_plain_heading_label(previous_label)
            or _looks_like_attachment_heading_anchor(previous_label)
            or len(previous_lines) <= 2
        )
    )


def _materialize_table_block_text(ref: dict[str, Any] | None) -> str:
    if not isinstance(ref, dict):
        return ""
    render_mode = str(ref.get("slice_render_mode") or ref.get("render_mode") or "").strip().lower()
    preferred_variant = _normalize_export_variant(
        ref.get("recommended_export_variant") or ref.get("export_variant")
    )
    has_source_snapshot = bool(str(ref.get("source_snapshot_path") or ref.get("source_snapshot_url") or "").strip())
    notes = [
        str(item or "").strip()
        for item in (ref.get("inherited_notes") or ref.get("notes") or [])
        if str(item or "").strip()
    ]
    table_title = str(ref.get("table_title") or "").strip()
    headers = _sanitize_table_text_cells(ref.get("inherited_headers") or ref.get("headers") or [])
    rows = _sanitize_table_text_rows(
        ref.get("slice_rows") or ref.get("rows") or [],
        expected_columns=len(headers),
    )
    rows = _drop_materialized_header_echo_rows(rows, headers)
    if _should_omit_materialized_table_text(
        headers,
        rows,
        render_mode=render_mode,
        notes=notes,
        preferred_variant=preferred_variant,
        has_source_snapshot=has_source_snapshot,
    ):
        return _build_materialized_table_surrogate_text(
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
        )

    lines: list[str] = []
    if headers:
        lines.append(_format_materialized_table_row(headers))
    for row in rows:
        lines.append(_format_materialized_table_row(row))
    return "\n".join(line for line in lines if line).strip()


def _sanitize_table_text_rows(rows: Any, *, expected_columns: int) -> list[list[str]]:
    if not isinstance(rows, list):
        return []
    sanitized: list[list[str]] = []
    column_count = max(
        expected_columns,
        max((len(row) for row in rows if isinstance(row, list)), default=0),
    )
    for row in rows:
        if not isinstance(row, list):
            continue
        normalized = _sanitize_table_text_cells(row, expected_columns=column_count)
        if _row_is_materialized_table_noise(normalized):
            continue
        sanitized.append(normalized)
    return sanitized


def _sanitize_table_text_cells(cells: Any, *, expected_columns: int = 0) -> list[str]:
    normalized: list[str] = []
    if isinstance(cells, list):
        normalized = [_sanitize_table_text_cell(cell) for cell in cells]
    if expected_columns > 0:
        normalized = (normalized + [""] * expected_columns)[:expected_columns]
    while normalized and not normalized[-1]:
        normalized.pop()
    return normalized


def _sanitize_table_text_cell(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if re.fullmatch(r"col\d+", text, re.I):
        return ""
    if re.fullmatch(r"#?(?:ref|value|name|n/?a)!?", text, re.I):
        return ""
    return text


def _row_is_materialized_table_noise(row: list[str]) -> bool:
    if not row:
        return True
    meaningful = [cell for cell in row if cell]
    if not meaningful:
        return True
    return all(not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", cell) for cell in meaningful)


def _drop_materialized_header_echo_rows(rows: list[list[str]], headers: list[str]) -> list[list[str]]:
    if not rows or not headers:
        return rows
    normalized_headers = [str(cell or "").strip() for cell in headers]
    trimmed_rows = list(rows)
    while trimmed_rows:
        first_row = [str(cell or "").strip() for cell in trimmed_rows[0]]
        if first_row != normalized_headers[: len(first_row)]:
            break
        trimmed_rows.pop(0)
    return trimmed_rows


def _should_omit_materialized_table_text(
    headers: list[str],
    rows: list[list[str]],
    *,
    render_mode: str,
    notes: list[str],
    preferred_variant: str,
    has_source_snapshot: bool,
) -> bool:
    if not headers and not rows:
        return True
    if render_mode != "structured":
        return True
    if has_source_snapshot and preferred_variant == "source":
        return True
    if has_source_snapshot and preferred_variant == "slice" and _looks_like_large_source_backed_slice_table(headers, rows):
        return True
    if not rows and not any(headers):
        return True
    if _structured_table_looks_low_quality(headers, rows):
        return True
    if _looks_like_complex_merged_table(rows, notes):
        return True
    if _looks_like_poor_slice_readability(headers, rows):
        return True
    return max(len(headers), max((len(row) for row in rows), default=0)) >= 8


def _looks_like_large_source_backed_slice_table(headers: list[str], rows: list[list[str]]) -> bool:
    column_count = max(len(headers), max((len(row) for row in rows), default=0))
    if column_count >= 5 and len(rows) >= 6:
        return True
    return len(rows) >= 10


def _build_materialized_table_surrogate_text(
    *,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
) -> str:
    title = str(table_title or "").strip()
    if title and not _looks_like_markdown_table_row(title):
        return title

    for note in notes:
        cleaned = _clean_paragraph_block_text(str(note or "").strip())
        if cleaned and not _looks_like_markdown_table_row(cleaned):
            return cleaned

    meaningful_headers = [
        str(cell or "").strip()
        for cell in headers
        if str(cell or "").strip() and not re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I)
    ]
    if meaningful_headers:
        return " / ".join(meaningful_headers[:3])

    if rows:
        first_row = [str(cell or "").strip() for cell in rows[0] if str(cell or "").strip()]
        if first_row:
            return " / ".join(first_row[:3])
    return ""


def _format_materialized_table_row(cells: list[str]) -> str:
    if not cells:
        return ""
    return "|" + "|".join(str(cell or "").strip() for cell in cells) + "|"


def _split_text_blocks(text: str) -> list[dict[str, Any]]:
    raw_lines = str(text or "").replace("\r\n", "\n").split("\n")
    lines: list[str] = []
    for raw_line in raw_lines:
        lines.extend(_normalize_block_input_line(raw_line))
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
            header_cells = _extract_explicit_header_from_paragraph(paragraph)
            flush_paragraph()
            table_lines = [lines[index]]
            index += 1
            while index < len(lines):
                if _looks_like_markdown_table_row(lines[index]):
                    table_lines.append(lines[index])
                    index += 1
                    continue
                if table_lines and _looks_like_table_bridge_line(lines, index):
                    table_lines[-1] = _append_continuation_to_table_line(table_lines[-1], lines[index])
                    index += 1
                    continue
                break
            table_text = "\n".join(line for line in table_lines if str(line).strip()).strip()
            blocks.append({"kind": "table", "text": table_text, "lines": table_lines, "header_cells": header_cells})
            continue
        paragraph.append(lines[index])
        index += 1
    flush_paragraph()
    return _merge_header_stub_blocks(blocks)


def _merge_header_stub_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if len(blocks) < 3:
        return blocks
    merged: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if (
            index + 2 < len(blocks)
            and str(block.get("kind") or "").strip().lower() == "table"
            and str(blocks[index + 1].get("kind") or "").strip().lower() == "paragraph"
            and str(blocks[index + 2].get("kind") or "").strip().lower() == "table"
        ):
            header_cells = block.get("header_cells") or []
            if not header_cells:
                table_lines = block.get("lines") or []
                if len(table_lines) == 1:
                    header_cells = _parse_markdown_cells(table_lines[0])
            heading_text = str(blocks[index + 1].get("text") or "").strip()
            if (
                _looks_like_header_stub_row(header_cells)
                and _looks_like_plain_heading_label(heading_text)
                and not (blocks[index + 2].get("header_cells") or [])
            ):
                merged.append(blocks[index + 1])
                merged.append({**blocks[index + 2], "header_cells": header_cells})
                index += 3
                continue
        merged.append(block)
        index += 1
    return merged


def _looks_like_header_stub_row(cells: list[str]) -> bool:
    normalized = [str(cell or "").strip() for cell in cells if str(cell or "").strip()]
    if len(normalized) < 3:
        return False
    if any(len(cell) > 24 for cell in normalized):
        return False
    return any(
        re.search(r"(?:序号|项目|要求|测试项目|测试工况|评价和验收)", cell, re.I)
        for cell in normalized
    )


def _starts_table_block(lines: list[str], index: int) -> bool:
    if index >= len(lines):
        return False
    if _looks_like_local_clause_heading(str(lines[index] or "").strip()):
        return False
    return _looks_like_markdown_table_row(lines[index])


def _extract_structured_table(lines: list[str], *, explicit_header: list[str] | None = None) -> tuple[list[str], list[list[str]], str]:
    parsed = _parse_markdown_table(lines)
    if not parsed:
        parsed = _parse_pipe_table(lines, explicit_header=explicit_header)
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


def _parse_pipe_table(lines: list[str], *, explicit_header: list[str] | None = None) -> list[list[str]]:
    rows = [_parse_markdown_cells(line) for line in lines if _looks_like_markdown_table_row(line)]
    if not rows:
        return []
    expected_columns = max(len(explicit_header or []), max(len(row) for row in rows))
    if expected_columns < 2:
        return []
    allow_sparse_explicit = bool(explicit_header) and _looks_like_reusable_matrix_header(explicit_header or [])
    normalized: list[list[str]] = []
    mismatched = 0
    for row in rows:
        normalized_row = _normalize_pipe_table_row(
            row,
            expected_columns=expected_columns,
            allow_sparse_explicit=allow_sparse_explicit,
        )
        if normalized_row is not None:
            normalized.append(normalized_row)
            if not allow_sparse_explicit and normalized_row != [str(cell or "").strip() for cell in row]:
                mismatched += 1
            continue
        mismatched += 1
    if not normalized or mismatched > max(1, len(rows) // 2):
        return []
    if explicit_header:
        if len(explicit_header) != expected_columns:
            return []
        return [explicit_header, *normalized]
    if len(normalized) < 2:
        return []
    return normalized


def _normalize_pipe_table_row(
    row: list[str],
    *,
    expected_columns: int,
    allow_sparse_explicit: bool,
) -> list[str] | None:
    cells = [str(cell or "").strip() for cell in row]
    if not cells:
        return None
    if allow_sparse_explicit:
        cells = ["" if re.fullmatch(r"col\d+", cell, re.I) else cell for cell in cells]
        if _is_sparse_matrix_noise_row(cells):
            return None
        meaningful = [cell for cell in cells if cell]
        text_cells = [
            cell
            for cell in meaningful
            if cell not in {"●", "○", "-", "/", "N/A"} and re.search(r"[A-Za-z\u4e00-\u9fff0-9]", cell)
        ]
        has_long_text = any(len(cell) >= 12 for cell in text_cells)
        if not text_cells and not has_long_text:
            return None
        if len(cells) > expected_columns:
            cells = cells[:expected_columns]
        return (cells + [""] * expected_columns)[:expected_columns]
    if len(cells) == expected_columns:
        return cells
    if abs(len(cells) - expected_columns) <= 1 and expected_columns >= 3:
        return (cells + [""] * expected_columns)[:expected_columns]
    return None


def _is_sparse_matrix_noise_row(cells: list[str]) -> bool:
    meaningful = [cell for cell in cells if cell]
    if not meaningful:
        return True
    text_cells = [
        cell
        for cell in meaningful
        if cell not in {"●", "○", "-", "/", "N/A"} and re.search(r"[A-Za-z\u4e00-\u9fff0-9]", cell)
    ]
    return not text_cells


def _looks_like_reusable_matrix_header(headers: list[str]) -> bool:
    normalized = [str(cell or "").strip() for cell in headers if str(cell or "").strip()]
    if len(normalized) < 6:
        return False
    header_text = " ".join(normalized)
    keyword_hits = sum(
        1
        for pattern in (
            r"applicable",
            r"confidential",
            r"工作产出",
            r"保密",
            r"生命周期",
            r"lifecycle",
            r"activity",
            r"阶段",
            r"\b(?:R|A|S|I|C)\b",
        )
        if re.search(pattern, header_text, re.I)
    )
    return keyword_hits >= 2


def _looks_like_sparse_matrix_requirement_text(text: str) -> bool:
    lines = [str(line or "").strip() for line in str(text or "").splitlines() if str(line or "").strip()]
    table_lines = [line for line in lines if _looks_like_markdown_table_row(line)]
    if not table_lines:
        return False
    wide_rows = sum(1 for line in table_lines if len(_parse_markdown_cells(line)) >= 4)
    if wide_rows <= 0:
        return False
    joined = "\n".join(table_lines)
    if re.search(r"(?:适用|Applicable|confidential|机密|工作产出|生命周期|lifecycle|phase|●)", joined, re.I):
        return True
    return False


def _resolve_table_block_header(
    *,
    lines: list[str],
    explicit_header: list[str],
    inherited_header: list[str],
    notes: list[str],
    row: dict[str, Any] | None = None,
    table_title: str = "",
) -> list[str]:
    if explicit_header:
        return [str(cell or "").strip() for cell in explicit_header if str(cell or "").strip()]
    if inherited_header and _looks_like_sparse_matrix_requirement_text("\n".join(lines)):
        return [str(cell or "").strip() for cell in inherited_header if str(cell or "").strip()]
    contextual = _infer_contextual_single_row_header(
        lines=lines,
        row=row or {},
        table_title=table_title,
    )
    if contextual:
        return contextual
    inferred = _infer_explicit_header_from_block_notes(lines, notes)
    if inferred:
        return inferred
    return []


def _resolve_table_block_title(
    *,
    blocks: list[dict[str, Any]],
    block_index: int,
    fallback_title: str,
) -> str:
    for neighbor_index in (block_index - 1,):
        if neighbor_index < 0 or neighbor_index >= len(blocks):
            continue
        neighbor = blocks[neighbor_index]
        if str(neighbor.get("kind") or "").strip().lower() != "paragraph":
            continue
        candidate = _extract_heading_candidate_from_paragraph(str(neighbor.get("text") or "").strip())
        if candidate:
            return candidate
    return str(fallback_title or "").strip()


def _extract_heading_candidate_from_paragraph(text: str) -> str:
    cleaned = _clean_paragraph_block_text(str(text or "").strip())
    lines = [str(line or "").strip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    for line in reversed(lines):
        if _looks_like_reference_only_line(line):
            continue
        if re.fullmatch(r"col\d+", line, re.I):
            continue
        if _looks_like_generic_compact_band_header(line) or _is_generic_table_anchor_text(line):
            continue
        if (
            _looks_like_local_clause_heading(line)
            or _looks_like_headingish_single_cell(line)
            or _looks_like_plain_heading_label(line)
            or _looks_like_attachment_heading_anchor(line)
            or _looks_like_attachment_table_title(line)
        ):
            return line
        normalized = _normalize_match_text(line)
        if 4 <= len(normalized) <= 40 and len(line) <= 40 and not re.search(r"[。；;!?！？]$", line):
            return line
    return ""


def _infer_explicit_header_from_block_notes(lines: list[str], notes: list[str]) -> list[str]:
    widest_row = _widest_pipe_row_width("\n".join(lines))
    if widest_row <= 0:
        return []
    note_lines = [str(line or "").strip() for note in notes for line in str(note or "").splitlines() if str(line or "").strip()]
    note_text = "\n".join(note_lines)
    if widest_row == 2:
        if re.search(r"(?:工作产出保密性级别|confidentiality level)", note_text, re.I):
            return ["工作产出保密性级别", "说明"]
        if re.search(r"rasic", note_text, re.I):
            return ["RASIC 定义", "说明"]
        if note_lines:
            label = note_lines[-1]
            if len(label) <= 40 and not re.match(r"^ref[:：]", label, re.I):
                return [label, "说明"]
        return ["项目", "内容"]
    if widest_row == 3 and re.search(r"(?:备注|note)", note_text, re.I):
        return ["项目", "内容", "备注"]
    return []


def _infer_contextual_single_row_header(
    *,
    lines: list[str],
    row: dict[str, Any],
    table_title: str,
) -> list[str]:
    rows = [_parse_markdown_cells(line) for line in lines if _looks_like_markdown_table_row(line)]
    if len(rows) != 1:
        return []
    cells = [str(cell or "").strip() for cell in rows[0]]
    if len(cells) < 2:
        return []

    appendix_scope = normalize_scope_id(str(row.get("appendix_scope") or ""))
    appendix_title = str(row.get("appendix_title") or "").strip()
    parent_code = str(row.get("parent_section_code") or "").strip().upper()
    context_text = " ".join(
        str(value or "").strip()
        for value in (
            appendix_title,
            table_title,
            row.get("table_title"),
            row.get("parent_section_title"),
            row.get("parent_section_label"),
            row.get("section_title"),
            row.get("source_anchor"),
        )
        if str(value or "").strip()
    )
    if (
        is_test_outline_scope_value(appendix_scope)
        or is_test_outline_scope_value(appendix_title)
        or is_test_outline_scope_value(parent_code.split(".", 1)[0])
        or "试验大纲" in context_text
        or "匹配评估表" in context_text
        or "测试分组" in context_text
    ):
        return _infer_test_outline_single_row_headers(cells)
    return []


def _infer_test_outline_single_row_headers(cells: list[str]) -> list[str]:
    if not cells:
        return []
    first = str(cells[0] or "").strip()
    normalized_tokens = [str(cell or "").strip().upper() for cell in cells]
    looks_like_matrix_row = bool(re.fullmatch(r"\d+[A-Za-z（）()\-]*", first)) or any(
        token in {"DV", "PV", "DV/PV"} for token in normalized_tokens
    )
    if not looks_like_matrix_row:
        return []

    cell_count = len(cells)
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
    return []


def _widest_pipe_row_width(text: str) -> int:
    widths = []
    for line in str(text or "").splitlines():
        if not _looks_like_markdown_table_row(line):
            continue
        cells = [
            cell
            for cell in _parse_markdown_cells(line)
            if not re.fullmatch(r"col\d+", str(cell or "").strip(), re.I)
        ]
        widths.append(len(cells))
    return max(widths, default=0)


def _find_recent_reusable_table_header(
    row: dict[str, Any],
    header_contexts: list[dict[str, Any]],
) -> list[str]:
    if not header_contexts:
        return []
    text = _entry_source_text(row)
    if not _looks_like_sparse_matrix_requirement_text(text):
        return []
    appendix_scope = str(row.get("appendix_scope") or "").strip().upper()
    source_page = _coerce_int(row.get("source_page"))
    widest_row = _widest_pipe_row_width(text)
    for context in reversed(header_contexts):
        if str(context.get("appendix_scope") or "").strip().upper() != appendix_scope:
            continue
        context_end = _coerce_int(context.get("page_end"))
        if source_page > 0 and context_end > 0 and source_page > context_end + 2:
            continue
        headers = [str(cell or "").strip() for cell in context.get("headers") or [] if str(cell or "").strip()]
        if not _looks_like_reusable_matrix_header(headers):
            continue
        if widest_row > 0 and len(headers) + 1 < widest_row:
            continue
        return headers
    return []


def _remember_reusable_table_headers(
    header_contexts: list[dict[str, Any]],
    row: dict[str, Any],
    row_assets: list[dict[str, Any]],
) -> None:
    appendix_scope = str(row.get("appendix_scope") or "").strip().upper()
    for asset in row_assets:
        headers = [str(cell or "").strip() for cell in asset.get("headers") or [] if str(cell or "").strip()]
        if str(asset.get("render_mode") or "") != "structured" or not _looks_like_reusable_matrix_header(headers):
            continue
        page_span = asset.get("page_span") or {}
        header_contexts.append(
            {
                "appendix_scope": appendix_scope,
                "page_start": _coerce_int(page_span.get("start")),
                "page_end": _coerce_int(page_span.get("end")) or _coerce_int(page_span.get("start")),
                "headers": headers,
            }
        )
    if len(header_contexts) > 16:
        del header_contexts[:-16]


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


def _normalize_block_input_line(line: str) -> list[str]:
    value = str(line or "").strip()
    if not value:
        return [str(line or "")]
    if "|" in value:
        single_cells = _parse_markdown_cells(value)
        if len(single_cells) == 1:
            return [single_cells[0]]
    if not _looks_like_markdown_table_row(value):
        return [value]

    cells = _parse_markdown_cells(value)
    if not cells:
        return [value]

    if _should_preserve_pipe_row_layout(cells):
        return ["|" + "|".join(str(cell or "").strip() for cell in cells) + "|"]

    if _looks_like_placeholder_only_row(cells):
        pieces = _split_placeholder_title_row(cells[0])
        return pieces or []

    collapsed = _collapse_repeated_adjacent_cells(cells)
    if len(collapsed) == 1:
        return [collapsed[0]]

    return ["|" + "|".join(collapsed) + "|"]


def _is_markdown_separator_row(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", str(cell or "").strip()) for cell in cells)


def _extract_explicit_header_from_paragraph(paragraph: list[str]) -> list[str]:
    if not paragraph:
        return []
    while paragraph and not str(paragraph[-1]).strip():
        paragraph.pop()
    if not paragraph:
        return []
    candidate = str(paragraph[-1] or "").strip()
    headers = _match_explicit_header_line(candidate)
    if headers:
        paragraph.pop()
        return headers
    return []


def _match_explicit_header_line(line: str) -> list[str]:
    text = re.sub(r"\s+", "", str(line or ""))
    if not text:
        return []
    patterns = [
        (["序号", "层级", "代号", "名称", "重量（kg）", "材料（主要）", "数量", "开发状态"], ["重量(kg)", "重量（kg）"]),
        (["序号", "层级", "代号", "版本", "名称", "重量（kg）", "数量", "开发状态"], ["重量(kg)", "重量（kg）"]),
        (["序号", "项目", "要求"], []),
        (["序号", "项目", "要求", "备注"], []),
        (["序号", "项目", "图示", "要求", "备注"], []),
        (["序号", "项目", "图示", "要求"], []),
        (["序号", "缩略语", "英文全称", "中文全称"], []),
        (["序号", "功能定义", "描述"], []),
        (["序号", "定义", "描述", "备注"], []),
    ]
    for base_headers, aliases in patterns:
        candidate_headers = list(base_headers)
        variants = {"".join(base_headers)}
        for alias in aliases:
            alt = [alias if header in {"重量（kg）", "重量(kg)"} else header for header in base_headers]
            variants.add("".join(alt))
            candidate_headers = alt
        if text in variants:
            return candidate_headers
    return []


def _looks_like_table_bridge_line(lines: list[str], index: int) -> bool:
    if index < 0 or index >= len(lines):
        return False
    line = str(lines[index] or "").strip()
    if not line or "|" in line:
        return False
    if index + 1 >= len(lines) or not _looks_like_markdown_table_row(lines[index + 1]):
        return False
    if len(line) > 80:
        return False
    if _looks_like_new_table_title_band_line(line):
        return False
    if re.match(r"^(备注|注[:：]|\d+(?:\.\d+){0,5}[)）、\.]?\s)", line):
        return False
    return True


def _looks_like_new_table_title_band_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value or "|" in value:
        return False
    if len(value) > 64:
        return False
    if re.search(r"\d", value):
        return False
    if re.search(r"[。！？!?；;]$", value):
        return False
    if re.match(r"^(?:ref[:：]|备注|note[:：])", value, re.I):
        return True
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]{3,}", value))


def _append_continuation_to_table_line(line: str, continuation: str) -> str:
    cells = _parse_markdown_cells(line)
    if len(cells) < 2:
        return line
    target_index = _pick_table_continuation_target(cells)
    merged = list(cells)
    suffix = str(continuation or "").strip()
    merged[target_index] = f"{str(merged[target_index] or '').rstrip()} {suffix}".strip()
    return "|" + "|".join(merged) + "|"


def _looks_like_placeholder_only_row(cells: list[str]) -> bool:
    if len(cells) < 2:
        return False
    return all(re.fullmatch(r"col\d+", str(cell or "").strip(), re.I) for cell in cells[1:])


def _split_placeholder_title_row(first_cell: str) -> list[str]:
    text = str(first_cell or "").strip()
    if not text:
        return []
    match = re.search(r"((?:\d+(?:\.\d+)*)[）)])\s*([^\n]+)$", text)
    if match:
        prefix = text[: match.start()].strip()
        heading = f"{match.group(1)} {match.group(2).strip()}".strip()
        pieces: list[str] = []
        if prefix:
            pieces.append(prefix)
        if heading:
            pieces.append(heading)
        return pieces
    return [text]


def _should_preserve_pipe_row_layout(cells: list[str]) -> bool:
    normalized = [str(cell or "").strip() for cell in cells]
    if len(normalized) < 8:
        return False
    meaningful = [cell for cell in normalized if cell]
    if len(meaningful) < 3:
        return False
    if any(re.fullmatch(r"col\d+", cell, re.I) for cell in meaningful):
        return True
    if sum(1 for cell in normalized if not cell) >= 2:
        return True
    if len(normalized) < 12:
        return False
    short_counts: dict[str, int] = {}
    for cell in meaningful:
        if len(cell) > 20:
            continue
        key = re.sub(r"\s+", " ", cell).strip().lower()
        if not key:
            continue
        short_counts[key] = short_counts.get(key, 0) + 1
        if short_counts[key] >= 3:
            return True
    return False


def _collapse_repeated_adjacent_cells(cells: list[str]) -> list[str]:
    collapsed: list[str] = []
    for raw in cells:
        value = str(raw or "").strip()
        if not value:
            continue
        if collapsed and _cell_equivalent_for_collapse(value, collapsed[-1]):
            continue
        collapsed.append(value)
    return collapsed or [str(cell or "").strip() for cell in cells]


def _cell_equivalent_for_collapse(left: str, right: str) -> bool:
    a = re.sub(r"\s+", "", str(left or ""))
    b = re.sub(r"\s+", "", str(right or ""))
    if not a or not b:
        return False
    if a in {"/", "-", "—"} or b in {"/", "-", "—"}:
        return False
    if re.fullmatch(r":?-{2,}:?", a) or re.fullmatch(r":?-{2,}:?", b):
        return False
    if a == b:
        return True
    if re.fullmatch(r"col\d+", a, re.I) and re.fullmatch(r"col\d+", b, re.I):
        return True
    return False


def _looks_like_local_clause_heading(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    dot_style = False
    match = re.match(r"^\s*(\d+(?:\.\d+)*)\s*[）)]\s*(.+)$", value)
    if not match:
        match = re.match(r"^\s*(\d+)\s*[\.．、]\s*(.+)$", value)
        dot_style = match is not None
    if not match:
        return False
    title = str(match.group(2) or "").strip()
    if not title or len(title) > 140:
        return False
    if "|" in title:
        return False
    normalized_title = re.sub(r"[：:]\s*$", "", title).strip()
    if dot_style and (len(normalized_title or title) > 48 or re.search(r"[，,。；;：:]", normalized_title)):
        return False
    return True


def _looks_like_headingish_single_cell(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"[。！？!?]", value):
        return False
    if len(value) > 160:
        return False
    normalized_value = re.sub(r"[：:]\s*$", "", value).strip()
    if re.match(r"^\d+(?:\.\d+)*\s*[）)]", value):
        return True
    if re.match(r"^\d+\.\s*[A-Za-z\u4e00-\u9fff]", value):
        if len(normalized_value or value) > 56 or re.search(r"[，,；;：:]", normalized_value):
            return False
        return True
    if re.match(r"^\d+(?:\.\d+){1,5}\s+", value):
        return True
    return False


def _looks_like_plain_heading_label(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if len(value) > 48:
        return False
    if "|" in value or re.search(r"[。！？!?；;:：]", value):
        return False
    if value.count(" ") > 4:
        return False
    return bool(re.search(r"[\u4e00-\u9fffA-Za-z]", value))


def _looks_like_trailing_table_context_noise(text: str, *, next_entry_text: str) -> bool:
    lines = _iter_table_raw_lines(text)
    if not lines or len(lines) > 3:
        return False

    joined = "".join(lines)
    if next_entry_text and re.fullmatch(r"#?(?:ref|value|name)!?", joined.strip(), re.I):
        return True
    normalized = _normalize_match_text(joined)
    if len(normalized) < 4:
        return False

    next_lines = _iter_table_raw_lines(next_entry_text)
    next_norms = [_normalize_match_text(line) for line in next_lines[:4] if _normalize_match_text(line)]
    if next_norms and any(
        normalized == candidate or normalized in candidate or candidate in normalized
        for candidate in next_norms
        if len(candidate) >= 4
    ):
        return True

    if not next_entry_text or not all(_is_compact_band_line(line) for line in lines):
        return False

    header_tokens = ("序号", "内容", "备注", "位置", "附件名称", "页码", "名称")
    return len(joined) <= 24 and any(token in joined for token in header_tokens)


def _extract_local_clause_anchor_prefix(text: str) -> str:
    value = str(text or "").strip()
    match = re.match(r"^\s*(\d+(?:\.\d+)*)\s*([）)])\s*(.+)$", value)
    if not match:
        return ""
    number = str(match.group(1) or "").strip()
    suffix = str(match.group(2) or "）").strip() or "）"
    label = str(match.group(3) or "").strip()
    if not label:
        return ""
    label = re.split(r"[|（(：:—\-；;]", label, maxsplit=1)[0].strip()
    if not label:
        return ""
    return f"{number}{suffix}{label}"


def _pick_table_continuation_target(cells: list[str]) -> int:
    preferred_index = 0
    preferred_score = -1
    for index, cell in enumerate(cells):
        value = str(cell or "").strip()
        if not value or value in {"/", "-"}:
            continue
        score = _estimate_cell_visual_length(value)
        if index == 0:
            score -= 10
        if re.fullmatch(r"\d+", value):
            score -= 6
        if score > preferred_score:
            preferred_score = score
            preferred_index = index
    return preferred_index


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
    text = _entry_source_text(row)
    for line in str(text or "").splitlines():
        candidate = str(line or "").strip()
        if _looks_like_local_clause_title(candidate):
            return candidate
    source_anchor_title = _extract_table_title_from_source_anchor(str(row.get("source_anchor") or ""))
    if source_anchor_title:
        return source_anchor_title
    for key in ("table_title", "parent_section_label", "heading_text", "section_title"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def _ensure_leading_table_title_block(
    content_blocks: list[dict[str, Any]],
    *,
    row: dict[str, Any],
    table_title: str,
) -> list[dict[str, Any]]:
    if not content_blocks:
        return content_blocks
    if str(content_blocks[0].get("kind") or "").strip().lower() != "table":
        return content_blocks
    title = str(table_title or "").strip()
    if not title or _looks_like_markdown_table_row(title):
        return content_blocks
    if not (
        _looks_like_local_clause_title(title)
        or _looks_like_headingish_single_cell(title)
        or _looks_like_plain_heading_label(title)
        or _looks_like_attachment_table_title(title)
    ):
        return content_blocks
    return [{"kind": "paragraph", "text": title}, *content_blocks]


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
    if render_mode == "structured" and (headers or rows):
        image_bytes = render_table_png(headers, rows, include_header=bool(headers))
    else:
        image_bytes = render_text_snapshot_png(text)
    return _write_png_atomically(target, image_bytes)


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
    prefer_clause_window: bool = False,
    allow_next_clause_stop: bool = True,
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
                _SOURCE_SNAPSHOT_SIGNATURE_VERSION,
                "clause-window" if prefer_clause_window else "candidate-window",
                "next-clause-stop" if allow_next_clause_stop else "band-window",
                # Bump when anchor/crop heuristics change so stored source snapshots are regenerated.
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
        return (
            cached,
            _resolve_cached_source_snapshot_meta(
                source_path=source_path,
                source_page=source_page,
                page_end=page_end,
                table_text=table_text,
                table_title=table_title,
                headers=headers,
                rows=rows,
                notes=notes,
                source_table_detection_cache=source_table_detection_cache,
            )
            if cached
            else {},
        )

    snapshot_root.mkdir(parents=True, exist_ok=True)
    source_seed = "|".join([cache_key[0], str(source_page), str(page_end), snapshot_signature])
    target = snapshot_root / f"src_{hashlib.sha1(source_seed.encode('utf-8')).hexdigest()[:16]}.png"
    if _snapshot_file_is_usable(target):
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = target
        return target, _resolve_cached_source_snapshot_meta(
            source_path=source_path,
            source_page=source_page,
            page_end=page_end,
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
            source_table_detection_cache=source_table_detection_cache,
        )
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

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
            prefer_clause_window=prefer_clause_window,
            allow_next_clause_stop=allow_next_clause_stop,
        )
        if (
            not prefer_clause_window
            and _should_retry_source_snapshot_with_clause_window(
                rendered=rendered,
                render_meta=render_meta,
                table_title=table_title,
                headers=headers,
                rows=rows,
                notes=notes,
            )
        ):
            retried, retry_meta = _render_pdf_page_span_snapshot(
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
                prefer_clause_window=True,
                allow_next_clause_stop=allow_next_clause_stop,
            )
            if _source_snapshot_retry_is_better(current=rendered, retried=retried):
                rendered, render_meta = retried, retry_meta
        _write_png_atomically(target, rendered)
    except Exception:
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = None
        return None, {}

    if source_snapshot_cache is not None:
        source_snapshot_cache[cache_key] = target
    return target, render_meta


def _write_full_page_span_source_snapshot(
    *,
    snapshot_root: Path,
    source_file_path: str,
    source_page: int,
    source_page_end: int,
    cache_token: str,
    cache_key_hint: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None,
    crop_to_content: bool = True,
    max_height_px: int | None = None,
    max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX,
) -> tuple[Path | None, dict[str, Any]]:
    source_path = Path(str(source_file_path or "").strip())
    if fitz is None or not source_path.exists() or source_path.suffix.lower() != ".pdf":
        return None, {}
    if source_page <= 0:
        return None, {}
    page_end = source_page_end if source_page_end >= source_page else source_page
    signature = hashlib.sha1(
        "|".join(
            [
                _SOURCE_SNAPSHOT_SIGNATURE_VERSION,
                "full-page-span",
                cache_token,
                cache_key_hint,
            ]
        ).encode("utf-8")
    ).hexdigest()[:10]
    cache_key = (str(source_path.resolve()), source_page, page_end, signature)
    if source_snapshot_cache is not None and cache_key in source_snapshot_cache:
        return source_snapshot_cache[cache_key], {}

    snapshot_root.mkdir(parents=True, exist_ok=True)
    seed = "|".join([cache_key[0], str(cache_key[1]), str(cache_key[2]), cache_key[3]])
    target = snapshot_root / f"src_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}.png"
    if _snapshot_file_is_usable(target):
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = target
        return target, {"page_span_count": max(1, page_end - source_page + 1), "image_block_count": 0}
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    try:
        with fitz.open(str(source_path)) as document:
            if document.page_count <= 0:
                raise ValueError("source pdf has no pages")
            start_index = max(0, source_page - 1)
            end_index = min(document.page_count - 1, max(start_index, page_end - 1))
            page_indices = list(range(start_index, end_index + 1))
            plan = _build_full_page_span_crop_plan(document, page_indices) if crop_to_content else [(page_index, None) for page_index in page_indices]
            render_width = max(1, int(max_width_px or _SNAPSHOT_MAX_WIDTH_PX))
            images = _render_pdf_crop_plan(
                document,
                plan,
                max_width_px=render_width,
                max_scale=5.0,
            )
            rendered = _compose_vertical_images(
                images,
                max_width_px=render_width,
                max_height_px=max_height_px,
            )
            render_meta = _summarize_pdf_crop_plan_features(document, plan)
        _write_png_atomically(target, rendered)
    except Exception:
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = None
        return None, {}

    if source_snapshot_cache is not None:
        source_snapshot_cache[cache_key] = target
    return target, render_meta


def _source_snapshot_pixel_size(image_bytes: bytes) -> tuple[int, int]:
    try:
        with PILImage.open(BytesIO(image_bytes)) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _should_retry_source_snapshot_with_clause_window(
    *,
    rendered: bytes,
    render_meta: dict[str, Any],
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
) -> bool:
    width, height = _source_snapshot_pixel_size(rendered)
    if width <= 0 or height <= 0:
        return False
    heading_like = (
        _looks_like_local_clause_title(table_title)
        or _looks_like_headingish_single_cell(table_title)
        or _looks_like_plain_heading_label(table_title)
        or _looks_like_attachment_table_title(table_title)
    )
    page_span_count = _coerce_int(render_meta.get("page_span_count"))
    if page_span_count <= 0:
        page_span_count = 1
    complexish = (
        page_span_count > 1
        or len(headers) >= 5
        or len(rows) >= 3
        or _looks_like_complex_merged_table(rows, notes)
        or _looks_like_poor_slice_readability(headers, rows)
    )
    if not complexish:
        return False
    if _has_explicit_numbered_row_slice_label(rows) and page_span_count <= 1:
        return False
    if page_span_count <= 1 and _looks_like_labeled_info_source_first_table(headers, rows):
        return False
    if not heading_like and len(headers) < 6 and page_span_count <= 1:
        return False
    min_height = max(120, int(width * 0.18))
    return height <= min_height


def _source_snapshot_retry_is_better(*, current: bytes, retried: bytes) -> bool:
    current_width, current_height = _source_snapshot_pixel_size(current)
    retried_width, retried_height = _source_snapshot_pixel_size(retried)
    if retried_width <= 0 or retried_height <= 0:
        return False
    if current_width <= 0 or current_height <= 0:
        return True
    return retried_height >= current_height + 30 or (retried_width * retried_height) >= int(
        current_width * current_height * 1.35
    )


def _read_png_dimensions(path: str) -> tuple[int, int]:
    target = Path(str(path or "").strip())
    if not target.exists():
        return (0, 0)
    try:
        with PILImage.open(target) as image:
            return int(image.width), int(image.height)
    except Exception:
        return (0, 0)


def _prune_duplicate_shallow_snapshot_tables(
    content_blocks: list[dict[str, Any]],
    table_refs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    if not content_blocks or not table_refs:
        return content_blocks, table_refs, set()

    drop_ids: set[str] = set()
    title_to_refs: dict[str, list[dict[str, Any]]] = {}
    for ref in table_refs:
        table_id = str(ref.get("table_id") or "").strip()
        title = _normalize_match_text(str(ref.get("table_title") or ""))
        if not table_id or not title:
            continue
        title_to_refs.setdefault(title, []).append(ref)

    for ref in table_refs:
        table_id = str(ref.get("table_id") or "").strip()
        title = _normalize_match_text(str(ref.get("table_title") or ""))
        if not table_id or not title:
            continue
        if str(ref.get("slice_render_mode") or "").strip().lower() != "snapshot_only":
            continue
        if ref.get("inherited_headers") or ref.get("headers") or ref.get("slice_rows") or ref.get("rows"):
            continue
        _, height = _read_png_dimensions(str(ref.get("source_snapshot_path") or ""))
        if height <= 0 or height > 80:
            continue
        siblings = [
            item
            for item in title_to_refs.get(title, [])
            if str(item.get("table_id") or "").strip() != table_id
            and (
                str(item.get("slice_render_mode") or "").strip().lower() == "structured"
                or bool(item.get("slice_rows") or item.get("rows"))
            )
        ]
        if siblings:
            drop_ids.add(table_id)

    if not drop_ids:
        return content_blocks, table_refs, set()

    cleaned_blocks = [
        block
        for block in content_blocks
        if not (
            str(block.get("kind") or "").strip().lower() == "table"
            and str(block.get("table_id") or "").strip() in drop_ids
        )
    ]
    cleaned_refs = [ref for ref in table_refs if str(ref.get("table_id") or "").strip() not in drop_ids]
    return cleaned_blocks, cleaned_refs, drop_ids


def _prune_redundant_lead_source_tables(
    content_blocks: list[dict[str, Any]],
    table_refs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    if len(content_blocks) < 2 or len(table_refs) < 2:
        return content_blocks, table_refs, set()

    ref_map = {
        str(ref.get("table_id") or "").strip(): ref
        for ref in table_refs
        if str(ref.get("table_id") or "").strip()
    }
    ordered_ids = [
        str(block.get("table_id") or "").strip()
        for block in content_blocks
        if str(block.get("kind") or "").strip().lower() == "table" and str(block.get("table_id") or "").strip()
    ]
    if len(ordered_ids) < 2:
        return content_blocks, table_refs, set()

    drop_ids: set[str] = set()
    for index, table_id in enumerate(ordered_ids[:-1]):
        ref = ref_map.get(table_id)
        if not isinstance(ref, dict):
            continue
        if str(ref.get("slice_render_mode") or "").strip().lower() != "structured":
            continue
        if _normalize_export_variant(ref.get("recommended_export_variant") or ref.get("export_variant")) != "source":
            continue
        title = _normalize_match_text(str(ref.get("table_title") or ""))
        if not title:
            continue
        headers = _sanitize_table_text_cells(ref.get("inherited_headers") or ref.get("headers") or [])
        rows = _sanitize_table_text_rows(ref.get("slice_rows") or ref.get("rows") or [], expected_columns=len(headers))
        rows = _drop_materialized_header_echo_rows(rows, headers)
        mismatched_row_width = any(len(row) != len(headers) for row in rows)
        if len(rows) > 1 or not (_structured_table_looks_low_quality(headers, rows) or mismatched_row_width):
            continue
        if not any(
            _normalize_match_text(str(ref_map.get(later_id, {}).get("table_title") or "")) == title
            and _normalize_export_variant(
                ref_map.get(later_id, {}).get("recommended_export_variant")
                or ref_map.get(later_id, {}).get("export_variant")
            )
            == "source"
            for later_id in ordered_ids[index + 1 :]
        ):
            continue
        drop_ids.add(table_id)

    if not drop_ids:
        return content_blocks, table_refs, set()

    cleaned_blocks = [
        block
        for block in content_blocks
        if not (
            str(block.get("kind") or "").strip().lower() == "table"
            and str(block.get("table_id") or "").strip() in drop_ids
        )
    ]
    cleaned_refs = [ref for ref in table_refs if str(ref.get("table_id") or "").strip() not in drop_ids]
    return cleaned_blocks, cleaned_refs, drop_ids


def _recover_table_from_pdf_source(
    *,
    source_file_path: str,
    source_page: int,
    source_page_end: int,
    table_text: str,
    table_title: str,
    current_headers: list[str],
    current_rows: list[list[str]],
    notes: list[str],
    before_context_text: str,
    after_context_text: str,
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    source_line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> tuple[list[str], list[list[str]], list[str]] | None:
    source_path = Path(str(source_file_path or "").strip())
    if fitz is None or not source_path.exists() or source_path.suffix.lower() != ".pdf":
        return None
    if source_page <= 0:
        return None

    page_end = source_page_end if source_page_end >= source_page else source_page
    with fitz.open(str(source_path)) as document:
        if document.page_count <= 0:
            return None
        start_index = max(0, source_page - 1)
        if start_index >= document.page_count:
            return None
        end_index = min(document.page_count - 1, max(start_index, page_end - 1))
        page_indices = list(range(start_index, end_index + 1))
        match_fragments = _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=current_headers,
            rows=current_rows,
            notes=notes,
        )
        page_indices = _refine_table_page_indices(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            match_fragments=match_fragments,
            headers=current_headers,
            slice_rows=current_rows,
            detection_cache=source_table_detection_cache,
        )
        if not page_indices:
            return None
        before_anchor_candidates = _build_context_anchor_fragments(before_context_text, from_end=True)
        after_anchor_candidates = _build_context_anchor_fragments(after_context_text, from_end=False)
        self_anchor_candidates = _build_current_entry_anchor_fragments(
            table_text=table_text,
            table_title=table_title,
            before_context_text=before_context_text,
        )
        start_anchor, end_anchor = _find_context_anchor_window(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            before_candidates=before_anchor_candidates,
            after_candidates=after_anchor_candidates,
            self_candidates=self_anchor_candidates,
            detection_cache=source_line_detection_cache,
        )
        required_match_fragments = _build_recovery_required_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=current_headers,
            rows=current_rows,
        )

        best_rows: list[list[str]] | None = None
        best_headers: list[str] | None = None
        best_notes: list[str] | None = None
        best_score = -10**9

        for page_index in page_indices:
            candidates = _load_pdf_table_candidates(
                document,
                source_key=str(source_path.resolve()),
                page_index=page_index,
                detection_cache=source_table_detection_cache,
            )
            if not candidates:
                continue
            for candidate in candidates:
                sliced_rows = _slice_pdf_candidate_rows_to_anchor_window(
                    candidate,
                    page_index=page_index,
                    start_anchor=start_anchor,
                    end_anchor=end_anchor,
                )
                sliced_rows = _trim_pdf_candidate_rows_by_match_fragments(
                    sliced_rows,
                    match_fragments=match_fragments,
                )
                candidate_text = _flatten_table_candidate_text(sliced_rows)
                if required_match_fragments and _score_pdf_table_candidate(candidate_text, required_match_fragments) <= 0:
                    continue
                recovered = _coerce_pdf_candidate_rows_to_table(
                    sliced_rows,
                    current_headers=current_headers,
                )
                if not recovered:
                    continue
                headers, rows, recovered_notes = recovered
                trimmed_rows = _trim_recovered_rows_to_explicit_current_section(
                    recovered_headers=headers,
                    recovered_rows=rows,
                    current_rows=current_rows,
                )
                if trimmed_rows is None:
                    continue
                rows = trimmed_rows
                score = _table_quality_score(headers, rows)
                score += _score_pdf_table_candidate(candidate_text, match_fragments)
                score += _score_pdf_table_candidate(candidate_text, required_match_fragments) * 2
                if start_anchor and int(start_anchor.get("page_index", -1)) == page_index:
                    score += 8
                if end_anchor and int(end_anchor.get("page_index", -1)) == page_index:
                    score += 4
                if score > best_score:
                    best_score = score
                    best_headers = headers
                    best_rows = rows
                    best_notes = recovered_notes

        if not best_headers or best_rows is None:
            return None
        best_rows = _append_numbered_section_continuation_rows(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            headers=best_headers,
            rows=best_rows,
            detection_cache=source_table_detection_cache,
        )
        return best_headers, best_rows, best_notes or []


def _trim_recovered_rows_to_explicit_current_section(
    *,
    recovered_headers: list[str],
    recovered_rows: list[list[str]],
    current_rows: list[list[str]],
) -> list[list[str]] | None:
    target_rows = [row for row in current_rows if isinstance(row, list) and row]
    if not target_rows or not recovered_rows:
        return recovered_rows
    if not _has_explicit_numbered_row_slice_label(target_rows):
        return recovered_rows

    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number:
        return recovered_rows

    section_start = _find_candidate_section_start(recovered_rows, section_number)
    if section_start is not None:
        section_stop = _find_candidate_section_stop(recovered_rows, section_start, section_number)
        section_rows = [
            list(row)
            for row in recovered_rows[section_start:section_stop]
            if isinstance(row, list) and row
        ]
        if _recovered_rows_look_like_current_section(section_rows, target_rows, section_number=section_number):
            return section_rows

    matches = _resolve_row_slice_match_indices(
        recovered_rows,
        headers=recovered_headers,
        target_rows=target_rows,
    )
    if not matches:
        return None
    start_index, end_index = matches
    matched_rows = [
        list(row)
        for row in recovered_rows[start_index : end_index + 1]
        if isinstance(row, list) and row
    ]
    if not _recovered_rows_look_like_current_section(matched_rows, target_rows, section_number=section_number):
        return None
    return matched_rows


def _recovered_rows_look_like_current_section(
    candidate_rows: list[list[str]],
    target_rows: list[list[str]],
    *,
    section_number: str,
) -> bool:
    if not candidate_rows or not target_rows:
        return False
    if _candidate_row_section_number(candidate_rows[0]) != section_number:
        return False

    target_title = ""
    candidate_title = ""
    if len(target_rows[0]) > 1:
        target_title = _normalize_match_text(str(target_rows[0][1] or ""))
    if len(candidate_rows[0]) > 1:
        candidate_title = _normalize_match_text(str(candidate_rows[0][1] or ""))
    if target_title and candidate_title and not (
        target_title in candidate_title or candidate_title in target_title
    ):
        return False
    return True


def _slice_pdf_candidate_rows_to_anchor_window(
    candidate: dict[str, Any],
    *,
    page_index: int,
    start_anchor: dict[str, Any] | None,
    end_anchor: dict[str, Any] | None,
) -> list[list[str]]:
    rows = candidate.get("rows_text") or []
    boxes = candidate.get("row_bboxes") or []
    if not rows:
        return []
    if len(rows) != len(boxes):
        return [row for row in rows if isinstance(row, list)]

    top_limit = None
    bottom_limit = None
    if start_anchor and int(start_anchor.get("page_index", -1)) == page_index:
        top_limit = float(start_anchor["bbox"][1]) - 4.0
    if end_anchor and int(end_anchor.get("page_index", -1)) == page_index:
        bottom_limit = float(end_anchor["bbox"][1]) - 4.0

    selected: list[list[str]] = []
    for row, bbox in zip(rows, boxes):
        if not isinstance(row, list):
            continue
        if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
            selected.append(row)
            continue
        row_top = float(bbox[1])
        row_bottom = float(bbox[3])
        if top_limit is not None and row_bottom <= top_limit:
            continue
        if bottom_limit is not None and row_top >= bottom_limit:
            continue
        selected.append(row)
    return selected or [row for row in rows if isinstance(row, list)]


def _coerce_pdf_candidate_rows_to_table(
    rows: list[list[Any]],
    *,
    current_headers: list[str],
) -> tuple[list[str], list[list[str]], list[str]] | None:
    normalized_rows = [_normalize_pdf_candidate_row(row) for row in rows]
    normalized_rows = [row for row in normalized_rows if any(cell for cell in row)]
    if not normalized_rows:
        return None

    notes: list[str] = []
    while normalized_rows and _looks_like_pdf_noise_band_row(normalized_rows[0]):
        normalized_rows.pop(0)
    if not normalized_rows:
        return None

    max_columns = max(len(row) for row in normalized_rows)
    if max_columns < 2:
        return None
    normalized_rows = [(row + [""] * max_columns)[:max_columns] for row in normalized_rows]
    normalized_rows = _drop_empty_pdf_separator_columns(normalized_rows)
    if not normalized_rows:
        return None
    max_columns = max(len(row) for row in normalized_rows)
    normalized_rows = [(row + [""] * max_columns)[:max_columns] for row in normalized_rows]

    scan_index = 0
    while scan_index < len(normalized_rows):
        current_row = normalized_rows[scan_index]
        next_row = normalized_rows[scan_index + 1] if scan_index + 1 < len(normalized_rows) else []
        following_row = normalized_rows[scan_index + 2] if scan_index + 2 < len(normalized_rows) else []
        if _looks_like_pdf_noise_band_row(current_row):
            scan_index += 1
            continue
        if next_row and _looks_like_pdf_title_band_row(current_row, next_row, following_row):
            title_text = " ".join(cell for cell in current_row if cell).strip()
            if title_text:
                notes.append(title_text)
            scan_index += 1
            continue
        break
    normalized_rows = normalized_rows[scan_index:]
    if not normalized_rows:
        return None

    header_row: list[str] = []
    body_rows: list[list[str]] = normalized_rows
    unit_row_index = -1
    for index, row in enumerate(normalized_rows):
        next_row = normalized_rows[index + 1] if index + 1 < len(normalized_rows) else []
        following_row = normalized_rows[index + 2] if index + 2 < len(normalized_rows) else []
        if next_row and _looks_like_pdf_title_band_row(row, next_row, following_row):
            title_text = " ".join(cell for cell in row if cell).strip()
            if title_text:
                notes.append(title_text)
            continue
        if next_row and _looks_like_pdf_group_header_row(row, next_row):
            header_row = _merge_pdf_group_header_rows(row, next_row)
            body_rows = normalized_rows[index + 2 :]
            break
        if _looks_like_pdf_header_row(row, next_row):
            if next_row and following_row and _looks_like_pdf_unit_row(next_row, data_row=following_row):
                header_row = _merge_pdf_header_rows(row, next_row)
                body_rows = normalized_rows[index + 2 :]
                unit_row_index = index + 1
            else:
                header_row = row
                body_rows = normalized_rows[index + 1 :]
            break

    if not header_row:
        sanitized_headers = _sanitize_pdf_fallback_headers(current_headers, sample_rows=normalized_rows)
        header_row = sanitized_headers if len(sanitized_headers) == max_columns else []
        body_rows = normalized_rows

    headers = _clean_recovered_headers(header_row, max_columns=max_columns, sample_rows=body_rows)
    if not headers or len(headers) != max_columns:
        return None

    cleaned_body: list[list[str]] = []
    for index, row in enumerate(body_rows):
        next_row = body_rows[index + 1] if index + 1 < len(body_rows) else []
        following_row = body_rows[index + 2] if index + 2 < len(body_rows) else []
        if row == headers:
            continue
        if unit_row_index >= 0 and _looks_like_pdf_unit_row(row, data_row=[]):
            continue
        if len(cleaned_body) == 0 and _looks_like_pdf_title_band_row(row, next_row, following_row):
            continue
        if cleaned_body and (
            _looks_like_pdf_section_break_row(row)
            or _looks_like_pdf_title_band_row(row, next_row, following_row)
            or _looks_like_pdf_header_row(row, next_row)
        ):
            break
        if cleaned_body and _looks_like_pdf_trailing_note_row(row):
            note_text = next((cell for cell in row if cell), "")
            if note_text:
                notes.append(note_text)
            break
        if not any(cell for cell in row):
            continue
        cleaned_body.append(row)
    if not cleaned_body:
        return None
    headers, cleaned_body = _drop_placeholder_recovered_columns(headers, cleaned_body)
    cleaned_body = _repair_decoupling_frequency_targets(headers, cleaned_body)
    return headers, cleaned_body, notes


def _trim_pdf_candidate_rows_by_match_fragments(
    rows: list[list[Any]],
    *,
    match_fragments: list[tuple[str, int]],
) -> list[list[Any]]:
    if not rows or not match_fragments:
        return rows
    scored_indices: list[int] = []
    for index, row in enumerate(rows):
        row_text = _flatten_table_candidate_text([row])
        if _score_pdf_table_candidate(row_text, match_fragments) > 0:
            scored_indices.append(index)
    if not scored_indices:
        return rows
    start = scored_indices[0]
    end = scored_indices[-1]
    if start > 0:
        prev_row = _normalize_pdf_candidate_row(rows[start - 1] if isinstance(rows[start - 1], list) else [])
        next_row = _normalize_pdf_candidate_row(rows[start] if isinstance(rows[start], list) else [])
        if not _looks_like_pdf_data_row(prev_row) and (
            _looks_like_pdf_title_band_row(prev_row, next_row) or _looks_like_pdf_header_row(prev_row, next_row)
        ):
            start -= 1
    if end < len(rows) - 1 and end - start <= 1:
        while end + 1 < len(rows):
            next_row = _normalize_pdf_candidate_row(rows[end + 1] if isinstance(rows[end + 1], list) else [])
            if _looks_like_pdf_section_break_row(next_row):
                break
            end += 1
            if end - start >= 3 and not _looks_like_pdf_data_row(next_row):
                break
    return rows[start : end + 1]


def _normalize_pdf_candidate_row(row: list[Any]) -> list[str]:
    if not isinstance(row, list):
        return []
    normalized: list[str] = []
    for cell in row:
        value = re.sub(r"\s+", " ", str(cell or "").replace("_", " ")).strip()
        value = _repair_fragmented_mixed_language_cell(value)
        value = _repair_fragmented_numeric_cell(value)
        value = _repair_fragmented_cjk_punctuation_cell(value)
        normalized.append(value)
    return normalized


def _repair_fragmented_mixed_language_cell(value: str) -> str:
    text = str(value or "").strip()
    if not text or not re.search(r"[A-Za-z]", text):
        return text

    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    if len(cjk_runs) < 4 or max((len(run) for run in cjk_runs), default=0) > 1:
        return text

    collapsed_cjk = "".join(cjk_runs)
    if len(collapsed_cjk) < 4:
        return text

    ascii_tokens = [token for token in re.findall(r"[A-Za-z]{2,}", text) if len(token) >= 2]
    ascii_part = " ".join(ascii_tokens[:4]).strip()
    if ascii_part:
        return f"{collapsed_cjk} / {ascii_part}"
    return collapsed_cjk


def _repair_fragmented_numeric_cell(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return text

    repaired = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)
    repaired = re.sub(r"(?<=[+\-])\s+(?=\d)", "", repaired)
    if re.search(r"[A-Za-z\u4e00-\u9fff]", repaired):
        return repaired

    repaired = re.sub(r"±0(\d{2})(?![\d.])", r"±0.\1", repaired)
    repaired = re.sub(r"([±+\-])0(\d)\s*\.", r"\g<1>0.\2", repaired)
    repaired = re.sub(r"(?<=\d)\s+(?=[±+\-])", "、", repaired)
    repaired = re.sub(r"\s*、\s*", "、", repaired)

    trailing_decimal = re.fullmatch(r"([+\-]?)(\d)(\d)\s*\.", repaired)
    if trailing_decimal:
        sign, integer, fraction = trailing_decimal.groups()
        repaired = f"{sign}{integer}.{fraction}"

    repaired = re.sub(r"(?:\s*[、.])+$", "", repaired)
    return repaired


def _repair_fragmented_cjk_punctuation_cell(value: str) -> str:
    text = str(value or "").strip()
    if not text or re.search(r"[A-Za-z]", text):
        return text

    enumerated = re.fullmatch(r"(\d+)\s+(.+?)\s*、", text)
    if enumerated:
        number, body = enumerated.groups()
        return f"{number}、{body.strip()}"
    return text


def _looks_like_pdf_noise_band_row(row: list[str]) -> bool:
    non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
    if not non_empty:
        return True
    return all(re.fullmatch(r"[.…·•\-—_/]+", cell) for cell in non_empty)


def _looks_like_pdf_trailing_note_row(row: list[str]) -> bool:
    non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
    if len(non_empty) != 1:
        return False
    value = non_empty[0]
    return len(value) >= 24 and bool(re.match(r"^[—\-•●注备注]", value))


def _looks_like_pdf_title_band_row(
    row: list[str],
    next_row: list[str],
    following_row: list[str] | None = None,
) -> bool:
    non_empty = [cell for cell in row if cell]
    if len(non_empty) != 1:
        return False
    title = non_empty[0]
    if len(title) < 2 or len(title) > 120:
        return False
    next_non_empty = [cell for cell in next_row if cell]
    if len(next_non_empty) >= 2:
        return True
    if len(next_non_empty) == 1 and following_row:
        return _looks_like_pdf_title_band_row(next_row, following_row)
    return False


def _looks_like_pdf_header_row(row: list[str], next_row: list[str]) -> bool:
    non_empty = [cell for cell in row if cell]
    if len(non_empty) < 2:
        return False
    if not next_row:
        return False
    if all(re.search(r"\d", cell) for cell in non_empty):
        return False
    digit_like_count = sum(1 for cell in non_empty if re.search(r"\d", cell))
    if digit_like_count >= max(1, len(non_empty) // 2):
        return False
    if re.fullmatch(r"\d+(?:\.\d+)?", non_empty[0]):
        return False
    next_non_empty = [cell for cell in next_row if cell]
    if next_non_empty and all(not re.search(r"\d", cell) for cell in next_non_empty) and not _looks_like_pdf_unit_row(
        next_row,
        data_row=[],
    ):
        return False
    return True


def _looks_like_pdf_group_header_row(row: list[str], next_row: list[str]) -> bool:
    row_non_empty = [cell for cell in row if cell]
    next_non_empty = [cell for cell in next_row if cell]
    if len(row_non_empty) < 1 or len(next_non_empty) < 2:
        return False
    if len(row_non_empty) == 1 and len(str(row_non_empty[0] or "").strip()) > 40:
        return False
    if len(row_non_empty) >= len(next_non_empty):
        return False
    if all(re.search(r"\d", cell) for cell in next_non_empty):
        return False
    if _looks_like_pdf_data_row(next_row):
        return False
    return any(not cell for cell in row)


def _looks_like_pdf_unit_row(row: list[str], *, data_row: list[str]) -> bool:
    non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
    if not non_empty or len(non_empty) > 2:
        return False
    if data_row:
        data_non_empty = [str(cell or "").strip() for cell in data_row if str(cell or "").strip()]
        if not data_non_empty or all(not re.search(r"[\dA-Za-z\u4e00-\u9fff]", cell) for cell in data_non_empty):
            return False
    return all(
        len(cell) <= 12
        and not re.search(r"[\u4e00-\u9fff]{2,}", cell)
        and bool(re.search(r"[A-Za-z%/°℃\-+±~～0-9]", cell))
        for cell in non_empty
    )


def _merge_pdf_header_rows(header_row: list[str], unit_row: list[str]) -> list[str]:
    merged = list(header_row)
    for index, unit in enumerate(unit_row):
        unit_text = str(unit or "").strip()
        if not unit_text:
            continue
        if index < len(merged) and str(merged[index] or "").strip():
            merged[index] = f"{str(merged[index] or '').strip()} {unit_text}".strip()
            continue
        target_index = max(0, index - 1)
        while target_index >= 0:
            header_text = str(merged[target_index] or "").strip()
            if header_text:
                merged[target_index] = f"{header_text} {unit_text}".strip()
                break
            target_index -= 1
        else:
            if index < len(merged):
                merged[index] = unit_text
    return merged


def _merge_pdf_group_header_rows(group_row: list[str], subheader_row: list[str]) -> list[str]:
    merged: list[str] = []
    current_group = ""
    for group, subheader in zip(group_row, subheader_row):
        group_text = str(group or "").strip()
        subheader_text = str(subheader or "").strip()
        if group_text:
            current_group = group_text
        if current_group and subheader_text:
            if current_group == subheader_text or subheader_text in current_group:
                merged.append(subheader_text)
            else:
                merged.append(f"{current_group} {subheader_text}".strip())
            continue
        merged.append(subheader_text or current_group)
    return merged


def _drop_empty_pdf_separator_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return rows
    max_columns = max(len(row) for row in rows)
    keep_indices: list[int] = []
    for column_index in range(max_columns):
        if any(str(row[column_index] or "").strip() for row in rows if column_index < len(row)):
            keep_indices.append(column_index)
    if len(keep_indices) < 2 or len(keep_indices) == max_columns:
        return rows
    return [
        [str(row[index] or "").strip() if index < len(row) else "" for index in keep_indices]
        for row in rows
    ]


def _drop_placeholder_recovered_columns(
    headers: list[str],
    rows: list[list[str]],
) -> tuple[list[str], list[list[str]]]:
    if not headers or not rows:
        return headers, rows

    keep_indices: list[int] = []
    for index, header in enumerate(headers):
        header_text = str(header or "").strip()
        is_placeholder = not header_text or bool(re.fullmatch(r"(?:col\d+|列\d+)", header_text, re.I))
        column_values = [str(row[index] or "").strip() for row in rows if index < len(row)]
        if is_placeholder and all(not value for value in column_values):
            continue
        keep_indices.append(index)

    if len(keep_indices) == len(headers) or len(keep_indices) < 2:
        return headers, rows

    trimmed_headers = [headers[index] for index in keep_indices]
    trimmed_rows = [[row[index] if index < len(row) else "" for index in keep_indices] for row in rows]
    return trimmed_headers, trimmed_rows


def _repair_decoupling_frequency_targets(headers: list[str], rows: list[list[str]]) -> list[list[str]]:
    if len(headers) < 5 or len(rows) < 4:
        return rows
    if not any("模态" in str(header or "") for header in headers):
        return rows
    target_index = next((index for index, header in enumerate(headers) if "目标" in str(header or "")), -1)
    if target_index < 0 or not any("解耦" in str(header or "") for header in headers):
        return rows

    repaired = [list(row) for row in rows]
    for row_index, row in enumerate(repaired[:-1]):
        cell = str(row[target_index] or "").strip() if target_index < len(row) else ""
        if not cell:
            continue
        parts = _split_stacked_frequency_targets(cell)
        if len(parts) < 3:
            continue
        blank_run = 0
        for follower in repaired[row_index + 1 :]:
            value = str(follower[target_index] or "").strip() if target_index < len(follower) else ""
            if value:
                break
            blank_run += 1
        if blank_run < 2:
            continue

        redistributed: list[str]
        if (
            len(parts) >= 2
            and not _looks_like_frequency_range(parts[0])
            and _looks_like_frequency_range(parts[1])
        ):
            redistributed = [f"{parts[0]} {parts[1]}".strip(), *parts[2:]]
        else:
            redistributed = parts
        if blank_run < len(redistributed):
            continue

        repaired[row_index][target_index] = ""
        for offset, value in enumerate(redistributed, start=1):
            repaired[row_index + offset][target_index] = value
        break
    return repaired


def _split_stacked_frequency_targets(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return []
    ranges = re.findall(r"\d+\s*-\s*\d+\s*Hz", text, re.I)
    if len(ranges) < 2:
        return []
    prefix = re.sub(r"\d+\s*-\s*\d+\s*Hz", " ", text, flags=re.I)
    prefix = re.sub(r"\s+", " ", prefix).strip()
    parts: list[str] = []
    if prefix:
        parts.append(prefix)
    parts.extend(ranges)
    return parts


def _looks_like_frequency_range(value: str) -> bool:
    return bool(re.fullmatch(r"\d+\s*-\s*\d+\s*Hz", str(value or "").strip(), re.I))


def _sanitize_pdf_fallback_headers(headers: list[str], *, sample_rows: list[list[str]]) -> list[str]:
    normalized = [str(cell or "").strip() for cell in headers]
    if not normalized:
        return []
    first_row = sample_rows[0] if sample_rows else []
    sanitized: list[str] = []
    for index, header in enumerate(normalized):
        value = header
        first_cell = str(first_row[index] or "").strip() if index < len(first_row) else ""
        if first_cell and first_cell in value and value != first_cell:
            prefix = value.split(first_cell, 1)[0].strip()
            if prefix:
                value = prefix
        sanitized.append(value)
    return sanitized


def _clean_recovered_headers(
    header_row: list[str],
    *,
    max_columns: int,
    sample_rows: list[list[str]],
) -> list[str]:
    headers = [str(cell or "").strip() for cell in header_row]
    headers = (headers + [""] * max_columns)[:max_columns]
    if not any(headers):
        if max_columns == 2:
            headers = ["项目", "内容"]
        elif max_columns == 3:
            headers = ["项目", "内容", "备注"]
        else:
            headers = [f"列{index + 1}" for index in range(max_columns)]
    cleaned: list[str] = []
    for index, header in enumerate(headers):
        value = re.sub(r"\s+", " ", str(header or "")).strip()
        if not value or re.fullmatch(r"col\d+", value, re.I):
            if max_columns == 2:
                value = "项目" if index == 0 else "内容"
            elif index == 1 and max_columns >= 4:
                value = "类别"
            else:
                value = f"列{index + 1}"
        cleaned.append(value)
    if max_columns == 3 and re.fullmatch(r"(?:col\d+|列\d+)", cleaned[2], re.I):
        base = str(cleaned[1] or "").strip()
        if base and _sample_column_looks_numeric(sample_rows, 1) and _sample_column_looks_numeric(sample_rows, 2):
            cleaned[1] = f"{base} 下限".strip()
            cleaned[2] = f"{base} 上限".strip()
    if cleaned == ["项目", "内容"] and sample_rows and len(sample_rows[0]) == 2:
        return cleaned
    return cleaned


def _sample_column_looks_numeric(rows: list[list[str]], column_index: int) -> bool:
    seen = 0
    numeric_like = 0
    for row in rows[:8]:
        if column_index >= len(row):
            continue
        value = str(row[column_index] or "").strip()
        if not value:
            continue
        seen += 1
        if re.search(r"\d", value):
            numeric_like += 1
    return seen > 0 and numeric_like >= max(1, seen - 1)


def _table_quality_score(headers: list[str], rows: list[list[str]]) -> int:
    score = len(headers) * 8 + min(len(rows), 12) * 3
    score -= sum(8 for cell in headers if not cell or re.fullmatch(r"(?:col\d+|列\d+)", cell, re.I))
    placeholder_cells = 0
    overloaded_rows = 0
    for row in rows:
        placeholder_cells += sum(
            1 for cell in row if re.fullmatch(r"(?:col\d+|/|-|—)?", str(cell or "").strip(), re.I)
        )
        if len(headers) >= 6:
            long_cells = [cell for cell in row if len(str(cell or "").strip()) >= 28]
            blankish = sum(1 for cell in row if str(cell or "").strip() in {"", "/", "-", "—"})
            if long_cells and blankish >= 2:
                overloaded_rows += 1
    score -= placeholder_cells
    score -= overloaded_rows * 10
    if rows and rows[0] == headers:
        score -= 12
    if len(rows) <= 1:
        long_cells = sum(1 for row in rows for cell in row if len(str(cell or "").strip()) >= 120)
        score -= long_cells * 30
        if headers in (["项目", "内容"], ["项目", "内容", "备注"]):
            score -= 24
    return score


def _looks_like_pdf_section_break_row(row: list[str]) -> bool:
    non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
    if not non_empty:
        return True
    if len(non_empty) > 1:
        return False
    return bool(_looks_like_local_clause_title(non_empty[0]) or re.match(r"^(?:备注|注[:：])", non_empty[0]))


def _looks_like_pdf_data_row(row: list[str]) -> bool:
    non_empty = [str(cell or "").strip() for cell in row if str(cell or "").strip()]
    if len(non_empty) < 2:
        return False
    if _looks_like_local_clause_title(non_empty[0]):
        return False
    if any(re.search(r"\d", cell) for cell in non_empty):
        return True
    return any(cell in {"/", "-", "—"} for cell in non_empty)


def _recovered_table_looks_useful(headers: list[str], rows: list[list[str]]) -> bool:
    if not headers or not rows:
        return False
    if len(headers) >= 2 and any(len(str(cell or "").strip()) > 48 for cell in headers):
        return False
    placeholder_headers = sum(
        1 for cell in headers if not cell or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I)
    )
    meaningful_rows = [
        row
        for row in rows
        if sum(1 for cell in row if str(cell or "").strip()) >= 2
    ]
    if not meaningful_rows:
        return False
    if len(rows) == 1 and placeholder_headers > 0:
        return False
    return True


def _recovered_source_indicates_column_mismatch(
    *,
    current_headers: list[str],
    current_rows: list[list[str]],
    recovered_headers: list[str],
    recovered_rows: list[list[str]],
) -> bool:
    current_col_count = max(len(current_headers), max((len(row) for row in current_rows), default=0))
    recovered_col_count = max(len(recovered_headers), max((len(row) for row in recovered_rows), default=0))
    if current_col_count <= 0 or recovered_col_count < current_col_count + 1:
        return False

    placeholder_headers = sum(
        1
        for cell in recovered_headers
        if not str(cell or "").strip() or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I)
    )
    meaningful_rows = [
        row
        for row in recovered_rows
        if sum(1 for cell in row if str(cell or "").strip()) >= 2
    ]
    return placeholder_headers >= 1 and len(meaningful_rows) <= 2


def _structured_table_looks_low_quality(headers: list[str], rows: list[list[str]]) -> bool:
    if not headers or not rows:
        return True
    if any(not cell or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I) for cell in headers):
        return True
    if any(str(cell or "").strip() in {"/", "-", "—"} for cell in headers):
        return True
    if re.fullmatch(r"\d+(?:\.\d+)?", str(headers[0] or "").strip()):
        return True
    if any(re.search(r"\d", str(cell or "")) for cell in headers if len(str(cell or "").strip()) >= 4):
        return True
    if rows and rows[0] == headers:
        return True
    if len(headers) >= 6:
        for row in rows:
            blankish = sum(1 for cell in row if str(cell or "").strip() in {"", "/", "-", "—"})
            long_cells = sum(1 for cell in row if len(str(cell or "").strip()) >= 28)
            if blankish >= 2 and long_cells >= 1:
                return True
    return False


def _should_replace_with_recovered_table(
    *,
    current_render_mode: str,
    current_headers: list[str],
    current_rows: list[list[str]],
    recovered_headers: list[str],
    recovered_rows: list[list[str]],
) -> bool:
    if not recovered_headers or not recovered_rows:
        return False
    current_score = _table_quality_score(current_headers, current_rows)
    recovered_score = _table_quality_score(recovered_headers, recovered_rows)
    if current_render_mode != "structured":
        return len(recovered_rows) >= 2 and recovered_score > current_score + 4
    if _structured_table_looks_low_quality(current_headers, current_rows):
        return recovered_score >= current_score - 2

    header_gain = len(recovered_headers) - len(current_headers)
    row_gain = len(recovered_rows) - len(current_rows)
    current_placeholder_headers = sum(
        1 for cell in current_headers if not cell or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I)
    )
    recovered_placeholder_headers = sum(
        1 for cell in recovered_headers if not cell or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I)
    )
    if header_gain >= 1 and recovered_score >= current_score - 2:
        return True
    if row_gain >= 2 and recovered_score >= current_score + 2:
        return True
    if recovered_placeholder_headers < current_placeholder_headers and recovered_score >= current_score:
        return True
    return recovered_score > current_score + 8


def _split_text_around_recovered_table(
    text: str,
    *,
    headers: list[str],
    rows: list[list[str]],
    title: str,
) -> tuple[list[str], list[str]]:
    def _clean_line(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip())

    lines = [_clean_line(line) for line in str(text or "").splitlines() if _clean_line(line)]
    if len(lines) <= 1:
        return (lines[:1], [])

    line_bounds = _find_recovered_table_line_bounds(lines, headers=headers, rows=rows)
    if not line_bounds:
        start_index = 1 if title and _clean_line(lines[0]) != _clean_line(title) else 0
        end_index = len(lines) - 1
    else:
        start_index, end_index = line_bounds

    lead = "\n".join(lines[:start_index]).strip()
    tail = "\n".join(lines[end_index + 1 :]).strip()
    return ([lead] if lead else [], [tail] if tail else [])


def _find_recovered_table_line_bounds(
    lines: list[str],
    *,
    headers: list[str],
    rows: list[list[str]],
) -> tuple[int, int] | None:
    anchors: list[str] = []
    if headers:
        anchors.extend(cell for cell in headers[:3] if cell)
    if rows:
        anchors.extend(cell for cell in rows[0][:3] if cell)
        anchors.extend(cell for cell in rows[-1][:2] if cell)
    if not anchors:
        return None

    start_index = None
    end_index = None
    for index, line in enumerate(lines):
        normalized_line = _normalize_match_text(line)
        if not normalized_line:
            continue
        if start_index is None and any(_normalize_match_text(anchor) in normalized_line for anchor in anchors[:4]):
            start_index = index
        if any(_normalize_match_text(anchor) in normalized_line for anchor in anchors[2:]):
            end_index = index
    if start_index is None:
        return None
    return start_index, max(start_index, end_index if end_index is not None else start_index)


def _paragraph_only_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("kind") or "") != "paragraph":
            continue
        text = _clean_paragraph_block_text(str(block.get("text") or "").strip())
        if text:
            output.append({"kind": "paragraph", "text": text})
    return output


def _clean_paragraph_block_text(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = _drop_reference_only_paragraph_lines(value)
    value = _strip_markdown_noise_lines(value)
    return _drop_trailing_table_intro_band(_drop_repeated_trailing_merged_line(value))


def _looks_like_reference_only_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(re.fullmatch(r"(?:ref|reference)\s*[:：].{1,120}", value, re.I))


def _drop_reference_only_paragraph_lines(text: str) -> str:
    lines = [str(line or "").strip() for line in str(text or "").splitlines() if str(line or "").strip()]
    if not lines:
        return ""
    kept = [line for line in lines if not _looks_like_reference_only_line(line)]
    if not kept:
        return ""
    return "\n".join(kept).strip()


def _strip_leading_markdown_noise_token(line: str) -> str:
    value = str(line or "").strip()
    if not value:
        return ""
    return re.sub(r"^\s*~~[A-Za-z0-9 _-]{1,16}~~\s*", "", value).strip()


def _strip_markdown_noise_lines(text: str) -> str:
    lines = [_strip_leading_markdown_noise_token(str(line or "").strip()) for line in str(text or "").splitlines()]
    kept = [line for line in lines if line]
    return "\n".join(kept).strip()


def _drop_trailing_table_intro_band(text: str) -> str:
    lines = [str(line or "").strip() for line in str(text or "").splitlines() if str(line or "").strip()]
    if len(lines) < 3:
        return str(text or "").strip()

    tail_start = len(lines)
    while tail_start > 0 and _is_compact_band_line(lines[tail_start - 1]):
        tail_start -= 1
    if tail_start >= len(lines):
        return "\n".join(lines).strip()

    tail = lines[tail_start:]
    joined = _normalize_match_text(" ".join(tail))
    if not joined:
        return "\n".join(lines).strip()
    if not (
        re.search(r"序号.*测试(?:类别|项目).*测试(?:小类|顺序|工况)", joined)
        or re.search(r"序号.*项目.*要求", joined)
        or any(re.fullmatch(r"col\d+", line, re.I) for line in tail)
    ):
        return "\n".join(lines).strip()
    if not any(
        len(_normalize_match_text(line)) >= 16 or any(mark in line for mark in ("：", ":", "。", "；", ";"))
        for line in lines[:tail_start]
    ):
        return "\n".join(lines).strip()
    return "\n".join(lines[:tail_start]).strip()


def _drop_repeated_trailing_merged_line(text: str) -> str:
    lines = [str(line or "").strip() for line in str(text or "").splitlines() if str(line or "").strip()]
    if len(lines) < 4:
        return str(text or "").strip()

    last_line = lines[-1]
    normalized_last = _normalize_match_text(last_line)
    if len(normalized_last) < 64:
        return "\n".join(lines)

    for start_index in range(1, len(lines) - 2):
        prior_joined = " ".join(lines[start_index:-1]).strip()
        normalized_prior = _normalize_match_text(prior_joined)
        if len(normalized_prior) < 48:
            continue
        similarity = SequenceMatcher(None, normalized_prior, normalized_last).ratio()
        if normalized_prior in normalized_last or similarity >= 0.9:
            return "\n".join(lines[:-1]).strip()
    return "\n".join(lines).strip()


def _drop_trailing_table_context_paragraphs(
    paragraphs: list[str],
    *,
    next_entry_text: str,
) -> list[str]:
    cleaned = [_clean_paragraph_block_text(str(item or "").strip()) for item in paragraphs if str(item or "").strip()]
    if not cleaned:
        return []
    if _looks_like_trailing_table_context_noise(cleaned[-1], next_entry_text=next_entry_text):
        cleaned.pop()
    return cleaned


def _should_skip_trailing_table_context_block(
    blocks: list[dict[str, Any]],
    block_index: int,
    *,
    next_entry_text: str,
) -> bool:
    if block_index <= 0 or block_index >= len(blocks):
        return False
    if str(blocks[block_index - 1].get("kind") or "") != "table":
        return False
    if block_index != _last_nonempty_block_index(blocks):
        return False
    return _looks_like_trailing_table_context_noise(
        str(blocks[block_index].get("text") or "").strip(),
        next_entry_text=next_entry_text,
    )


def _trailing_table_context_text(
    blocks: list[dict[str, Any]],
    block_index: int,
    *,
    next_entry_text: str,
) -> str:
    neighbor_index = block_index + 1
    if neighbor_index < 0 or neighbor_index >= len(blocks):
        return ""
    if neighbor_index != _last_nonempty_block_index(blocks):
        return ""
    neighbor_text = str(blocks[neighbor_index].get("text") or "").strip()
    if _looks_like_trailing_table_context_noise(neighbor_text, next_entry_text=next_entry_text):
        return neighbor_text
    return ""


def _last_nonempty_block_index(blocks: list[dict[str, Any]]) -> int:
    for index in range(len(blocks) - 1, -1, -1):
        if str(blocks[index].get("text") or "").strip():
            return index
    return -1


def _should_prefer_source_clause_window(
    *,
    table_title: str,
    render_mode: str,
    headers: list[str],
    rows: list[list[str]],
    source_page: int,
    source_page_end: int,
    notes: list[str] | None = None,
    trailing_context_text: str = "",
) -> bool:
    normalized_mode = str(render_mode or "").strip().lower()
    title = str(table_title or "").strip()
    explicit_numbered_row_slice = _has_explicit_numbered_row_slice_label(rows)
    if explicit_numbered_row_slice:
        # Appendix/test-outline single-row items are more stable when cropped from the
        # matched table row instead of a broad heading-to-heading clause window.
        return False
    heading_like = (
        _looks_like_local_clause_title(title)
        or _looks_like_headingish_single_cell(title)
        or _looks_like_plain_heading_label(title)
        or _looks_like_attachment_table_title(title)
    )
    cross_page = source_page > 0 and source_page_end > source_page
    if trailing_context_text:
        return True
    if normalized_mode != "structured":
        return heading_like

    if cross_page and heading_like:
        return True
    if heading_like and _looks_like_axis_symbol_source_first_table(headers, rows):
        return True

    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if heading_like and len(rows) <= 2 and 0 < col_count <= 4 and _has_meaningful_source_context_note(notes or []):
        return True
    if heading_like and len(rows) <= 1 and 0 < col_count <= 4:
        return True
    return False


def _has_meaningful_source_context_note(notes: list[str]) -> bool:
    for item in notes:
        lines = [line.strip() for line in str(item or "").splitlines() if line.strip()]
        if lines and (
            _looks_like_local_clause_title(lines[0])
            or _looks_like_headingish_single_cell(lines[0])
            or _looks_like_plain_heading_label(lines[0])
            or _looks_like_attachment_table_title(lines[0])
        ):
            lines = lines[1:]
        value = "\n".join(lines).strip()
        if not value:
            continue
        if "\n" not in value and (
            _looks_like_local_clause_title(value)
            or _looks_like_headingish_single_cell(value)
            or _looks_like_plain_heading_label(value)
            or _looks_like_attachment_table_title(value)
        ):
            continue
        if value.count("|") >= 2:
            continue
        normalized = _normalize_match_text(value)
        if len(normalized) < 12:
            continue
        if any(mark in value for mark in ("。", "；", ";", "：", ":", "，", ",")):
            return True
        lines = [line.strip() for line in value.splitlines() if line.strip()]
        if len("".join(lines[:2])) >= 18:
            return True
    return False


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
    ref["complex_table_mode"] = bool(ref.get("complex_table_mode"))
    ref["complex_zone_id"] = str(ref.get("complex_zone_id") or "").strip()
    ref["complex_family"] = str(ref.get("complex_family") or "").strip()
    ref["complex_split_level"] = str(ref.get("complex_split_level") or "").strip()
    ref["complex_group_label"] = str(ref.get("complex_group_label") or "").strip()
    ref["complex_warnings"] = _normalize_string_list(ref.get("complex_warnings"))
    ref["complex_shadow_signatures"] = _normalize_string_list(ref.get("complex_shadow_signatures"))
    ref["export_variant_locked"] = bool(ref.get("export_variant_locked"))
    recommendation = _recommend_table_export_variant(ref)
    recommended_variant = recommendation["recommended_export_variant"]
    export_variant = _normalize_export_variant(ref.get("export_variant"))
    if not export_variant:
        export_variant = recommended_variant
    if ref["export_variant_locked"] and str(ref.get("source_snapshot_path") or "").strip():
        recommended_variant = "source"
        export_variant = "source"
    ref["recommended_export_variant"] = recommended_variant
    ref["export_variant_reasons"] = recommendation["export_variant_reasons"]
    ref["export_variant_score"] = recommendation["export_variant_score"]
    ref["export_variant"] = export_variant
    ref["export_variant_overridden"] = False if ref["export_variant_locked"] else export_variant != recommended_variant
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
    if bool(ref.get("export_variant_locked")) and str(ref.get("source_snapshot_path") or "").strip():
        return {
            "recommended_export_variant": "source",
            "export_variant_reasons": ["原文更忠实"],
            "export_variant_score": 100,
        }
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
    source_structure_mismatch = bool(ref.get("source_structure_mismatch"))
    source_column_count_hint = _coerce_int(ref.get("source_column_count_hint"))

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

    if source_snapshot_path and render_mode == "structured" and _structured_table_looks_low_quality(headers, rows):
        reasons.append("切片可读性差")
        score = max(score, 72)

    if source_snapshot_path and _looks_like_symbolic_or_placeholder_source_first_table(headers, rows):
        reasons.append("切片可读性差")
        score = max(score, 72)

    if source_snapshot_path and _looks_like_axis_symbol_source_first_table(headers, rows):
        reasons.append("切片可读性差")
        score = max(score, 72)

    if source_snapshot_path and _looks_like_compact_threshold_source_first_table(headers, rows, notes):
        reasons.append("切片可读性差")
        score = max(score, 72)

    current_col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if (
        source_snapshot_path
        and source_structure_mismatch
        and source_column_count_hint >= current_col_count + 1
    ):
        reasons.append("切片可读性差")
        score = max(score, 72)

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


def _looks_like_symbolic_or_placeholder_source_first_table(headers: list[str], rows: list[list[str]]) -> bool:
    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if col_count == 0 or col_count > 4 or len(rows) > 3:
        return False

    tokens = [
        str(cell or "").strip()
        for row in rows
        for cell in row
        if str(cell or "").strip()
    ]
    if not tokens:
        return False

    if any(re.fullmatch(r"#?(?:ref|value|name|n/a)!?", token, re.I) for token in tokens):
        return True

    short_alpha_tokens = [token for token in tokens if re.fullmatch(r"[A-Za-z]{1,2}", token)]
    return len(short_alpha_tokens) >= 2


def _looks_like_axis_symbol_source_first_table(headers: list[str], rows: list[list[str]]) -> bool:
    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if col_count < 2 or col_count > 4 or len(rows) < 4:
        return False

    labels = [str(row[0] or "").strip() for row in rows if isinstance(row, list) and row]
    if len(labels) < 4:
        return False

    axis_hits = sum(
        1
        for label in labels
        if re.fullmatch(r"(?:[WXYZ]|R[WXYZ]|T[XYZ]|RX|RY|RZ)", label, re.I)
    )
    if axis_hits < max(4, len(labels) - 1):
        return False

    numericish_cells = 0
    populated_value_cells = 0
    for row in rows:
        if not isinstance(row, list):
            continue
        for cell in row[1:]:
            token = str(cell or "").strip()
            if not token:
                continue
            populated_value_cells += 1
            if re.fullmatch(r"[<>]?[+-]?\d+(?:\.\d+)?(?:\s*(?:mm|°|hz|%))?", token, re.I):
                numericish_cells += 1
    return populated_value_cells > 0 and numericish_cells >= max(4, populated_value_cells - 1)


def _looks_like_compact_threshold_source_first_table(
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
) -> bool:
    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if col_count < 2 or col_count > 4 or len(rows) != 1:
        return False

    body_cells = [str(cell or "").strip() for cell in rows[0] if str(cell or "").strip()]
    if len(body_cells) < 2:
        return False

    visual_lengths = [_estimate_cell_visual_length(cell) for cell in body_cells]
    long_cells = sum(1 for length in visual_lengths if length >= 12)
    note_length = _estimate_cell_visual_length(" ".join(notes))
    return max(visual_lengths, default=0) >= 24 and long_cells >= 2 and note_length >= 12


def _looks_like_labeled_info_source_first_table(headers: list[str], rows: list[list[str]]) -> bool:
    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if col_count < 2 or col_count > 4 or len(rows) < 2 or len(rows) > 6:
        return False
    if _has_explicit_numbered_row_slice_label(rows):
        return False

    labels: list[str] = []
    long_value_cells = 0
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            return False
        label = str(row[0] or "").strip()
        if not label or _estimate_cell_visual_length(label) > 16:
            return False
        populated_values = [str(cell or "").strip() for cell in row[1:] if str(cell or "").strip()]
        if not populated_values:
            return False
        labels.append(label)
        long_value_cells += sum(1 for cell in populated_values if _estimate_cell_visual_length(cell) >= 12)

    return len(set(labels)) >= max(2, len(labels) - 1) and long_value_cells >= max(2, len(rows) - 1)


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


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    items = [str(item).strip() for item in value if str(item).strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


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


def _render_table_image(
    rows: list[list[str]],
    *,
    max_width_px: int,
    header_row_count: int = 1,
) -> tuple[BytesIO, int, int]:
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
        fill_color = "#DCE9F8" if row_index < max(0, int(header_row_count or 0)) else "#FFFFFF"
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


def _iter_table_raw_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def _build_compact_anchor_band(raw_lines: list[str], *, table_title: str = "") -> str:
    if not raw_lines:
        return ""

    title_norm = _normalize_match_text(table_title)
    start_index = 0
    while start_index < len(raw_lines):
        line = str(raw_lines[start_index] or "").strip()
        normalized = _normalize_match_text(line)
        if not normalized:
            start_index += 1
            continue
        if title_norm and normalized == title_norm:
            start_index += 1
            continue
        if _looks_like_local_clause_heading(line):
            start_index += 1
            continue
        if len(line) > 28 or re.search(r"[。；;!?！？]$", line):
            start_index += 1
            continue
        break

    parts: list[str] = []
    for line in raw_lines[start_index : start_index + 4]:
        value = str(line or "").strip()
        if not value:
            continue
        if re.match(r"^\d+(?:[.)、]?)?$", value):
            break
        if len(value) > 24:
            break
        if re.search(r"[。；;!?！？]$", value):
            break
        parts.append(value)
    if len(parts) < 2:
        return ""
    return " ".join(parts).strip()


def _is_generic_table_anchor_text(text: str) -> bool:
    normalized = _normalize_match_text(text)
    if len(normalized) < 4:
        return True
    return normalized in {
        "序号",
        "项目",
        "要求",
        "备注",
        "定义",
        "描述",
        "mm",
        "mm°",
        "频率hz",
        "频差hz",
        "振幅mm",
        "序号项目要求",
        "序号项目要求备注",
        "序号定义描述备注",
    }


def _build_current_entry_anchor_fragments(
    *,
    table_text: str,
    table_title: str,
    before_context_text: str,
) -> list[tuple[str, int]]:
    seen: set[str] = set()
    fragments: list[tuple[str, int]] = []

    def add(value: str, weight: int) -> None:
        normalized = _normalize_match_text(value)
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        fragments.append((normalized, weight))

    raw_lines = _iter_table_raw_lines(table_text)
    if raw_lines:
        local_clause_anchor = _extract_local_clause_anchor_prefix(raw_lines[0])
        if local_clause_anchor:
            add(local_clause_anchor, 26)
        add(raw_lines[0], 22)
        compact_band = _build_compact_anchor_band(raw_lines, table_title=table_title)
        if compact_band:
            add(compact_band, 16)

    if table_title:
        local_clause_anchor = _extract_local_clause_anchor_prefix(table_title)
        if local_clause_anchor:
            add(local_clause_anchor, 24)
        add(table_title, 18)

    previous_lines = _iter_table_raw_lines(before_context_text)
    if previous_lines:
        previous_last = previous_lines[-1]
        if (
            _looks_like_local_clause_heading(previous_last)
            or _looks_like_headingish_single_cell(previous_last)
            or _looks_like_plain_heading_label(previous_last)
        ):
            add(previous_last, 20)

    return fragments


def _build_recovery_required_match_fragments(
    *,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
) -> list[tuple[str, int]]:
    seen: set[str] = set()
    fragments: list[tuple[str, int]] = []

    def add(value: str, weight: int) -> None:
        normalized = _normalize_match_text(value)
        if len(normalized) < 2 or normalized in seen or _is_generic_table_anchor_text(value):
            return
        seen.add(normalized)
        fragments.append((normalized, weight))

    raw_lines = _iter_table_raw_lines(table_text)
    compact_band = _build_compact_anchor_band(raw_lines, table_title=table_title)
    if compact_band:
        add(compact_band, 18)

    title_norm = _normalize_match_text(table_title)
    for line in raw_lines[:6]:
        normalized = _normalize_match_text(line)
        if not normalized or (title_norm and normalized == title_norm):
            continue
        if _looks_like_local_clause_heading(line):
            continue
        add(line, 12 if not fragments else 8)
        if len(fragments) >= 3:
            break

    if headers:
        header_line = " ".join(str(cell or "").strip() for cell in headers[:4] if str(cell or "").strip())
        if header_line:
            add(header_line, 10)

    if rows:
        first_row_line = " ".join(str(cell or "").strip() for cell in rows[0][:4] if str(cell or "").strip())
        if first_row_line:
            add(first_row_line, 8)

    return fragments


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
    raw_lines = _iter_table_raw_lines(table_text)
    compact_band = _build_compact_anchor_band(raw_lines, table_title=table_title)
    if compact_band:
        add(compact_band, 11)
    for index, line in enumerate(raw_lines[:4]):
        add(line, 8 if index == 0 else 5)
    if len(raw_lines) > 6:
        for index, line in enumerate(raw_lines[-4:]):
            add(line, 6 if index >= 2 else 4)
    for header in headers[:12]:
        add(header, 4)
    for row in rows[:4]:
        joined = " ".join(str(cell or "").strip() for cell in row if str(cell or "").strip())
        add(joined, 5)
        for cell in row[:8]:
            add(str(cell or ""), 2)
    for row in rows[-2:]:
        joined = " ".join(str(cell or "").strip() for cell in row if str(cell or "").strip())
        add(joined, 4)
    for note in notes[:2]:
        add(note, 1)
    return fragments


def _table_current_column_count(headers: list[str], rows: list[list[str]]) -> int:
    return max(
        len(headers),
        max((len(row) for row in rows if isinstance(row, list)), default=0),
    )


def _extract_numbered_section_continuation_segment(
    candidate: dict[str, Any],
    *,
    current_col_count: int,
) -> dict[str, Any] | None:
    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if (
        not candidate_rows
        or not candidate_boxes
        or len(candidate_rows) != len(candidate_boxes)
    ):
        return None

    first_numbered_index: int | None = None
    for index, row in enumerate(candidate_rows):
        if _candidate_row_section_number(row):
            first_numbered_index = index
            break
    if first_numbered_index is None or first_numbered_index <= 0:
        return None

    continuation_rows = [
        row
        for row in candidate_rows[:first_numbered_index]
        if isinstance(row, list) and any(str(cell or "").strip() for cell in row)
    ]
    if len(continuation_rows) < 2:
        return None
    if any(_candidate_row_section_number(row) for row in continuation_rows):
        return None

    candidate_col_count = max((len(row) for row in continuation_rows), default=0)
    if current_col_count > 0 and abs(candidate_col_count - current_col_count) > 1:
        return None

    informative_rows = sum(
        1
        for row in continuation_rows
        if any(str(cell or "").strip() for cell in row[2:])
    )
    if informative_rows < min(2, len(continuation_rows)):
        return None

    boxes = [
        tuple(float(value) for value in candidate_boxes[index][:4])
        for index in range(first_numbered_index)
        if isinstance(candidate_boxes[index], (list, tuple)) and len(candidate_boxes[index]) >= 4
    ]
    if not boxes:
        return None

    bbox = (
        min(float(box[0]) for box in boxes),
        min(float(box[1]) for box in boxes),
        max(float(box[2]) for box in boxes),
        max(float(box[3]) for box in boxes),
    )
    return {
        "candidate": candidate,
        "rows": continuation_rows,
        "bbox": bbox,
        "stop_index": first_numbered_index,
    }


def _page_has_table_match_signal(
    document,
    *,
    source_key: str,
    page_index: int,
    match_fragments: list[tuple[str, int]],
    headers: list[str],
    slice_rows: list[list[str]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> bool:
    candidates = _load_pdf_table_candidates(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=detection_cache,
    )
    if not candidates:
        return False

    current_col_count = _table_current_column_count(headers, slice_rows)
    best_score = 0
    for candidate in candidates:
        best_score = max(
            best_score,
            _score_pdf_table_candidate(str(candidate.get("text") or ""), match_fragments),
        )
        if headers and slice_rows and _match_row_slice_bbox(candidate, headers=headers, slice_rows=slice_rows) is not None:
            return True
        if _extract_numbered_section_continuation_segment(candidate, current_col_count=current_col_count):
            return True
    return best_score >= 3


def _last_page_numbered_section_may_continue(
    candidates: list[dict[str, Any]],
    *,
    match_fragments: list[tuple[str, int]],
    slice_rows: list[list[str]],
) -> bool:
    target_rows = [row for row in slice_rows if isinstance(row, list) and row]
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number or not candidates:
        return False

    chosen = _select_best_pdf_table_candidate(candidates, match_fragments)
    if chosen is None:
        return False

    candidate_rows = chosen.get("rows_text") or []
    section_start = _find_candidate_section_start(candidate_rows, section_number)
    if section_start is None:
        return False
    section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)
    return section_stop >= len(candidate_rows)


def _refine_table_page_indices(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    match_fragments: list[tuple[str, int]],
    headers: list[str],
    slice_rows: list[list[str]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> list[int]:
    refined = sorted({int(page_index) for page_index in page_indices if int(page_index) >= 0})
    if not refined:
        return []

    while len(refined) > 1 and not _page_has_table_match_signal(
        document,
        source_key=source_key,
        page_index=refined[0],
        match_fragments=match_fragments,
        headers=headers,
        slice_rows=slice_rows,
        detection_cache=detection_cache,
    ):
        refined.pop(0)

    while len(refined) > 1 and not _page_has_table_match_signal(
        document,
        source_key=source_key,
        page_index=refined[-1],
        match_fragments=match_fragments,
        headers=headers,
        slice_rows=slice_rows,
        detection_cache=detection_cache,
    ):
        refined.pop()

    current_col_count = _table_current_column_count(headers, slice_rows)
    while refined and refined[-1] + 1 < int(document.page_count):
        last_page_candidates = _load_pdf_table_candidates(
            document,
            source_key=source_key,
            page_index=refined[-1],
            detection_cache=detection_cache,
        )
        if not _last_page_numbered_section_may_continue(
            last_page_candidates,
            match_fragments=match_fragments,
            slice_rows=slice_rows,
        ):
            break

        next_page_index = refined[-1] + 1
        next_page_candidates = _load_pdf_table_candidates(
            document,
            source_key=source_key,
            page_index=next_page_index,
            detection_cache=detection_cache,
        )
        has_continuation = any(
            _extract_numbered_section_continuation_segment(candidate, current_col_count=current_col_count)
            for candidate in next_page_candidates
        )
        if not has_continuation:
            break
        refined.append(next_page_index)

    return refined


def _coerce_numbered_section_continuation_rows(
    rows: list[list[Any]],
    *,
    current_col_count: int,
    section_number: str,
    section_title: str,
) -> list[list[str]]:
    normalized_rows = [_normalize_pdf_candidate_row(row) for row in rows if isinstance(row, list)]
    normalized_rows = [row for row in normalized_rows if any(cell for cell in row)]
    if not normalized_rows:
        return []

    max_columns = max(current_col_count, max((len(row) for row in normalized_rows), default=0))
    normalized_rows = [(row + [""] * max_columns)[:max_columns] for row in normalized_rows]
    normalized_rows = _drop_empty_pdf_separator_columns(normalized_rows)
    if not normalized_rows:
        return []

    target_columns = max(current_col_count, max((len(row) for row in normalized_rows), default=0))
    coerced: list[list[str]] = []
    previous_norm = ""
    for row in normalized_rows:
        if current_col_count >= len(row) + 2:
            current = ([section_number, section_title, *row] + [""] * target_columns)[:target_columns]
        else:
            current = (row + [""] * target_columns)[:target_columns]
        if target_columns >= 1 and not str(current[0] or "").strip():
            current[0] = section_number
        if target_columns >= 2 and not str(current[1] or "").strip():
            current[1] = section_title
        normalized = _normalize_match_text(" ".join(str(cell or "").strip() for cell in current if str(cell or "").strip()))
        if normalized and normalized == previous_norm:
            continue
        previous_norm = normalized
        coerced.append(current)
    return coerced


def _append_numbered_section_continuation_rows(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    headers: list[str],
    rows: list[list[str]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> list[list[str]]:
    target_rows = [row for row in rows if isinstance(row, list) and row]
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number or len(page_indices) <= 1:
        return [list(row) for row in rows if isinstance(row, list)]

    section_title = ""
    if target_rows and len(target_rows[0]) > 1:
        section_title = str(target_rows[0][1] or "").strip()
    elif len(headers) > 1:
        section_title = str(headers[1] or "").strip()
    current_col_count = _table_current_column_count(headers, target_rows)
    extended_rows = [list(row) for row in target_rows]

    for page_index in page_indices[1:]:
        candidates = _load_pdf_table_candidates(
            document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=detection_cache,
        )
        segment = next(
            (
                _extract_numbered_section_continuation_segment(candidate, current_col_count=current_col_count)
                for candidate in candidates
                if _extract_numbered_section_continuation_segment(candidate, current_col_count=current_col_count)
            ),
            None,
        )
        if segment is None:
            break
        continuation_rows = _coerce_numbered_section_continuation_rows(
            segment["rows"],
            current_col_count=current_col_count,
            section_number=section_number,
            section_title=section_title,
        )
        if not continuation_rows:
            break
        extended_rows.extend(continuation_rows)

    return extended_rows


def _resolve_cached_source_snapshot_meta(
    *,
    source_path: Path,
    source_page: int,
    page_end: int,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    if fitz is None or not source_path.exists():
        return {}

    with fitz.open(str(source_path)) as document:
        start_index = max(0, source_page - 1)
        if start_index >= int(document.page_count):
            return {}
        end_index = min(int(document.page_count) - 1, max(start_index, page_end - 1))
        page_indices = list(range(start_index, end_index + 1))
        match_fragments = _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
        )
        refined = _refine_table_page_indices(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            match_fragments=match_fragments,
            headers=headers,
            slice_rows=rows,
            detection_cache=source_table_detection_cache,
        )
    if not refined:
        return {}
    return {
        "resolved_page_start": refined[0] + 1,
        "resolved_page_end": refined[-1] + 1,
        "page_span_count": len(refined),
    }


def _extend_structured_table_rows_from_source(
    *,
    source_file_path: str,
    source_page: int,
    source_page_end: int,
    table_text: str,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    notes: list[str],
    source_table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> tuple[list[list[str]], int, int]:
    source_path = Path(str(source_file_path or "").strip())
    if (
        fitz is None
        or not source_path.exists()
        or source_path.suffix.lower() != ".pdf"
        or source_page <= 0
        or not headers
        or not rows
    ):
        end_page = source_page_end if source_page_end >= source_page else source_page
        return rows, source_page, end_page

    page_end = source_page_end if source_page_end >= source_page else source_page
    with fitz.open(str(source_path)) as document:
        start_index = max(0, source_page - 1)
        if start_index >= int(document.page_count):
            return rows, source_page, page_end
        end_index = min(int(document.page_count) - 1, max(start_index, page_end - 1))
        page_indices = list(range(start_index, end_index + 1))
        match_fragments = _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=headers,
            rows=rows,
            notes=notes,
        )
        refined = _refine_table_page_indices(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            match_fragments=match_fragments,
            headers=headers,
            slice_rows=rows,
            detection_cache=source_table_detection_cache,
        )
        if not refined:
            return rows, source_page, page_end
        extended_rows = _append_numbered_section_continuation_rows(
            document,
            source_key=str(source_path.resolve()),
            page_indices=refined,
            headers=headers,
            rows=rows,
            detection_cache=source_table_detection_cache,
        )

    return (
        extended_rows if len(extended_rows) >= len(rows) else rows,
        refined[0] + 1,
        refined[-1] + 1,
    )


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
    prefer_clause_window: bool = False,
    max_height_px: int | None = None,
    allow_next_clause_stop: bool = True,
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
        page_indices = _refine_table_page_indices(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            match_fragments=match_fragments,
            headers=headers,
            slice_rows=rows,
            detection_cache=source_table_detection_cache,
        )
        if not page_indices:
            raise ValueError("no relevant source pages for table snapshot")
        before_anchor_candidates = _build_context_anchor_fragments(before_context_text, from_end=True)
        after_anchor_candidates = _build_context_anchor_fragments(after_context_text, from_end=False)
        self_anchor_candidates = _build_current_entry_anchor_fragments(
            table_text=table_text,
            table_title=table_title,
            before_context_text=before_context_text,
        )
        start_anchor, end_anchor = _find_context_anchor_window(
            document,
            source_key=str(source_path.resolve()),
            page_indices=page_indices,
            before_candidates=before_anchor_candidates,
            after_candidates=after_anchor_candidates,
            self_candidates=self_anchor_candidates,
            detection_cache=source_line_detection_cache,
            prefer_last_before=_prefer_last_before_context_anchor(before_context_text),
            promote_after_band_start=_should_promote_anchor_to_band_start(after_context_text),
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
            line_detection_cache=source_line_detection_cache,
            source_page_floor_offsets=source_page_floor_offsets,
            prefer_anchor_window=prefer_clause_window,
            allow_next_clause_stop=allow_next_clause_stop,
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
        render_meta["resolved_page_start"] = page_indices[0] + 1
        render_meta["resolved_page_end"] = page_indices[-1] + 1
        images = _render_pdf_crop_plan(document, crop_plan, max_width_px=max_width_px)

    return _compose_vertical_images(
        images,
        max_width_px=max_width_px,
        max_height_px=max_height_px,
    ), render_meta


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
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
    prefer_anchor_window: bool = False,
    allow_next_clause_stop: bool = True,
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    effective_start_page = int(start_anchor.get("page_index")) if start_anchor else page_indices[0]
    effective_end_anchor = end_anchor
    if allow_next_clause_stop and start_anchor is not None:
        next_clause_anchor = _find_next_local_clause_anchor(
            document,
            source_key=source_key,
            page_indices=page_indices,
            start_anchor=start_anchor,
            detection_cache=None,
            table_detection_cache=detection_cache,
        )
        if next_clause_anchor is not None:
            next_clause_anchor = _prefer_prior_generic_compact_band_anchor(
                document,
                source_key=source_key,
                anchor=next_clause_anchor,
                detection_cache=None,
            )
            if effective_end_anchor is None:
                effective_end_anchor = next_clause_anchor
            elif not _looks_like_boundary_anchor_text(str(effective_end_anchor.get("text") or "")):
                effective_end_anchor = next_clause_anchor
            elif prefer_anchor_window and (
                int(effective_end_anchor.get("page_index", -1)) == int(start_anchor.get("page_index", -1))
                and int(next_clause_anchor.get("page_index", -1)) > int(start_anchor.get("page_index", -1))
            ):
                effective_end_anchor = next_clause_anchor
            elif prefer_anchor_window and (
                int(next_clause_anchor.get("page_index", -1)) < int(effective_end_anchor.get("page_index", -1))
                or (
                    int(next_clause_anchor.get("page_index", -1)) == int(effective_end_anchor.get("page_index", -1))
                    and float(next_clause_anchor["bbox"][1]) < float(effective_end_anchor["bbox"][1]) - 2.0
                )
            ):
                effective_end_anchor = next_clause_anchor
    effective_end_page = int(effective_end_anchor.get("page_index")) if effective_end_anchor else page_indices[-1]
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
        chosen = _select_best_pdf_crop_candidate(
            candidates,
            match_fragments=match_fragments,
            headers=headers,
            slice_rows=slice_rows,
            end_anchor=effective_end_anchor if page_index == effective_end_page else None,
        )
        chosen_score = _score_pdf_table_candidate(str(chosen.get("text") or ""), match_fragments) if chosen else 0
        row_slice_preview = (
            _match_row_slice_bbox(chosen, headers=headers, slice_rows=slice_rows)
            if chosen is not None and headers and slice_rows
            else None
        )
        section_start_anchor: dict[str, Any] | None = None
        section_end_anchor: dict[str, Any] | None = None
        section_start_box: tuple[float, float, float, float] | None = None
        next_section_box: tuple[float, float, float, float] | None = None
        if chosen is not None and slice_rows:
            section_start_anchor, section_end_anchor = _find_row_slice_section_anchor_window(
                document,
                source_key=source_key,
                page_index=page_index,
                candidate=chosen,
                headers=headers,
                slice_rows=slice_rows,
                detection_cache=line_detection_cache,
            )
            section_start_box, next_section_box = _find_row_slice_section_box_window(
                chosen,
                slice_rows=slice_rows,
            )
        page_end_anchor = effective_end_anchor if page_index == effective_end_page else None
        page_end_anchor_from_section = False
        if page_end_anchor is None and section_end_anchor is not None:
            page_end_anchor = section_end_anchor
            page_end_anchor_from_section = True
        if _should_ignore_section_end_anchor_for_clean_single_row_slice(
            page_end_anchor_from_section=page_end_anchor_from_section,
            chosen=chosen,
            headers=headers,
            slice_rows=slice_rows,
            row_slice_bbox=row_slice_preview,
            end_anchor=page_end_anchor,
        ):
            page_end_anchor = None
        if (
            chosen is not None
            and page_end_anchor is not None
            and row_slice_preview is not None
            and len(slice_rows) > 1
            and _row_slice_candidate_mentions_anchor_text(
                chosen,
                headers=headers,
                slice_rows=slice_rows,
                anchor=page_end_anchor,
            )
        ):
            page_end_anchor = None
        if (
            chosen is not None
            and page_end_anchor is not None
            and row_slice_preview is None
        ):
            end_bbox = tuple(page_end_anchor.get("bbox") or ())
            chosen_bbox = tuple(chosen.get("bbox") or ())
            if len(end_bbox) >= 4 and len(chosen_bbox) >= 4:
                if (
                    float(end_bbox[0]) >= float(chosen_bbox[0]) - 4.0
                    and float(end_bbox[2]) <= float(chosen_bbox[2]) + 4.0
                    and float(end_bbox[1]) >= float(chosen_bbox[1]) - 4.0
                    and float(end_bbox[3]) <= float(chosen_bbox[3]) + 4.0
                    and (
                        not _looks_like_boundary_anchor_text(str(page_end_anchor.get("text") or ""))
                        or (len(headers) >= 6 and len(slice_rows) >= 6)
                    )
                ):
                    page_end_anchor = None
        row_slice_conflicts_with_start_anchor = (
            row_slice_preview is not None
            and not _row_slice_bbox_is_start_anchor_compatible(
                start_anchor=start_anchor if page_index == effective_start_page else None,
                row_slice_bbox=row_slice_preview,
            )
        )
        row_slice_mentions_end_anchor = (
            chosen is not None
            and page_index == effective_end_page
            and page_end_anchor is not None
            and _row_slice_candidate_mentions_anchor_text(
                chosen,
                headers=headers,
                slice_rows=slice_rows,
                anchor=page_end_anchor,
            )
        )
        anchor_window_hint_bbox = (
            row_slice_preview
            if prefer_anchor_window and _should_use_anchor_window_row_slice_hint(start_anchor, row_slice_preview)
            else None
        )
        force_anchor_window_tighten = (
            not prefer_anchor_window
            and page_index == effective_start_page
            and page_index == effective_end_page
            and start_anchor is not None
            and effective_end_anchor is not None
            and (
                row_slice_conflicts_with_start_anchor
                or (row_slice_mentions_end_anchor and (row_slice_preview is None or len(slice_rows) <= 1))
            )
        )
        if (
            chosen is not None
            and not prefer_anchor_window
            and page_end_anchor is not None
            and page_index == effective_end_page
            and page_index != effective_start_page
            and chosen_score < 3
            and row_slice_preview is None
            and (headers or slice_rows)
        ):
            continue
        page_start_anchor = start_anchor if page_index == effective_start_page else None
        if page_start_anchor is None and section_start_anchor is not None and row_slice_preview is None:
            page_start_anchor = section_start_anchor
        bbox = _compose_page_crop_bbox(
            page,
            chosen=None if prefer_anchor_window else chosen,
            start_anchor=page_start_anchor,
            end_anchor=page_end_anchor,
            headers=headers,
            slice_rows=slice_rows,
            page_floor=float((source_page_floor_offsets or {}).get(page_index + 1, 0.0)),
        )
        if force_anchor_window_tighten and bbox is not None:
            content_bboxes = _collect_anchor_window_content_bboxes(
                page,
                window_top=float(start_anchor["bbox"][1]),
                window_bottom=float(page_end_anchor["bbox"][1]) if page_end_anchor else float(page.rect.height),
            )
            if content_bboxes:
                bbox = _tighten_anchor_window_crop_bbox(
                    page.rect,
                    current_bbox=bbox,
                    content_bboxes=content_bboxes,
                    start_anchor=start_anchor,
                    end_anchor=effective_end_anchor,
                )
        if prefer_anchor_window and bbox is not None:
            window_top = float(page_start_anchor["bbox"][1]) if page_start_anchor else 0.0
            if page_index == effective_start_page and anchor_window_hint_bbox is not None:
                window_top = min(window_top, float(anchor_window_hint_bbox[1]))
            window_bottom = (
                float(page_end_anchor["bbox"][1])
                if page_index == effective_end_page and page_end_anchor
                else float(page.rect.height)
            )
            content_bboxes = _collect_anchor_window_content_bboxes(
                page,
                window_top=window_top,
                window_bottom=window_bottom,
            )
            if (
                page_index == effective_end_page
                and page_index != effective_start_page
                and page_end_anchor is not None
                and not content_bboxes
            ):
                continue
            bbox = _tighten_anchor_window_crop_bbox(
                page.rect,
                current_bbox=bbox,
                content_bboxes=content_bboxes,
                start_anchor=page_start_anchor,
                end_anchor=page_end_anchor,
            )
        elif (
            bbox is not None
            and section_start_anchor is not None
            and row_slice_preview is None
        ):
            window_top = float(section_start_anchor["bbox"][1])
            if section_start_box is not None and section_start_anchor is None:
                window_top = min(window_top, float(section_start_box[1]))
            window_bottom = float(page_end_anchor["bbox"][1]) if page_end_anchor is not None else float(page.rect.height)
            if next_section_box is not None:
                window_bottom = min(window_bottom, max(window_top + 20.0, float(next_section_box[1]) - 12.0))
            content_bboxes = _collect_anchor_window_content_bboxes(
                page,
                window_top=window_top,
                window_bottom=window_bottom,
            )
            if content_bboxes:
                tightened = _tighten_anchor_window_crop_bbox(
                    page.rect,
                    current_bbox=bbox,
                    content_bboxes=content_bboxes,
                    start_anchor=section_start_anchor,
                    end_anchor=page_end_anchor,
                )
                bbox = (
                    tightened[0],
                    max(float(tightened[1]), float(section_start_anchor["bbox"][1]) - 2.0),
                    tightened[2],
                    tightened[3],
                )
        plan.append((page_index, bbox))
    return plan


def _build_full_page_span_crop_plan(
    document,
    page_indices: list[int],
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    for page_index in page_indices:
        page = document.load_page(page_index)
        bbox = _build_full_page_content_crop_bbox(page)
        plan.append((page_index, bbox))
    return plan


def _build_full_page_content_crop_bbox(page) -> tuple[float, float, float, float] | None:
    content_bboxes = _collect_anchor_window_content_bboxes(
        page,
        window_top=0.0,
        window_bottom=float(page.rect.height),
    )
    if not content_bboxes:
        return None

    page_width = float(page.rect.width)
    page_height = float(page.rect.height)
    left = max(0.0, min(float(bbox[0]) for bbox in content_bboxes) - 12.0)
    top = max(0.0, min(float(bbox[1]) for bbox in content_bboxes) - 10.0)
    right = min(page_width, max(float(bbox[2]) for bbox in content_bboxes) + 12.0)
    bottom = min(page_height, max(float(bbox[3]) for bbox in content_bboxes) + 10.0)
    if right <= left + 20.0:
        right = min(page_width, left + 20.0)
    if bottom <= top + 20.0:
        bottom = min(page_height, top + 20.0)
    return (left, top, right, bottom)


def _should_use_anchor_window_row_slice_hint(
    start_anchor: dict[str, Any] | None,
    row_slice_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if start_anchor is None or row_slice_bbox is None:
        return False
    anchor_text = str(start_anchor.get("text") or "")
    if _looks_like_local_clause_heading(anchor_text):
        return False
    anchor_bbox = tuple(start_anchor.get("bbox") or ())
    if len(anchor_bbox) < 4 or len(row_slice_bbox) < 4:
        return False
    delta = float(anchor_bbox[1]) - float(row_slice_bbox[1])
    return 8.0 <= delta <= 140.0


def _complex_full_page_snapshot_width_px(row: dict[str, Any]) -> int:
    family = str(row.get("complex_family") or "").strip().lower()
    if family == "merged_requirement_matrix":
        return _MERGED_REQUIREMENT_COMPLEX_FULL_PAGE_SNAPSHOT_MAX_WIDTH_PX
    return _COMPLEX_FULL_PAGE_SNAPSHOT_MAX_WIDTH_PX


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
        if from_end and _looks_like_reference_only_line(line):
            continue
        variants = [line]
        local_clause_variant = _extract_local_clause_anchor_prefix(line)
        if local_clause_variant:
            variants.append(local_clause_variant)
        plain_numbered_heading = _looks_like_plain_numbered_heading_line(line)
        if not plain_numbered_heading:
            variants.append(_strip_anchor_line_prefix(line))
        label_variant = _extract_anchor_label_prefix(line)
        if label_variant:
            variants.append(label_variant)
        mixed_variants = [] if plain_numbered_heading else _extract_mixed_heading_anchor_parts(line)
        variants.extend(mixed_variants)
        for variant in variants:
            normalized = _normalize_match_text(variant)
            if len(normalized) < 2 or normalized in seen:
                continue
            seen.add(normalized)
            if variant == local_clause_variant:
                weight = 20 - index * 2
            elif variant == label_variant:
                weight = 18
            elif variant in mixed_variants:
                weight = 17
            else:
                weight = 12 - index * 3
            candidates.append((normalized, max(3, weight)))
    return candidates


def _strip_anchor_line_prefix(line: str) -> str:
    return re.sub(r"^\s*[0-9一二三四五六七八九十IVXivx\.\-、()（）]+\s*", "", str(line or "")).strip()


def _looks_like_plain_numbered_heading_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value or "|" in value or len(value) > 80:
        return False
    if re.search(r"[。！？!?]", value):
        return False
    return bool(re.match(r"^\d{1,3}\s+[A-Za-z\u4e00-\u9fff]", value))


def _extract_anchor_label_prefix(line: str) -> str:
    text = str(line or "").strip()
    if not text:
        return ""
    match = re.match(r"^([\u4e00-\u9fffA-Za-z ]{1,12})\s*[：:]", text)
    if not match:
        return ""
    return match.group(1).strip()


def _extract_mixed_heading_anchor_parts(line: str) -> list[str]:
    text = str(line or "").strip()
    if not text:
        return []

    variants: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        normalized = _normalize_match_text(value)
        if len(normalized) < 2 or normalized in seen:
            return
        seen.add(normalized)
        variants.append(value.strip())

    for match in re.finditer(r"[\u4e00-\u9fff][\u4e00-\u9fff0-9（）()]{1,40}", text):
        add(match.group(0))

    english_spans = re.findall(r"[A-Za-z][A-Za-z0-9/&().\- ]{3,60}", text)
    for span in english_spans[:2]:
        words = [word for word in span.split() if word]
        if not words:
            continue
        add(" ".join(words[: min(3, len(words))]))

    return variants


def _find_context_anchor_window(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    before_candidates: list[tuple[str, int]],
    after_candidates: list[tuple[str, int]],
    self_candidates: list[tuple[str, int]] | None = None,
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    prefer_last_before: bool = True,
    promote_after_band_start: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    start_anchor = _find_context_anchor(
        document,
        source_key=source_key,
        page_indices=page_indices,
        candidates=before_candidates,
        detection_cache=detection_cache,
        prefer_last=prefer_last_before,
    )
    current_anchor = _find_context_anchor(
        document,
        source_key=source_key,
        page_indices=page_indices,
        candidates=self_candidates or [],
        detection_cache=detection_cache,
        prefer_score_across_pages=True,
    )
    start_anchor = _prefer_later_context_anchor(start_anchor, current_anchor)

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
        min_score=8,
    )
    if promote_after_band_start and end_anchor is not None and not _looks_like_local_clause_heading(str(end_anchor.get("text") or "")):
        end_anchor = _promote_anchor_to_compact_band_start(
            document,
            source_key=source_key,
            anchor=end_anchor,
            detection_cache=detection_cache,
        )
    return start_anchor, end_anchor


def _prefer_last_before_context_anchor(text: str) -> bool:
    lines = _iter_table_raw_lines(text)
    if not lines:
        return True
    tail = str(lines[-1] or "").strip()
    if (
        _looks_like_local_clause_heading(tail)
        or _looks_like_headingish_single_cell(tail)
        or _looks_like_plain_heading_label(tail)
    ):
        return False
    return True


def _should_promote_anchor_to_band_start(text: str) -> bool:
    lines = _iter_table_raw_lines(text)
    if not lines:
        return False
    tail = str(lines[-1] or "").strip()
    if _looks_like_local_clause_heading(tail):
        return False
    return (
        _looks_like_headingish_single_cell(tail)
        or _looks_like_plain_heading_label(tail)
        or _looks_like_attachment_table_title(tail)
    )


def _is_compact_band_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if len(value) > 48:
        return False
    if any(mark in value for mark in ("。", "；", ";")):
        return False
    return True


def _looks_like_generic_compact_band_header(text: str) -> bool:
    normalized = _normalize_match_text(text)
    if not normalized:
        return False
    return normalized in {
        "序号",
        "项目",
        "位置",
        "内容",
        "名称",
        "备注",
        "页码",
        "附件名称",
        "pds表位置",
        "pds表位置内容",
    }


def _promote_anchor_to_compact_band_start(
    document,
    *,
    source_key: str,
    anchor: dict[str, Any],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    page_index = int(anchor.get("page_index", -1))
    if page_index < 0:
        return anchor
    lines = _load_pdf_page_line_records(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=detection_cache,
    )
    if not lines:
        return anchor

    anchor_bbox = tuple(anchor.get("bbox") or ())
    anchor_text = str(anchor.get("text") or "")
    anchor_index = -1
    for index, line in enumerate(lines):
        if tuple(line.get("bbox") or ()) == anchor_bbox and str(line.get("text") or "") == anchor_text:
            anchor_index = index
            break
    if anchor_index < 0:
        for index, line in enumerate(lines):
            bbox = tuple(line.get("bbox") or ())
            if len(bbox) < 4:
                continue
            if abs(float(bbox[1]) - float(anchor_bbox[1] if len(anchor_bbox) >= 2 else -9999.0)) <= 3.0:
                anchor_index = index
                break
    if anchor_index < 0 or not _is_compact_band_line(anchor_text):
        return anchor

    start_index = anchor_index
    collected = 1
    while start_index > 0:
        current_bbox = tuple(lines[start_index].get("bbox") or ())
        previous = lines[start_index - 1]
        previous_bbox = tuple(previous.get("bbox") or ())
        previous_text = str(previous.get("text") or "")
        if len(current_bbox) < 4 or len(previous_bbox) < 4 or not _is_compact_band_line(previous_text):
            break
        gap = float(current_bbox[1]) - float(previous_bbox[3])
        if gap > 16.0 and not (collected >= 3 and gap <= 40.0):
            break
        start_index -= 1
        collected += 1

    if start_index == anchor_index or collected < 3:
        return anchor
    promoted = dict(anchor)
    promoted["bbox"] = tuple(lines[start_index]["bbox"])
    promoted["text"] = str(lines[start_index].get("text") or promoted.get("text") or "")
    return promoted


def _prefer_prior_generic_compact_band_anchor(
    document,
    *,
    source_key: str,
    anchor: dict[str, Any],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    page_index = int(anchor.get("page_index", -1))
    anchor_bbox = tuple(anchor.get("bbox") or ())
    if page_index < 0 or len(anchor_bbox) < 4 or float(anchor_bbox[1]) > 220.0:
        return anchor
    lines = _load_pdf_page_line_records(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=detection_cache,
    )
    if not lines:
        return anchor

    anchor_text = str(anchor.get("text") or "")
    anchor_index = -1
    for index, line in enumerate(lines):
        if tuple(line.get("bbox") or ()) == anchor_bbox and str(line.get("text") or "") == anchor_text:
            anchor_index = index
            break
    if anchor_index <= 0:
        return anchor

    anchor_top = float(anchor_bbox[1])
    candidates = []
    for line in lines[:anchor_index]:
        bbox = tuple(line.get("bbox") or ())
        if len(bbox) < 4:
            continue
        if float(bbox[1]) > 220.0:
            continue
        if anchor_top - float(bbox[3]) > 72.0:
            continue
        text = str(line.get("text") or "")
        if not (_is_compact_band_line(text) and _looks_like_generic_compact_band_header(text)):
            continue
        candidates.append(line)
    if not candidates:
        return anchor

    chosen = min(candidates, key=lambda item: float((item.get("bbox") or (0.0, 9999.0, 0.0, 0.0))[1]))
    promoted = dict(anchor)
    promoted["bbox"] = tuple(chosen.get("bbox") or anchor_bbox)
    promoted["text"] = str(chosen.get("text") or promoted.get("text") or "")
    return promoted


def _find_next_local_clause_anchor(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    start_anchor: dict[str, Any],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
    table_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
) -> dict[str, Any] | None:
    start_page_index = int(start_anchor.get("page_index", -1))
    start_y = float(start_anchor["bbox"][1]) if start_anchor.get("bbox") else 0.0
    for page_index in page_indices:
        if page_index < start_page_index:
            continue
        lines = _load_pdf_page_line_records(
            document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=detection_cache,
        )
        for line in lines:
            bbox = tuple(line.get("bbox") or ())
            if len(bbox) < 4:
                continue
            if page_index == start_page_index and float(bbox[1]) <= start_y + 2.0:
                continue
            text = str(line.get("text") or "").strip()
            if not (
                _looks_like_local_clause_heading(text)
                or _looks_like_headingish_single_cell(text)
                or _looks_like_attachment_heading_anchor(text)
            ):
                continue
            if _is_generic_table_anchor_text(text):
                continue
            if _looks_like_attachment_heading_anchor(text) and not _looks_like_boundary_anchor_text(text):
                continue
            if _anchor_bbox_is_inline_table_cell_noise(
                document,
                source_key=source_key,
                page_index=page_index,
                bbox=bbox,
                detection_cache=table_detection_cache,
            ):
                continue
            return {
                "page_index": page_index,
                "bbox": bbox,
                "score": 1,
                "text": text,
            }
    return None


def _looks_like_boundary_anchor_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        _looks_like_local_clause_heading(value)
        or _looks_like_headingish_single_cell(value)
        or _looks_like_plain_heading_label(value)
        or _looks_like_attachment_heading_anchor(value)
    )


def _anchor_bbox_is_inline_table_cell_noise(
    document,
    *,
    source_key: str,
    page_index: int,
    bbox: tuple[float, float, float, float],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> bool:
    if detection_cache is None and not hasattr(document, "load_page"):
        return False
    candidates = _load_pdf_table_candidates(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=detection_cache,
    )
    if not candidates:
        return False
    left = float(bbox[0])
    top = float(bbox[1])
    right = float(bbox[2])
    bottom = float(bbox[3])
    for candidate in candidates:
        candidate_bbox = tuple(candidate.get("bbox") or ())
        if len(candidate_bbox) < 4:
            continue
        cx0, cy0, cx1, cy1 = (float(value) for value in candidate_bbox[:4])
        if left < cx0 - 4.0 or right > cx1 + 4.0 or top < cy0 - 4.0 or bottom > cy1 + 4.0:
            continue
        if left > cx0 + 72.0 or (top > cy0 + 18.0 and left > cx0 + 40.0):
            return True
    return False


def _prefer_later_context_anchor(
    anchor: dict[str, Any] | None,
    current_anchor: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if current_anchor is None:
        return anchor
    if anchor is None:
        return current_anchor

    current_text = str(current_anchor.get("text") or "")
    anchor_text = str(anchor.get("text") or "")
    anchor_is_heading = (
        _looks_like_local_clause_heading(anchor_text)
        or _looks_like_headingish_single_cell(anchor_text)
        or _looks_like_plain_heading_label(anchor_text)
    )
    current_is_heading = (
        _looks_like_local_clause_heading(current_text)
        or _looks_like_headingish_single_cell(current_text)
        or _looks_like_plain_heading_label(current_text)
    )
    if anchor_is_heading and not current_is_heading:
        return anchor
    if current_is_heading and not anchor_is_heading:
        return current_anchor
    if _looks_like_local_clause_heading(current_text) and not _looks_like_local_clause_heading(anchor_text):
        return current_anchor

    anchor_page = int(anchor.get("page_index", -1))
    current_page = int(current_anchor.get("page_index", -1))
    if (
        current_page == anchor_page
        and _looks_like_local_clause_heading(current_text)
        and float(current_anchor["bbox"][1]) < float(anchor["bbox"][1]) - 2.0
    ):
        return current_anchor
    if current_page > anchor_page:
        return current_anchor
    if current_page < anchor_page:
        return anchor
    if float(current_anchor["bbox"][1]) >= float(anchor["bbox"][1]) - 2.0:
        return current_anchor
    return anchor


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
    min_score: int = 1,
    prefer_score_across_pages: bool = False,
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
        if int(page_match.get("score") or 0) < max(1, min_score):
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
        if prefer_score_across_pages:
            if int(candidate["score"]) > int(best["score"]):
                best = candidate
                continue
            if int(candidate["score"]) < int(best["score"]):
                continue
        if int(candidate["page_index"]) < int(best["page_index"]):
            best = candidate
            continue
        if int(candidate["page_index"]) > int(best["page_index"]):
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
            fragments = _split_embedded_anchor_line_fragments(text, line_bbox)
            if not fragments:
                fragments = [{"text": text, "bbox": line_bbox}]
            for fragment in fragments:
                fragment_text = str(fragment.get("text") or "").strip()
                fragment_bbox = tuple(fragment.get("bbox") or ())
                if not fragment_text or len(fragment_bbox) < 4:
                    continue
                lines.append(
                    {
                        "text": fragment_text,
                        "norm": _normalize_match_text(fragment_text),
                        "bbox": fragment_bbox,
                    }
                )

    lines.sort(key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])))
    if detection_cache is not None:
        detection_cache[cache_key] = lines
    return lines


def _split_embedded_anchor_line_fragments(
    text: str,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    value = str(text or "").strip()
    if not value:
        return []
    matches = list(_EMBEDDED_ANCHOR_LINE_SPLIT_RE.finditer(value))
    if not matches:
        return []

    starts = [match.start() for match in matches]
    if starts[0] > 0:
        starts = [0, *starts]
    starts = sorted(set(index for index in starts if 0 <= index < len(value)))
    if len(starts) < 2:
        return []

    chunks: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(value)
        chunk = str(value[start:end] or "").strip()
        if chunk:
            chunks.append(chunk)
    if len(chunks) < 2:
        return []

    x0, y0, x1, y1 = bbox
    height = max(1.0, float(y1) - float(y0))
    fragments: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        top = float(y0) + height * index / len(chunks)
        bottom = float(y0) + height * (index + 1) / len(chunks)
        fragments.append(
            {
                "text": chunk,
                "bbox": (float(x0), top, float(x1), bottom),
            }
        )
    return fragments


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
            elif not prefer_last:
                if bbox[1] < best["bbox"][1] - 2.0:
                    best = candidate
                elif abs(float(bbox[1]) - float(best["bbox"][1])) <= 12.0 and float(bbox[0]) < float(best["bbox"][0]) - 8.0:
                    best = candidate
    return best


def _collect_anchor_window_content_bboxes(
    page,
    *,
    window_top: float,
    window_bottom: float,
    include_drawings: bool = True,
) -> list[tuple[float, float, float, float]]:
    if window_bottom <= window_top + 2.0:
        return []
    try:
        raw = page.get_text("dict")
    except Exception:
        raw = {"blocks": []}

    content_bboxes: list[tuple[float, float, float, float]] = []
    for block in raw.get("blocks", []):
        block_type = int(block.get("type", 0))
        if block_type == 0:
            for line in block.get("lines", []):
                spans = [span for span in line.get("spans", []) if str(span.get("text") or "").strip()]
                if not spans:
                    continue
                bbox = line.get("bbox") or spans[0].get("bbox")
                if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                    continue
                try:
                    line_bbox = tuple(float(value) for value in bbox[:4])
                except (TypeError, ValueError):
                    continue
                if line_bbox[3] < window_top - 2.0 or line_bbox[1] >= window_bottom:
                    continue
                clipped_top = max(window_top, line_bbox[1])
                clipped_bottom = min(window_bottom, line_bbox[3])
                if clipped_bottom <= clipped_top + 0.5:
                    continue
                content_bboxes.append((line_bbox[0], clipped_top, line_bbox[2], clipped_bottom))
        elif block_type == 1:
            bbox = block.get("bbox")
            if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
                continue
            try:
                image_bbox = tuple(float(value) for value in bbox[:4])
            except (TypeError, ValueError):
                continue
            if image_bbox[3] < window_top - 2.0 or image_bbox[1] >= window_bottom:
                continue
            clipped_top = max(window_top, image_bbox[1])
            clipped_bottom = min(window_bottom, image_bbox[3])
            if clipped_bottom <= clipped_top + 0.5:
                continue
            content_bboxes.append((image_bbox[0], clipped_top, image_bbox[2], clipped_bottom))

    if include_drawings and content_bboxes:
        try:
            drawings = page.get_drawings()
        except Exception:
            drawings = []
        for item in drawings or []:
            rect = item.get("rect")
            if rect is None:
                continue
            try:
                drawing_bbox = (
                    float(rect.x0),
                    float(rect.y0),
                    float(rect.x1),
                    float(rect.y1),
                )
            except (AttributeError, TypeError, ValueError):
                continue
            if drawing_bbox[3] < window_top - 2.0 or drawing_bbox[1] >= window_bottom:
                continue
            clipped_top = max(window_top, drawing_bbox[1])
            clipped_bottom = min(window_bottom, drawing_bbox[3])
            if clipped_bottom <= clipped_top + 0.5:
                continue
            content_bboxes.append((drawing_bbox[0], clipped_top, drawing_bbox[2], clipped_bottom))
    return content_bboxes


def _tighten_shallow_single_row_slice_horizontal_bbox(
    page,
    *,
    current_bbox: tuple[float, float, float, float],
    row_slice_bbox: tuple[float, float, float, float] | None,
    slice_rows: list[list[str]],
) -> tuple[float, float, float, float]:
    if row_slice_bbox is None or len(slice_rows) != 1:
        return current_bbox

    current_width = float(current_bbox[2]) - float(current_bbox[0])
    current_height = float(current_bbox[3]) - float(current_bbox[1])
    if current_width <= 0.0 or current_height <= 0.0:
        return current_bbox
    if current_height / current_width >= 0.12:
        return current_bbox

    window_top = max(0.0, float(row_slice_bbox[1]) - 2.0)
    window_bottom = min(float(page.rect.height), float(row_slice_bbox[3]) + 2.0)
    content_bboxes = _collect_anchor_window_content_bboxes(
        page,
        window_top=window_top,
        window_bottom=window_bottom,
        include_drawings=False,
    )
    if not content_bboxes:
        return current_bbox

    content_left = min(float(bbox[0]) for bbox in content_bboxes)
    content_right = max(float(bbox[2]) for bbox in content_bboxes)
    tightened_left = max(0.0, content_left - 12.0)
    tightened_right = min(float(page.rect.width), content_right + 12.0)
    tightened_width = tightened_right - tightened_left
    if tightened_width <= 0.0:
        return current_bbox
    if tightened_width >= current_width - 24.0:
        return current_bbox
    if tightened_width >= current_width * 0.94:
        return current_bbox

    return (
        tightened_left,
        float(current_bbox[1]),
        tightened_right,
        float(current_bbox[3]),
    )


def _tighten_anchor_window_crop_bbox(
    page_rect,
    *,
    current_bbox: tuple[float, float, float, float],
    content_bboxes: list[tuple[float, float, float, float]],
    start_anchor: dict[str, Any] | None,
    end_anchor: dict[str, Any] | None,
) -> tuple[float, float, float, float]:
    if not content_bboxes:
        return current_bbox

    page_width = float(page_rect.width)
    page_height = float(page_rect.height)
    content_left = min(float(bbox[0]) for bbox in content_bboxes)
    content_top = min(float(bbox[1]) for bbox in content_bboxes)
    content_right = max(float(bbox[2]) for bbox in content_bboxes)
    content_bottom = max(float(bbox[3]) for bbox in content_bboxes)

    left_candidates = [content_left - 12.0]
    right_candidates = [content_right + 12.0]
    top_candidates = [content_top - 8.0]
    start_anchor_bbox: tuple[float, float, float, float] | None = None
    start_anchor_is_clause = False

    if start_anchor:
        anchor_bbox = tuple(float(value) for value in start_anchor["bbox"])
        anchor_text = str(start_anchor.get("text") or "")
        start_anchor_bbox = anchor_bbox
        start_anchor_is_clause = _looks_like_local_clause_heading(anchor_text)
        anchor_top_padding = 6.0 if start_anchor_is_clause else 12.0
        left_candidates.append(anchor_bbox[0] - 12.0)
        right_candidates.append(anchor_bbox[2] + 12.0)
        top_candidates.append(anchor_bbox[1] - anchor_top_padding)

    left = max(0.0, min(left_candidates))
    if start_anchor_is_clause and start_anchor_bbox is not None and content_top <= start_anchor_bbox[3] + 8.0:
        top = max(0.0, min(content_top, start_anchor_bbox[1]))
    else:
        top = max(0.0, min(top_candidates))
    right = min(page_width, max(right_candidates))

    end_limit = page_height
    if end_anchor:
        end_limit = min(end_limit, max(0.0, float(end_anchor["bbox"][1]) - 2.0))
    bottom = min(page_height, min(end_limit, content_bottom + 8.0))

    if bottom <= top + 20.0:
        bottom = min(page_height, top + 20.0)
    if right <= left + 20.0:
        right = min(page_width, left + 20.0)
    return (left, top, right, bottom)


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
    row_slice_bbox: tuple[float, float, float, float] | None = None
    numbered_section_row_slice = False
    end_anchor_text = str(end_anchor.get("text") or "").strip() if end_anchor is not None else ""
    row_slice_mentions_end_anchor = _row_slice_mentions_anchor_text(slice_rows, end_anchor) or (
        _row_slice_candidate_mentions_anchor_text(
            chosen,
            headers=headers,
            slice_rows=slice_rows,
            anchor=end_anchor,
        )
        if chosen is not None
        else False
    )
    single_row_end_anchor_overlap = len(slice_rows) <= 1 and row_slice_mentions_end_anchor
    floor_reference_top: float | None = None
    effective_start_anchor = start_anchor
    if chosen:
        chosen_bbox = tuple(float(value) for value in chosen["bbox"])
        floor_reference_top = float(chosen_bbox[1])
        left = max(0.0, chosen_bbox[0])
        right = min(page_width, chosen_bbox[2])
        top = max(0.0, chosen_bbox[1] - 8.0)
        bottom = min(page_height, chosen_bbox[3] + 8.0)
        numbered_section_span_bbox = _build_numbered_section_span_bbox(
            chosen,
            target_rows=[row for row in slice_rows if isinstance(row, list) and row],
        )
        numbered_continuation_bbox = _build_numbered_section_continuation_bbox(
            chosen,
            headers=headers,
            target_rows=[row for row in slice_rows if isinstance(row, list) and row],
        )
        row_slice_bbox = _match_row_slice_bbox(chosen, headers=headers, slice_rows=slice_rows)
        numbered_section_row_slice = numbered_section_span_bbox is not None or numbered_continuation_bbox is not None
        if _start_anchor_looks_like_late_generic_table_cell(
            start_anchor=effective_start_anchor,
            chosen_bbox=chosen_bbox,
            row_slice_bbox=row_slice_bbox,
        ):
            effective_start_anchor = None
        if row_slice_bbox is not None and not _row_slice_bbox_is_start_anchor_compatible(
            start_anchor=effective_start_anchor,
            row_slice_bbox=row_slice_bbox,
        ):
            row_slice_bbox = None
        single_row_inline_end_anchor_overlap = (
            single_row_end_anchor_overlap
            and _end_anchor_overlap_looks_like_inline_table_cell(
                end_anchor=end_anchor,
                row_slice_bbox=row_slice_bbox,
            )
        )
        single_row_boundary_end_anchor_overlap = (
            single_row_end_anchor_overlap and not single_row_inline_end_anchor_overlap
        )
        if row_slice_bbox is not None and single_row_boundary_end_anchor_overlap:
            row_slice_bbox = None
        applied_row_slice = row_slice_bbox is not None
        if row_slice_bbox is not None:
            floor_reference_top = float(row_slice_bbox[1])
            row_slice_padding = 0.0 if numbered_section_row_slice else 8.0
            top = max(0.0, row_slice_bbox[1] - row_slice_padding)
            bottom = min(page_height, row_slice_bbox[3] + row_slice_padding)
    else:
        left, top, right, bottom = 24.0, 24.0, max(24.0, page_width - 24.0), max(24.0, page_height - 24.0)
        applied_row_slice = False

    if effective_start_anchor:
        anchor_bbox = tuple(float(value) for value in effective_start_anchor["bbox"])
        anchor_text = str(effective_start_anchor.get("text") or "")
        floor_reference_top = float(anchor_bbox[1])
        anchor_top_padding = 6.0 if _looks_like_local_clause_heading(anchor_text) else 12.0
        if not applied_row_slice:
            left = max(0.0, min(left, anchor_bbox[0] - 12.0))
            right = min(page_width, max(right, anchor_bbox[2] + 12.0))
            top = max(0.0, anchor_bbox[1] - anchor_top_padding)

    if end_anchor and not single_row_end_anchor_overlap:
        anchor_bbox = tuple(float(value) for value in end_anchor["bbox"])
        end_anchor_gap = 18.0 if applied_row_slice and len(slice_rows) <= 1 else 12.0
        candidate_bottom = max(0.0, anchor_bbox[1] - end_anchor_gap)
        if applied_row_slice and row_slice_bbox is not None and len(slice_rows) > 1:
            candidate_bottom = max(candidate_bottom, min(page_height, float(row_slice_bbox[3]) + 4.0))
        if candidate_bottom > top + 20.0:
            bottom = min(bottom, min(page_height, candidate_bottom))

    if page_floor > 0.0 and (floor_reference_top is None or floor_reference_top >= page_floor - 2.0):
        top = max(top, min(page_height, page_floor + 6.0))

    if bottom <= top + 20.0:
        bottom = min(page_height, top + 20.0)
    if right <= left + 20.0:
        right = min(page_width, left + 20.0)
    bbox = (left, top, right, bottom)
    if applied_row_slice and row_slice_bbox is not None:
        bbox = _tighten_shallow_single_row_slice_horizontal_bbox(
            page,
            current_bbox=bbox,
            row_slice_bbox=row_slice_bbox,
            slice_rows=slice_rows,
        )
    return bbox


def _row_slice_bbox_is_start_anchor_compatible(
    *,
    start_anchor: dict[str, Any] | None,
    row_slice_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if start_anchor is None or row_slice_bbox is None:
        return True
    anchor_bbox = tuple(start_anchor.get("bbox") or ())
    if len(anchor_bbox) < 4 or len(row_slice_bbox) < 4:
        return True
    delta = float(anchor_bbox[1]) - float(row_slice_bbox[1])
    if delta <= 0.0:
        return True
    anchor_text = str(start_anchor.get("text") or "")
    anchor_width = float(anchor_bbox[2]) - float(anchor_bbox[0])
    anchor_height = float(anchor_bbox[3]) - float(anchor_bbox[1])
    row_slice_height = float(row_slice_bbox[3]) - float(row_slice_bbox[1])
    if (
        (
            re.fullmatch(r"\d+", _normalize_match_text(anchor_text))
            or anchor_width <= 160.0
        )
        and anchor_height <= 18.0
        and float(anchor_bbox[1]) >= float(row_slice_bbox[1]) - 4.0
        and float(anchor_bbox[3]) <= float(row_slice_bbox[3]) + 4.0
        and delta <= max(180.0, row_slice_height * 0.85)
    ):
        return True
    max_delta = 96.0 if _looks_like_local_clause_heading(anchor_text) else 140.0
    return delta <= max_delta


def _end_anchor_overlap_looks_like_inline_table_cell(
    *,
    end_anchor: dict[str, Any] | None,
    row_slice_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if end_anchor is None or row_slice_bbox is None:
        return False
    anchor_text = str(end_anchor.get("text") or "").strip()
    anchor_bbox = tuple(end_anchor.get("bbox") or ())
    if not anchor_text or len(anchor_bbox) < 4:
        return False
    if (
        _looks_like_local_clause_heading(anchor_text)
        or _looks_like_headingish_single_cell(anchor_text)
        or _looks_like_attachment_heading_anchor(anchor_text)
    ):
        return False

    anchor_width = float(anchor_bbox[2]) - float(anchor_bbox[0])
    if anchor_width > 140.0:
        return False

    # True section boundaries are typically left-aligned; compact tokens such as
    # DV/PV or supplier/sample cells often sit well inside the matched row band.
    return float(anchor_bbox[0]) >= float(row_slice_bbox[0]) + 40.0


def _start_anchor_looks_like_late_generic_table_cell(
    *,
    start_anchor: dict[str, Any] | None,
    chosen_bbox: tuple[float, float, float, float] | None,
    row_slice_bbox: tuple[float, float, float, float] | None,
) -> bool:
    if start_anchor is None or row_slice_bbox is None:
        return False
    anchor_text = str(start_anchor.get("text") or "").strip()
    anchor_bbox = tuple(start_anchor.get("bbox") or ())
    if not anchor_text or len(anchor_bbox) < 4:
        return False
    if not _is_generic_table_anchor_text(anchor_text):
        return False
    if float(anchor_bbox[1]) <= float(row_slice_bbox[3]) + 4.0:
        return False
    if chosen_bbox is not None and float(anchor_bbox[1]) > float(chosen_bbox[3]) + 2.0:
        return True
    anchor_width = float(anchor_bbox[2]) - float(anchor_bbox[0])
    return anchor_width <= 180.0 and float(anchor_bbox[1]) >= float(row_slice_bbox[3]) + 24.0


def _row_slice_candidate_mentions_anchor_text(
    candidate: dict[str, Any],
    *,
    headers: list[str],
    slice_rows: list[list[str]],
    anchor: dict[str, Any] | None,
) -> bool:
    if anchor is None:
        return False
    target_rows = [row for row in slice_rows if isinstance(row, list) and row]
    candidate_rows = candidate.get("rows_text") or []
    if not target_rows or not candidate_rows:
        return False
    matches = _resolve_row_slice_match_indices(candidate_rows, headers=headers, target_rows=target_rows)
    if not matches:
        return False
    start_index, end_index = matches
    span_text = " ".join(
        str(cell or "").strip()
        for row in candidate_rows[start_index : end_index + 1]
        if isinstance(row, list)
        for cell in row
        if str(cell or "").strip()
    )
    return _text_mentions_anchor_text(span_text, anchor)


def _should_ignore_section_end_anchor_for_clean_single_row_slice(
    *,
    page_end_anchor_from_section: bool,
    chosen: dict[str, Any] | None,
    headers: list[str],
    slice_rows: list[list[str]],
    row_slice_bbox: tuple[float, float, float, float] | None,
    end_anchor: dict[str, Any] | None,
) -> bool:
    if (
        not page_end_anchor_from_section
        or chosen is None
        or end_anchor is None
        or row_slice_bbox is None
        or len(slice_rows) != 1
    ):
        return False
    if _row_slice_mentions_anchor_text(slice_rows, end_anchor) or _row_slice_candidate_mentions_anchor_text(
        chosen,
        headers=headers,
        slice_rows=slice_rows,
        anchor=end_anchor,
    ):
        return False
    end_bbox = tuple(end_anchor.get("bbox") or ())
    if len(end_bbox) < 4:
        return False
    return float(end_bbox[1]) <= float(row_slice_bbox[3]) + 12.0


def _row_slice_mentions_anchor_text(
    slice_rows: list[list[str]],
    anchor: dict[str, Any] | None,
) -> bool:
    if anchor is None or not slice_rows:
        return False
    return _text_mentions_anchor_text(
        " ".join(
            str(cell or "").strip()
            for row in slice_rows
            if isinstance(row, list)
            for cell in row
            if str(cell or "").strip()
        ),
        anchor,
    )


def _text_mentions_anchor_text(text: str, anchor: dict[str, Any] | None) -> bool:
    if anchor is None:
        return False
    normalized_rows = _normalize_match_text(text)
    if len(normalized_rows) < 4:
        return False

    anchor_text = str(anchor.get("text") or "").strip()
    variants: list[str] = [anchor_text]
    variants.extend(text for text, _ in _build_context_anchor_fragments(anchor_text, from_end=False))
    seen: set[str] = set()
    for variant in variants:
        normalized = _normalize_match_text(variant)
        if len(normalized) < 4 or normalized in seen:
            continue
        seen.add(normalized)
        if normalized in normalized_rows:
            return True
    return False


def _resolve_row_slice_match_indices(
    candidate_rows: list[list[Any]],
    *,
    headers: list[str],
    target_rows: list[list[str]],
) -> tuple[int, int] | None:
    if not target_rows:
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
    return matches


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

    section_number = _shared_row_slice_section_number(target_rows)
    numbered_section_bbox = _build_numbered_section_span_bbox(
        candidate,
        target_rows=target_rows,
    )
    numbered_continuation_bbox = _build_numbered_section_continuation_bbox(
        candidate,
        headers=headers,
        target_rows=target_rows,
    )
    if numbered_continuation_bbox is not None:
        return numbered_continuation_bbox

    matches = _resolve_row_slice_match_indices(candidate_rows, headers=headers, target_rows=target_rows)
    section_start = _find_candidate_section_start(candidate_rows, section_number) if section_number else None
    if numbered_section_bbox is not None and (matches is None or section_start is None or matches[0] <= section_start):
        return numbered_section_bbox
    if not matches:
        return None
    start_index, end_index = matches
    header_index = start_index
    if start_index > 0:
        previous_row = _normalize_pdf_candidate_row(candidate_rows[start_index - 1] if isinstance(candidate_rows[start_index - 1], list) else [])
        current_row = _normalize_pdf_candidate_row(candidate_rows[start_index] if isinstance(candidate_rows[start_index], list) else [])
        normalized_headers = _normalize_row_cells(headers)
        if previous_row and (
            _normalize_row_cells(previous_row) == normalized_headers
            or _looks_like_pdf_header_row(previous_row, current_row)
            or _looks_like_pdf_group_header_row(previous_row, current_row)
        ):
            header_index = start_index - 1

    if _looks_like_row_slice_first_item(target_rows[0]) == "1":
        crop_start_index = header_index if header_index < start_index else 0
    else:
        crop_start_index = header_index

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


def _build_numbered_section_span_bbox(
    candidate: dict[str, Any],
    *,
    target_rows: list[list[str]],
) -> tuple[float, float, float, float] | None:
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number:
        return None
    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if len(candidate_rows) != len(candidate_boxes) or not candidate_rows:
        return None

    section_start = _find_candidate_section_start(candidate_rows, section_number)
    if section_start is None or section_start >= len(candidate_boxes):
        return None
    section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)
    continuation_rows = candidate_rows[section_start + 1 : section_stop]
    if not continuation_rows:
        return None
    if not any(not _candidate_row_section_number(row) for row in continuation_rows):
        return None

    top_box = tuple(candidate_boxes[section_start] or ())
    if len(top_box) < 4:
        return None
    if 0 <= section_stop < len(candidate_boxes):
        next_box = tuple(candidate_boxes[section_stop] or ())
        if len(next_box) >= 4:
            bottom = float(next_box[1])
        else:
            bottom = float(top_box[3])
    else:
        end_index = max(section_start, section_stop - 1)
        bottom_box = tuple(candidate_boxes[end_index] or ())
        if len(bottom_box) < 4:
            return None
        bottom = float(bottom_box[3])

    candidate_bbox = tuple(float(value) for value in candidate["bbox"])
    if bottom <= float(top_box[1]) + 8.0:
        return None
    return (
        candidate_bbox[0],
        float(top_box[1]),
        candidate_bbox[2],
        bottom,
    )


def _build_numbered_section_continuation_bbox(
    candidate: dict[str, Any],
    *,
    headers: list[str],
    target_rows: list[list[str]],
) -> tuple[float, float, float, float] | None:
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number:
        return None
    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if len(candidate_rows) != len(candidate_boxes) or not candidate_rows:
        return None
    if _find_candidate_section_start(candidate_rows, section_number) is not None:
        return None

    continuation = _extract_numbered_section_continuation_segment(
        candidate,
        current_col_count=_table_current_column_count(headers, target_rows),
    )
    if continuation is None:
        return None

    stop_index = int(continuation.get("stop_index") or 0)
    if stop_index <= 0 or stop_index >= len(candidate_rows):
        return None
    next_section_number = _candidate_row_section_number(candidate_rows[stop_index])
    if not next_section_number or next_section_number == section_number:
        return None
    if not _continuation_rows_match_target_tail(
        continuation_rows=continuation.get("rows") or [],
        target_rows=target_rows,
    ):
        return None

    bbox = tuple(float(value) for value in continuation.get("bbox") or ())
    candidate_bbox = tuple(float(value) for value in candidate.get("bbox") or ())
    if len(bbox) < 4 or len(candidate_bbox) < 4 or float(bbox[3]) <= float(bbox[1]) + 8.0:
        return None
    return (
        candidate_bbox[0],
        float(bbox[1]),
        candidate_bbox[2],
        float(bbox[3]),
    )


def _continuation_rows_match_target_tail(
    *,
    continuation_rows: list[list[Any]],
    target_rows: list[list[str]],
) -> bool:
    normalized_target_rows = [_normalize_row_cells(row) for row in target_rows if isinstance(row, list) and row]
    normalized_continuation_rows = [
        _normalize_row_cells(row)
        for row in continuation_rows
        if isinstance(row, list) and any(str(cell or "").strip() for cell in row)
    ]
    if not normalized_target_rows or not normalized_continuation_rows:
        return False

    compare_count = min(len(normalized_target_rows), len(normalized_continuation_rows))
    normalized_target_tail = normalized_target_rows[-compare_count:]
    normalized_continuation_tail = normalized_continuation_rows[-compare_count:]
    shared_prefix = _shared_cell_prefix_length(normalized_target_rows)
    shared_suffix = _shared_cell_suffix_length(normalized_target_rows)
    expected_rows = [
        _build_row_match_tokens(row, shared_prefix=shared_prefix, shared_suffix=shared_suffix)
        for row in normalized_target_tail
    ]
    if not all(expected_rows):
        return False

    matched = 0
    for actual, expected in zip(normalized_continuation_tail, expected_rows):
        if _row_match_score(actual, expected) >= _minimum_row_match_score(expected):
            matched += 1
    required_matches = compare_count if compare_count <= 2 else max(2, compare_count - 1)
    return matched >= required_matches


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
        reverse=True,
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


def _has_explicit_numbered_row_slice_label(target_rows: list[list[str]]) -> bool:
    if not target_rows:
        return False
    seen_number = ""
    matched = False
    for row in target_rows:
        if not isinstance(row, list) or not row:
            return False
        raw = re.sub(r"\s+", "", str(row[0] or "").strip())
        if not raw:
            return False
        match = re.fullmatch(r"(\d+)(?:[（(][^()（）]{1,8}[)）])?", raw)
        if not match:
            return False
        number = str(match.group(1) or "")
        if not number:
            return False
        if seen_number and number != seen_number:
            return False
        seen_number = number
        matched = True
    return matched


def _find_row_slice_section_anchor_window(
    document,
    *,
    source_key: str,
    page_index: int,
    candidate: dict[str, Any],
    headers: list[str],
    slice_rows: list[list[str]],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    target_rows = [row for row in slice_rows if isinstance(row, list) and row]
    if not target_rows:
        return None, None
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number:
        return None, None

    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if not candidate_rows or len(candidate_rows) != len(candidate_boxes):
        return None, None

    section_start = _find_candidate_section_start(candidate_rows, section_number)
    if section_start is None or section_start >= len(candidate_boxes):
        return None, None
    section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)
    current_row = candidate_rows[section_start] if isinstance(candidate_rows[section_start], list) else []
    next_row = candidate_rows[section_stop] if 0 <= section_stop < len(candidate_rows) and isinstance(candidate_rows[section_stop], list) else []
    current_bbox = tuple(candidate_boxes[section_start] or ())
    next_bbox = tuple(candidate_boxes[section_stop] or ()) if 0 <= section_stop < len(candidate_boxes) else ()

    page_lines = _load_pdf_page_line_records(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=detection_cache,
    )
    candidate_bbox = tuple(candidate.get("bbox") or ())
    start_anchor = _find_scoped_section_anchor(
        page_lines,
        candidates=_build_section_row_anchor_fragments(
            current_row,
            fallback_number=section_number,
            fallback_title=str(target_rows[0][1] if len(target_rows[0]) > 1 else ""),
        ),
        min_y=float(current_bbox[1]) - 18.0 if len(current_bbox) >= 4 else None,
        max_y=float(current_bbox[3]) + 24.0 if len(current_bbox) >= 4 else None,
        max_x=float(candidate_bbox[0]) + 260.0 if len(candidate_bbox) >= 4 else None,
        page_index=page_index,
    )
    end_anchor = _find_scoped_section_anchor(
        page_lines,
        candidates=_build_section_row_anchor_fragments(next_row),
        min_y=float(next_bbox[1]) - 20.0 if len(next_bbox) >= 4 else None,
        max_y=float(next_bbox[3]) + 28.0 if len(next_bbox) >= 4 else None,
        max_x=float(candidate_bbox[0]) + 260.0 if len(candidate_bbox) >= 4 else None,
        page_index=page_index,
    )
    return start_anchor, end_anchor


def _find_row_slice_section_box_window(
    candidate: dict[str, Any],
    *,
    slice_rows: list[list[str]],
) -> tuple[tuple[float, float, float, float] | None, tuple[float, float, float, float] | None]:
    target_rows = [row for row in slice_rows if isinstance(row, list) and row]
    if not target_rows:
        return None, None
    section_number = _shared_row_slice_section_number(target_rows)
    if not section_number:
        return None, None

    candidate_rows = candidate.get("rows_text") or []
    candidate_boxes = candidate.get("row_bboxes") or []
    if not candidate_rows or len(candidate_rows) != len(candidate_boxes):
        return None, None

    section_start = _find_candidate_section_start(candidate_rows, section_number)
    if section_start is None or section_start >= len(candidate_boxes):
        return None, None
    section_stop = _find_candidate_section_stop(candidate_rows, section_start, section_number)
    start_box = tuple(candidate_boxes[section_start] or ())
    next_box = tuple(candidate_boxes[section_stop] or ()) if 0 <= section_stop < len(candidate_boxes) else ()
    return (
        start_box if len(start_box) >= 4 else None,
        next_box if len(next_box) >= 4 else None,
    )


def _build_section_row_anchor_fragments(
    row: list[Any],
    *,
    fallback_number: str = "",
    fallback_title: str = "",
) -> list[tuple[str, int]]:
    seen: set[str] = set()
    fragments: list[tuple[str, int]] = []

    def add(value: Any, weight: int) -> None:
        normalized = _normalize_match_text(value)
        if (
            (len(normalized) < 2 and not normalized.isdigit())
            or normalized in seen
        ):
            return
        seen.add(normalized)
        fragments.append((normalized, weight))

    number = ""
    title = ""
    if isinstance(row, list):
        if row:
            number = str(row[0] or "").strip()
        if len(row) > 1:
            title = str(row[1] or "").strip()

    add(number or fallback_number, 22)
    add(title or fallback_title, 20)
    for part in re.split(r"[\r\n]+", str(title or fallback_title or "")):
        add(part, 16)
    if number or fallback_number or title or fallback_title:
        add(f"{number or fallback_number} {title or fallback_title}".strip(), 26)
    return fragments


def _find_scoped_section_anchor(
    line_records: list[dict[str, Any]],
    *,
    candidates: list[tuple[str, int]],
    min_y: float | None,
    max_y: float | None,
    max_x: float | None,
    page_index: int,
) -> dict[str, Any] | None:
    if not line_records or not candidates:
        return None

    best: dict[str, Any] | None = None
    for line in line_records:
        bbox = tuple(line.get("bbox") or ())
        if len(bbox) < 4:
            continue
        if min_y is not None and float(bbox[3]) < float(min_y):
            continue
        if max_y is not None and float(bbox[1]) > float(max_y):
            continue
        if max_x is not None and float(bbox[0]) > float(max_x):
            continue
        score = 0
        norm = str(line.get("norm") or "")
        if not norm:
            continue
        for fragment, weight in candidates:
            if not fragment:
                continue
            if fragment == norm:
                score += int(weight) + 6
            elif fragment in norm:
                score += int(weight)
        if score <= 0:
            continue
        candidate = {
            "page_index": page_index,
            "bbox": bbox,
            "score": score,
            "text": str(line.get("text") or ""),
        }
        if best is None:
            best = candidate
            continue
        if int(candidate["score"]) > int(best["score"]):
            best = candidate
            continue
        if int(candidate["score"]) == int(best["score"]):
            if float(candidate["bbox"][1]) < float(best["bbox"][1]) - 2.0:
                best = candidate
            elif abs(float(candidate["bbox"][1]) - float(best["bbox"][1])) <= 10.0 and float(candidate["bbox"][0]) < float(best["bbox"][0]) - 6.0:
                best = candidate
    return best


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
        compact_raw = re.sub(r"\s+", "", raw)
        if digits and digits == raw:
            return digits
        if digits and re.fullmatch(rf"{re.escape(digits)}(?:[（(][^()（）]{{1,8}}[)）])?", compact_raw):
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
    if len(target_rows) >= 6:
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


def _select_best_pdf_crop_candidate(
    candidates: list[dict[str, Any]],
    *,
    match_fragments: list[tuple[str, int]],
    headers: list[str],
    slice_rows: list[list[str]],
    end_anchor: dict[str, Any] | None,
) -> dict[str, Any] | None:
    chosen = _select_best_pdf_table_candidate(candidates, match_fragments)
    if chosen is None or not headers or not slice_rows or end_anchor is None:
        return chosen

    end_bbox = tuple(end_anchor.get("bbox") or ())
    if len(end_bbox) < 4:
        return chosen

    def candidate_metrics(candidate: dict[str, Any]) -> tuple[int, float, float, tuple[float, float, float, float] | None]:
        row_slice_bbox = _match_row_slice_bbox(candidate, headers=headers, slice_rows=slice_rows)
        score = _score_pdf_table_candidate(str(candidate.get("text") or ""), match_fragments)
        area = float(candidate.get("area") or 0.0)
        if row_slice_bbox is None:
            return score, float("inf"), area, None
        cutoff = float(end_bbox[1]) - 10.0
        overflow = max(0.0, float(row_slice_bbox[3]) - cutoff)
        return score, overflow, area, row_slice_bbox

    chosen_score, chosen_overflow, _, _ = candidate_metrics(chosen)
    if chosen_overflow <= 12.0:
        return chosen

    best_score_floor = max(1, chosen_score - 28)
    alternatives = [
        (candidate, *candidate_metrics(candidate))
        for candidate in candidates
    ]
    viable = [
        item
        for item in alternatives
        if item[4] is not None and item[1] >= best_score_floor
    ]
    if not viable:
        viable = [item for item in alternatives if item[4] is not None]
    if not viable:
        return chosen

    best = min(
        viable,
        key=lambda item: (
            item[2] > 6.0,
            item[2],
            -item[1],
            item[3],
        ),
    )
    if best[2] + 4.0 < chosen_overflow or (best[2] <= 6.0 and chosen_overflow > 20.0):
        return best[0]
    return chosen


def _score_pdf_table_candidate(candidate_text: str, match_fragments: list[tuple[str, int]]) -> int:
    normalized_candidate = _normalize_match_text(candidate_text)
    if not normalized_candidate:
        return 0
    score = 0
    for fragment, weight in match_fragments:
        if fragment and fragment in normalized_candidate:
            score += weight
    return score


def _render_pdf_crop_plan(
    document,
    plan: list[tuple[int, tuple[float, float, float, float] | None]],
    *,
    max_width_px: int,
    max_scale: float = 3.0,
) -> list[PILImage.Image]:
    if not plan:
        return []
    widest_points = max(
        float(document.load_page(page_index).rect.width) if bbox is None else max(1.0, bbox[2] - bbox[0])
        for page_index, bbox in plan
    )
    scale = max(1.0, min(float(max_scale), max_width_px / max(widest_points, 1.0)))

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


def _compose_vertical_images(
    images: list[PILImage.Image],
    *,
    max_width_px: int,
    max_height_px: int | None = None,
) -> bytes:
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

    scale_factor = min(
        1.0,
        max_width_px / max(1, canvas.width),
    )
    if max_height_px and max_height_px > 0:
        scale_factor = min(scale_factor, max_height_px / max(1, canvas.height))
    if scale_factor < 1.0:
        resampling = getattr(getattr(PILImage, "Resampling", PILImage), "LANCZOS")
        scaled_width = max(1, int(canvas.width * scale_factor))
        scaled_height = max(1, int(canvas.height * scale_factor))
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
