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
    reusable_header_contexts: list[dict[str, Any]] = []
    for index, row in enumerate(entries):
        previous_text = _entry_source_text(entries[index - 1]) if index > 0 else ""
        next_text = _entry_source_text(entries[index + 1]) if index + 1 < len(entries) else ""
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
    copied = dict(row)
    if str(copied.get("entry_kind") or "").strip().upper() == "HEADING":
        copied["table_refs"] = []
        copied["content_blocks"] = _paragraph_only_blocks(_split_text_blocks(_entry_source_text(copied)))
        copied["has_table_asset"] = False
        copied["table_render_mode"] = ""
        return copied, []
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
        title = _table_title_for_row(copied)
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
                ),
            )
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
                    "source_page": _coerce_int(copied.get("source_page")),
                    "source_page_end": _coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                    "structure_confidence": 0.9,
                    "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
                }
            )
            asset = {
                "table_id": table_id,
                "document_id": document_id,
                "section_instance_key": str(copied.get("section_instance_key") or copied.get("section_key") or ""),
                "page_span": {
                    "start": _coerce_int(copied.get("source_page")),
                    "end": _coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
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
            copied["table_refs"] = [ref]
            copied["content_blocks"] = content_blocks
            copied["has_table_asset"] = True
            copied["table_render_mode"] = "structured"
            return copied, [asset]
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
        copied["table_refs"] = [visual_ref] if visual_ref else []
        paragraph_blocks = _paragraph_only_blocks(blocks)
        if visual_ref:
            paragraph_blocks.append({"kind": "table", "table_id": str(visual_ref.get("table_id") or "")})
        copied["content_blocks"] = paragraph_blocks
        copied["has_table_asset"] = bool(visual_ref)
        copied["table_render_mode"] = str(visual_ref.get("slice_render_mode") or "") if visual_ref else ""
        return copied, [visual_asset] if visual_asset else []

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
        after_context_text = next_entry_text if trailing_context_text else (after_block_text or next_entry_text)
        table_id = _build_table_id(copied, table_text, block_index)
        notes = _collect_block_notes(blocks, block_index)
        if trailing_context_text:
            notes = [note for note in notes if str(note or "").strip() != trailing_context_text]
        block_header = _resolve_table_block_header(
            lines=block.get("lines") or [],
            explicit_header=block.get("header_cells") or [],
            inherited_header=inherited_header or [],
            notes=notes,
        )
        headers, body_rows, render_mode = _extract_structured_table(
            block.get("lines") or [],
            explicit_header=block_header,
        )
        recovered = None
        if _looks_like_source_table_candidate_text(table_text, title=_table_title_for_row(copied)):
            recovered = _recover_table_from_pdf_source(
                source_file_path=source_file_path,
                source_page=_coerce_int(copied.get("source_page")),
                source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                table_text=table_text,
                table_title=_table_title_for_row(copied),
                current_headers=headers,
                current_rows=body_rows,
                notes=notes,
                before_context_text=before_context_text,
                after_context_text=after_context_text,
                source_table_detection_cache=source_table_detection_cache,
                source_line_detection_cache=source_line_detection_cache,
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
        snapshot_path = _write_snapshot(
            table_id=table_id,
            snapshot_root=snapshot_root,
            headers=headers,
            rows=body_rows,
            render_mode=render_mode,
            text=table_text,
        )
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
            prefer_clause_window=_should_prefer_source_clause_window(
                table_title=table_title,
                render_mode=render_mode,
                headers=headers,
                rows=body_rows,
                source_page=_coerce_int(copied.get("source_page")),
                source_page_end=_coerce_int(copied.get("source_page_end")) or _coerce_int(copied.get("source_page")),
                trailing_context_text=trailing_context_text,
            ),
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
        rows=[],
        notes=[],
        before_context_text=previous_entry_text,
        after_context_text=next_entry_text,
        source_snapshot_cache=source_snapshot_cache,
        source_table_detection_cache=source_table_detection_cache,
        source_line_detection_cache=source_line_detection_cache,
        source_page_floor_offsets={},
        prefer_clause_window=True,
    )
    if not source_snapshot_path:
        return None, None
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
            "start": _coerce_int(row.get("source_page")),
            "end": _coerce_int(row.get("source_page_end")) or _coerce_int(row.get("source_page")),
        },
        "table_title": first_line,
        "headers": [],
        "rows": [],
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
            "inherited_headers": [],
            "slice_rows": [],
            "inherited_group_title": "",
            "inherited_notes": [],
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path),
            "slice_render_key": table_id,
            "slice_render_mode": "snapshot_only",
            "source_page": _coerce_int(row.get("source_page")),
            "source_page_end": _coerce_int(row.get("source_page_end")) or _coerce_int(row.get("source_page")),
            "structure_confidence": 0.45,
            "source_image_block_count": image_block_count,
        }
    )
    return ref, asset


def _should_generate_visual_snapshot(text: str, *, title: str = "") -> bool:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 1:
        return False
    first_line = lines[0]
    if _looks_like_local_clause_title(first_line):
        return _has_strong_visual_snapshot_cue(text, title=first_line)

    title_value = str(title or "").strip()
    if _looks_like_attachment_table_title(title_value):
        compact_items = re.findall(r"[A-Za-z\u4e00-\u9fff0-9][A-Za-z\u4e00-\u9fff0-9\-_/（）()]{2,32}", text)
        if len(compact_items) >= 8:
            return True
        if len(text) >= 120:
            return True
    return False


def _should_keep_visual_snapshot(text: str, *, title: str, image_block_count: int) -> bool:
    if image_block_count > 0:
        return True
    return _has_strong_visual_snapshot_cue(text, title=title)


def _has_strong_visual_snapshot_cue(text: str, *, title: str = "") -> bool:
    value = f"{title}\n{text}".strip()
    if re.search(r"(?:环境适应性|环境要求)", value) and (
        re.search(r"IP\d{2}", value, re.I)
        or value.count("℃") >= 2
        or any(keyword in value for keyword in ("涉水", "冰冻", "温度梯度", "温度循环", "温度冲击"))
    ):
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
            r"(?:附件|附录|appendix|annex|算法|保密|rasic|definition|定义|目录|清单|评估表|试验大纲|规范|要求)",
            value,
            re.I,
        )
    )


def _looks_like_local_clause_title(text: str) -> bool:
    return bool(re.match(r"^\s*\d+(?:\.\d+)*\s*[）)]\s*.+$", str(text or "").strip()))


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


def render_table_png(headers: list[str], rows: list[list[str]], *, max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX) -> bytes:
    image_bytes, _, _ = _render_table_image([headers, *rows], max_width_px=max_width_px)
    return image_bytes.getvalue()


def render_text_snapshot_png(text: str, *, max_width_px: int = _SNAPSHOT_MAX_WIDTH_PX) -> bytes:
    image_bytes, _, _ = _render_text_snapshot(text, max_width_px=max_width_px)
    return image_bytes.getvalue()


def _entry_source_text(row: dict[str, Any]) -> str:
    return str(row.get("raw_text") or row.get("display_text") or row.get("requirement_text") or "").strip()


def _refresh_enriched_entry_text_fields(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    materialized = _materialize_requirement_text(copied)
    if not materialized:
        return copied
    copied["display_text"] = materialized
    copied["raw_text"] = materialized
    copied["requirement_text"] = materialized
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
    for block in blocks:
        kind = str(block.get("kind") or "").strip().lower()
        if kind == "paragraph":
            text = _clean_paragraph_block_text(str(block.get("text") or "").strip())
            if text:
                materialized_parts.append(text)
            continue
        if kind != "table":
            continue
        table_id = str(block.get("table_id") or "").strip()
        table_text = _materialize_table_block_text(table_ref_map.get(table_id))
        if table_text:
            materialized_parts.append(table_text)
    if not materialized_parts:
        return _clean_paragraph_block_text(_entry_source_text(row))
    return "\n".join(materialized_parts).strip()


def _materialize_table_block_text(ref: dict[str, Any] | None) -> str:
    if not isinstance(ref, dict):
        return ""
    headers = _sanitize_table_text_cells(ref.get("inherited_headers") or ref.get("headers") or [])
    rows = _sanitize_table_text_rows(
        ref.get("slice_rows") or ref.get("rows") or [],
        expected_columns=len(headers),
    )
    rows = _drop_materialized_header_echo_rows(rows, headers)
    if _should_omit_materialized_table_text(headers, rows):
        return ""

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


def _should_omit_materialized_table_text(headers: list[str], rows: list[list[str]]) -> bool:
    if not headers and not rows:
        return True
    return not rows and not any(headers)


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
    return blocks


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
) -> list[str]:
    if explicit_header:
        return [str(cell or "").strip() for cell in explicit_header if str(cell or "").strip()]
    if inherited_header and _looks_like_sparse_matrix_requirement_text("\n".join(lines)):
        return [str(cell or "").strip() for cell in inherited_header if str(cell or "").strip()]
    inferred = _infer_explicit_header_from_block_notes(lines, notes)
    if inferred:
        return inferred
    return []


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
    if not _looks_like_markdown_table_row(value):
        return [value]

    cells = _parse_markdown_cells(value)
    if not cells:
        return [value]

    if _looks_like_placeholder_only_row(cells):
        pieces = _split_placeholder_title_row(cells[0])
        return pieces or []

    collapsed = _collapse_repeated_adjacent_cells(cells)
    if len(collapsed) == 1:
        single = collapsed[0]
        if _looks_like_local_clause_heading(single) or _looks_like_headingish_single_cell(single):
            return [single]
        return ["|" + "|".join(collapsed) + "|"]

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
    match = re.match(r"^\s*(\d+(?:\.\d+)*)\s*[）)]\s*(.+)$", value)
    if not match:
        return False
    title = str(match.group(2) or "").strip()
    if not title or len(title) > 140:
        return False
    if "|" in title:
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
    if re.match(r"^\d+(?:\.\d+)*\s*[）)]", value):
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
    label = re.split(r"[|（(：:—\-]", label, maxsplit=1)[0].strip()
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
                "source-crop-v23",
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
        return cached, {}

    snapshot_root.mkdir(parents=True, exist_ok=True)
    source_seed = "|".join([cache_key[0], str(source_page), str(page_end), snapshot_signature])
    target = snapshot_root / f"src_{hashlib.sha1(source_seed.encode('utf-8')).hexdigest()[:16]}.png"
    if _snapshot_file_is_usable(target):
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = target
        return target, {}
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
        )
        _write_png_atomically(target, rendered)
    except Exception:
        if source_snapshot_cache is not None:
            source_snapshot_cache[cache_key] = None
        return None, {}

    if source_snapshot_cache is not None:
        source_snapshot_cache[cache_key] = target
    return target, render_meta


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
        match_fragments = _build_table_match_fragments(
            table_text=table_text,
            table_title=table_title,
            headers=current_headers,
            rows=current_rows,
            notes=notes,
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
        return best_headers, best_rows, best_notes or []


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
        normalized.append(value)
    return normalized


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


def _structured_table_looks_low_quality(headers: list[str], rows: list[list[str]]) -> bool:
    if not headers or not rows:
        return True
    if any(not cell or re.fullmatch(r"(?:col\d+|列\d+)", str(cell or "").strip(), re.I) for cell in headers):
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
    return _drop_repeated_trailing_merged_line(value)


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
    trailing_context_text: str = "",
) -> bool:
    normalized_mode = str(render_mode or "").strip().lower()
    title = str(table_title or "").strip()
    heading_like = (
        _looks_like_local_clause_title(title)
        or _looks_like_headingish_single_cell(title)
        or _looks_like_plain_heading_label(title)
        or _looks_like_attachment_table_title(title)
    )
    if trailing_context_text:
        return True
    if normalized_mode != "structured":
        return heading_like

    cross_page = source_page > 0 and source_page_end > source_page
    if cross_page and _looks_like_local_clause_title(title):
        return True

    col_count = max(len(headers), max((len(row) for row in rows), default=0))
    if heading_like and len(rows) <= 1 and 0 < col_count <= 4:
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

    if source_snapshot_path and _looks_like_symbolic_or_placeholder_source_first_table(headers, rows):
        reasons.append("切片可读性差")
        score = max(score, 72)

    if source_snapshot_path and _looks_like_axis_symbol_source_first_table(headers, rows):
        reasons.append("切片可读性差")
        score = max(score, 72)

    if source_snapshot_path and _looks_like_compact_threshold_source_first_table(headers, rows, notes):
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
            slice_rows=[] if prefer_clause_window else rows,
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            detection_cache=source_table_detection_cache,
            source_page_floor_offsets=source_page_floor_offsets,
            prefer_anchor_window=prefer_clause_window,
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
    prefer_anchor_window: bool = False,
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    effective_start_page = int(start_anchor.get("page_index")) if start_anchor else page_indices[0]
    effective_end_anchor = end_anchor
    if prefer_anchor_window and start_anchor is not None:
        next_clause_anchor = _find_next_local_clause_anchor(
            document,
            source_key=source_key,
            page_indices=page_indices,
            start_anchor=start_anchor,
            detection_cache=None,
        )
        if next_clause_anchor is not None:
            if effective_end_anchor is None:
                effective_end_anchor = next_clause_anchor
            elif (
                int(effective_end_anchor.get("page_index", -1)) == int(start_anchor.get("page_index", -1))
                and int(next_clause_anchor.get("page_index", -1)) > int(start_anchor.get("page_index", -1))
            ):
                effective_end_anchor = next_clause_anchor
            elif (
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
        chosen = _select_best_pdf_table_candidate(candidates, match_fragments)
        chosen_score = _score_pdf_table_candidate(str(chosen.get("text") or ""), match_fragments) if chosen else 0
        row_slice_preview = (
            _match_row_slice_bbox(chosen, headers=headers, slice_rows=slice_rows)
            if chosen is not None and headers and slice_rows
            else None
        )
        if (
            chosen is not None
            and not prefer_anchor_window
            and effective_end_anchor is not None
            and page_index == effective_end_page
            and page_index != effective_start_page
            and chosen_score < 3
            and row_slice_preview is None
            and (headers or slice_rows)
        ):
            continue
        bbox = _compose_page_crop_bbox(
            page,
            chosen=None if prefer_anchor_window else chosen,
            start_anchor=start_anchor if page_index == effective_start_page else None,
            end_anchor=effective_end_anchor if page_index == effective_end_page else None,
            headers=headers,
            slice_rows=slice_rows,
            page_floor=float((source_page_floor_offsets or {}).get(page_index + 1, 0.0)),
        )
        if prefer_anchor_window and bbox is not None:
            window_top = float(start_anchor["bbox"][1]) if page_index == effective_start_page and start_anchor else 0.0
            window_bottom = (
                float(effective_end_anchor["bbox"][1])
                if page_index == effective_end_page and effective_end_anchor
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
                and effective_end_anchor is not None
                and not content_bboxes
            ):
                continue
            bbox = _tighten_anchor_window_crop_bbox(
                page.rect,
                current_bbox=bbox,
                content_bboxes=content_bboxes,
                start_anchor=start_anchor if page_index == effective_start_page else None,
                end_anchor=effective_end_anchor if page_index == effective_end_page else None,
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
        variants = [line]
        local_clause_variant = _extract_local_clause_anchor_prefix(line)
        if local_clause_variant:
            variants.append(local_clause_variant)
        variants.append(_strip_anchor_line_prefix(line))
        label_variant = _extract_anchor_label_prefix(line)
        if label_variant:
            variants.append(label_variant)
        mixed_variants = _extract_mixed_heading_anchor_parts(line)
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
    if promote_after_band_start and end_anchor is not None:
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


def _find_next_local_clause_anchor(
    document,
    *,
    source_key: str,
    page_indices: list[int],
    start_anchor: dict[str, Any],
    detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None,
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
            if not _looks_like_local_clause_heading(text):
                continue
            return {
                "page_index": page_index,
                "bbox": bbox,
                "score": 1,
                "text": text,
            }
    return None


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

    lines.sort(key=lambda item: (float(item["bbox"][1]), float(item["bbox"][0])))
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


def _collect_anchor_window_content_bboxes(
    page,
    *,
    window_top: float,
    window_bottom: float,
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
                content_bboxes.append(line_bbox)
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
            content_bboxes.append(image_bbox)
    return content_bboxes


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

    if start_anchor:
        anchor_bbox = tuple(float(value) for value in start_anchor["bbox"])
        anchor_text = str(start_anchor.get("text") or "")
        anchor_top_padding = 6.0 if _looks_like_local_clause_heading(anchor_text) else 12.0
        if not applied_row_slice:
            left = max(0.0, min(left, anchor_bbox[0] - 12.0))
            right = min(page_width, max(right, anchor_bbox[2] + 12.0))
            top = max(0.0, anchor_bbox[1] - anchor_top_padding)

    if end_anchor:
        anchor_bbox = tuple(float(value) for value in end_anchor["bbox"])
        candidate_bottom = max(0.0, anchor_bbox[1] - 12.0)
        if applied_row_slice and row_slice_bbox is not None:
            candidate_bottom = max(candidate_bottom, min(page_height, float(row_slice_bbox[3]) + 4.0))
        if candidate_bottom > top + 20.0:
            bottom = min(bottom, min(page_height, candidate_bottom))

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
        crop_start_index = header_index
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
