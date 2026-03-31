from __future__ import annotations

import re
from pathlib import Path
from statistics import mean
from typing import Any

from app.services.complex_table_mode import (
    detect_complex_table_zones,
    rewrite_preview_rows_for_complex_tables,
    rewrite_preview_sections_for_complex_tables,
)
from app.services.extractor import (
    _split_embedded_local_clause_line,
    analyze_section_consistency,
    extract_atomic_requirements,
    extract_section_outline,
)
from app.services.parser import parse_document_with_diagnostics
from app.services.table_assets import enrich_entries_with_table_assets


SECTION_ANCHOR_RE = re.compile(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})(?:\s+([^|]+))?", re.I)
CLAUSE_ANCHOR_RE = re.compile(r"Clause\s+([0-9]+(?:\.[0-9]+){0,8})\s*[）)]?\s*([^|]+)?", re.I)
TITLE_ANCHOR_RE = re.compile(r"(?:Heading|Title)\s+([^|]+)", re.I)
APPENDIX_SCOPE_RE = re.compile(r"Appendix\s+([A-Z][A-Z0-9_-]*)\b", re.I)
APPENDIX_MARKER_LINE_RE = re.compile(
    r"^\s*(?:附件|附录|appendix|annex|attachment)\s*([A-Za-zＡ-Ｚａ-ｚ])\b.*$",
    re.I,
)
LOCAL_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+)+)\s*[）)]?\s*(.+)$")
TEST_OUTLINE_TITLE_RE = re.compile(
    r"(?:试验大纲|匹配评估表|测试分组)",
    re.I,
)
TEST_OUTLINE_EXCLUDE_RE = re.compile(
    r"(?:测试项目\s*/\s*test item|col\d+)$",
    re.I,
)
ELECTRIC_TEST_OUTLINE_HEADER_RE = re.compile(
    r"(?:序号\s*/?.*项目\s*/\s*item.*测试顺序.*参考标准.*要求\s*/\s*requirements)",
    re.I,
)
TEST_OUTLINE_MARKER_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"主动前减振器试验大纲", re.I), "T", "主动前减振器试验大纲"),
    (re.compile(r"主动后减振器试验大纲", re.I), "U", "主动后减振器试验大纲"),
    (re.compile(r"蓄能器试验大纲", re.I), "V", "蓄能器试验大纲"),
    (re.compile(r"整车用电器与线束匹配评估表", re.I), "W", "整车用电器与线束匹配评估表"),
    (re.compile(r"测试分组\s*/\s*test groups", re.I), "X", "测试分组 / Test Groups"),
    (re.compile(r"用电器试验大纲", re.I), "Y", "用电器试验大纲"),
]


def build_extraction_preview(
    file_path: str,
    *,
    document_id: str = "",
    snapshot_root: Path | None = None,
) -> dict[str, Any]:
    parsed = parse_document_with_diagnostics(file_path)
    segments = parsed.get("segments", [])
    requirements = extract_atomic_requirements(segments)
    raw_section_outline = extract_section_outline(segments)
    section_consistency = analyze_section_consistency(raw_section_outline)
    appendix_markers = _collect_appendix_markers(segments)
    test_outline_markers = _collect_test_outline_markers(segments)
    # Strict original-order mode: do not inject synthetic marker rows into section flow.
    section_outline = [
        {
            **dict(row),
            "is_appendix_marker": False,
        }
        for row in raw_section_outline
    ]

    req_rows = []
    text_lengths: list[int] = []
    req_pages: set[int] = set()
    max_page = 0

    for item in requirements:
        source_page = _to_int(item.get("source_page"))
        source_page_end = _to_int(item.get("source_page_end")) or source_page
        appendix_scope = _extract_appendix_scope(item.get("source_anchor", ""))
        section_code = _extract_section_code(item.get("source_anchor", ""))
        section_title_hint = _extract_section_title(item.get("source_anchor", ""))
        text = str(item.get("requirement_text", "")).strip()
        split_rows = _split_requirement_preview_rows(
            item=item,
            source_page=source_page,
            source_page_end=source_page_end,
            appendix_scope=appendix_scope,
            section_code=section_code,
            section_title_hint=section_title_hint,
            text=text,
            req_order_start=len(req_rows),
        )
        for row in split_rows:
            req_pages.update(page for page in range(row["source_page"], row["source_page_end"] + 1) if page > 0)
            if row["source_page_end"] > max_page:
                max_page = row["source_page_end"]
            text_lengths.append(int(row.get("text_length") or 0))
            req_rows.append(row)

    # Some appendix marker pages (e.g. W/X) are full-table pages without explicit
    # numeric subsection codes, so extractor may not emit atomic requirements.
    # Keep completeness by materializing one synthetic requirement per missing scope.
    req_rows.extend(
        _build_missing_test_outline_scope_requirements(
            segments=segments,
            markers=test_outline_markers,
            existing_requirements=req_rows,
            start_req_order=len(req_rows),
        )
    )

    _assign_section_instance_keys(req_rows, section_outline)

    section_counter: dict[str, dict[str, Any]] = {}
    for row in req_rows:
        section_instance_key = str(row.get("section_instance_key") or "").strip()
        section_key = str(row.get("section_key") or "").strip()
        if not section_instance_key:
            section_instance_key = section_key
            row["section_instance_key"] = section_instance_key
        if not section_instance_key:
            continue
        state = section_counter.setdefault(
            section_instance_key,
            {
                "appendix_scope": row.get("appendix_scope", ""),
                "section_code": row.get("section_code", ""),
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "requirement_count": 0,
                "source_page_start": row.get("source_page") if _to_int(row.get("source_page")) > 0 else None,
                "source_page_end": row.get("source_page_end") if _to_int(row.get("source_page_end")) > 0 else None,
                "total_chars": 0,
                "section_title": row.get("section_title", ""),
                "first_req_order": _to_int(row.get("req_order")),
            },
        )
        state["requirement_count"] += 1
        state["total_chars"] += _to_int(row.get("text_length"))
        if not state.get("section_title") and row.get("section_title"):
            state["section_title"] = row.get("section_title")
        source_page = _to_int(row.get("source_page"))
        source_page_end = _to_int(row.get("source_page_end")) or source_page
        if source_page > 0:
            current_start = state.get("source_page_start")
            state["source_page_start"] = source_page if current_start in (None, 0) else min(current_start, source_page)
        if source_page_end > 0:
            current_end = state.get("source_page_end")
            state["source_page_end"] = source_page_end if current_end in (None, 0) else max(current_end, source_page_end)
        req_order = _to_int(row.get("req_order"))
        if req_order >= 0:
            current_order = state.get("first_req_order")
            state["first_req_order"] = req_order if current_order is None else min(int(current_order), req_order)

    outline_index: dict[str, dict[str, Any]] = {}
    outline_instance_index: set[str] = set()
    key_to_instances = _collect_section_instance_groups(req_rows, section_outline)
    for node in section_outline:
        appendix_scope = str(node.get("appendix_scope") or "").strip().upper()
        code = str(node.get("section_code") or "")
        section_key = _join_section_key(appendix_scope, code)
        if not section_key:
            section_key = _join_title_section_key(appendix_scope, str(node.get("section_title") or ""))
        if section_key:
            outline_index[section_key] = node

    sections: list[dict[str, Any]] = []
    for outline_order, node in enumerate(section_outline):
        appendix_scope = str(node.get("appendix_scope") or "").strip().upper()
        code = str(node.get("section_code") or "")
        section_key = _join_section_key(appendix_scope, code)
        if not section_key:
            section_key = _join_title_section_key(appendix_scope, str(node.get("section_title") or ""))
        section_instance_key = _resolve_outline_section_instance_key(node, section_key, key_to_instances)
        req_state = section_counter.get(section_instance_key, {})
        outline_instance_index.add(section_instance_key)
        sections.append(
            {
                "appendix_scope": appendix_scope,
                "section_code": code,
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "section_title": node.get("section_title", "") or req_state.get("section_title", ""),
                "section_level": node.get("section_level", 0),
                "parent_section_code": node.get("parent_section_code", ""),
                "has_children": bool(node.get("has_children", False)),
                "is_leaf": bool(node.get("is_leaf", True)),
                "source_page_start": node.get("source_page_start"),
                "source_page_end": node.get("source_page_end"),
                "source_line_index_start": node.get("source_line_index_start"),
                "source_line_index_end": node.get("source_line_index_end"),
                "line_count": node.get("line_count", 0),
                "section_char_count": node.get("char_count", 0),
                "requirement_like": bool(node.get("requirement_like", False)),
                "is_appendix_marker": bool(node.get("is_appendix_marker", False)),
                "requirement_count": req_state.get("requirement_count", 0),
                "total_chars": req_state.get("total_chars", 0),
                "text_preview": node.get("text_preview", ""),
                "first_req_order": req_state.get("first_req_order"),
                "outline_order": outline_order,
            }
        )

    # Keep visibility for any requirement section not captured in outline.
    for section_instance_key, req_state in section_counter.items():
        if section_instance_key in outline_instance_index:
            continue
        section_key = str(req_state.get("section_key") or "")
        appendix_scope, code = _split_section_key(section_key)
        sections.append(
            {
                "appendix_scope": appendix_scope,
                "section_code": code,
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "section_title": req_state.get("section_title", ""),
                "section_level": code.count(".") + 1 if code else 0,
                "parent_section_code": _parent_section_code(code),
                "has_children": False,
                "is_leaf": True,
                "source_page_start": req_state.get("source_page_start"),
                "source_page_end": req_state.get("source_page_end"),
                "source_line_index_start": None,
                "source_line_index_end": None,
                "line_count": 0,
                "section_char_count": 0,
                "requirement_like": True,
                "is_appendix_marker": False,
                "requirement_count": req_state.get("requirement_count", 0),
                "total_chars": req_state.get("total_chars", 0),
                "text_preview": "",
                "first_req_order": req_state.get("first_req_order"),
                "outline_order": None,
            }
        )

    sections, section_tree_stats = _build_canonical_section_rows(sections)
    _attach_requirement_section_context(req_rows, sections)
    complex_table_zones = detect_complex_table_zones(
        segments=segments,
        source_file_path=file_path,
    )

    complete_entries_raw = _build_complete_requirement_entries(
        requirements=req_rows,
        sections=sections,
    )
    complete_entries, dedupe_stats = _build_section_unified_entries(complete_entries_raw)
    sections = rewrite_preview_sections_for_complex_tables(
        sections,
        zones=complex_table_zones,
    )
    complete_entries_raw = rewrite_preview_rows_for_complex_tables(
        complete_entries_raw,
        zones=complex_table_zones,
    )
    complete_entries = rewrite_preview_rows_for_complex_tables(
        complete_entries,
        zones=complex_table_zones,
    )
    sections = rewrite_preview_sections_for_complex_tables(
        sections,
        zones=complex_table_zones,
    )
    section_tree = [dict(row) for row in sections]
    table_snapshot_root = snapshot_root or (Path(file_path).resolve().parent / "table_snapshots")
    table_document_id = document_id or Path(file_path).stem
    complete_entries_raw, raw_table_assets = enrich_entries_with_table_assets(
        complete_entries_raw,
        document_id=table_document_id,
        snapshot_root=table_snapshot_root,
        source_file_path=file_path,
    )
    complete_entries, deduped_table_assets = enrich_entries_with_table_assets(
        complete_entries,
        document_id=table_document_id,
        snapshot_root=table_snapshot_root,
        source_file_path=file_path,
    )
    table_assets = _dedupe_table_assets(deduped_table_assets or raw_table_assets)

    diagnostics = parsed.get("diagnostics", {}) or {}
    page_details = diagnostics.get("page_details", []) if isinstance(diagnostics, dict) else []
    quality_scores = [
        float(detail.get("quality_score", 0.0)) for detail in page_details if isinstance(detail, dict)
    ]

    summary = {
        "requirements_count": len(req_rows),
        "complete_entries_count": len(complete_entries),
        "complete_entries_raw_count": len(complete_entries_raw),
        "heading_entries_count": sum(1 for row in complete_entries if str(row.get("entry_kind")) == "HEADING"),
        "hybrid_entries_count": sum(1 for row in complete_entries if str(row.get("entry_kind")) == "HYBRID"),
        "sections_count": sum(1 for row in sections if not bool(row.get("is_appendix_marker", False))),
        "section_outline_count": len(raw_section_outline),
        "section_tree_count": len(section_tree),
        "appendix_marker_count": len(appendix_markers),
        "test_outline_marker_count": len(test_outline_markers),
        "section_consistency_issues_total": section_consistency.get("total_issues", 0),
        "section_consistency_duplicate_code_count": section_consistency.get("duplicate_code_count", 0),
        "section_consistency_missing_sibling_group_count": section_consistency.get("missing_sibling_group_count", 0),
        "leaf_sections_count": sum(1 for row in sections if row.get("is_leaf")),
        "requirement_like_sections_count": sum(1 for row in sections if row.get("requirement_like")),
        "avg_requirement_length": round(mean(text_lengths), 2) if text_lengths else 0.0,
        "pages_with_requirements": len(req_pages),
        "max_requirement_page": max_page,
        "avg_selected_page_quality": round(mean(quality_scores), 4) if quality_scores else 0.0,
        "selected_engine_pages": diagnostics.get("selected_engine_pages", {}),
        "table_pages": diagnostics.get("table_pages", 0),
        "image_or_graphic_pages": diagnostics.get("image_or_graphic_pages", 0),
        "non_empty_pages": diagnostics.get("non_empty_pages", 0),
        "table_block_injected_count": diagnostics.get("table_block_injected_total", 0),
        "dropped_mega_table_rows": diagnostics.get("dropped_mega_table_rows_total", 0),
        "avg_table_block_quality": diagnostics.get("avg_table_block_quality", 0.0),
        "removed_noise_total": diagnostics.get("removed_noise_total", 0),
        "removed_repeat_total": diagnostics.get("removed_repeat_total", 0),
        "removed_watermark_total": diagnostics.get("removed_watermark_total", 0),
        "merge_mode": diagnostics.get("merge_mode", ""),
        "line_injection_count": diagnostics.get("line_injection_count", 0),
        "line_reorder_count": diagnostics.get("line_reorder_count", 0),
        "order_integrity_ok": bool(diagnostics.get("order_integrity_ok", True)),
        "low_confidence_page_count": len(diagnostics.get("low_confidence_pages", []) or []),
        "dedup_exact_count": int(dedupe_stats.get("dedup_exact_count", 0)),
        "dedup_prefix_count": int(dedupe_stats.get("dedup_prefix_count", 0)),
        "dedup_requirement_prefix_count": int(dedupe_stats.get("dedup_requirement_prefix_count", 0)),
        "dedupe_version": "heading_requirement_v1",
        "complex_table_zone_count": len(complex_table_zones),
        "complex_table_group_count": sum(len(zone.get("groups") or []) for zone in complex_table_zones),
        "section_order_model": "canonical_tree_v1",
        "zero_direct_requirement_parent_count": int(section_tree_stats.get("zero_direct_requirement_parent_count", 0)),
        "synthetic_section_count": int(section_tree_stats.get("synthetic_section_count", 0)),
        "orphan_section_count": int(section_tree_stats.get("orphan_section_count", 0)),
    }

    return {
        "file_type": parsed.get("file_type", ""),
        "summary": summary,
        "requirements": req_rows,
        "complete_requirement_entries": complete_entries,
        "complete_requirement_entries_raw": complete_entries_raw,
        "sections": sections,
        "section_tree": section_tree,
        "section_outline": section_outline,
        "appendix_markers": appendix_markers,
        "test_outline_markers": test_outline_markers,
        "section_consistency": section_consistency,
        "parser_diagnostics": diagnostics,
        "table_assets": table_assets,
        "complex_table_zones": complex_table_zones,
    }


def _split_requirement_preview_rows(
    *,
    item: dict[str, Any],
    source_page: int,
    source_page_end: int,
    appendix_scope: str,
    section_code: str,
    section_title_hint: str,
    text: str,
    req_order_start: int,
) -> list[dict[str, Any]]:
    chunks = _split_embedded_local_clause_line(text)
    if len(chunks) <= 1:
        chunks = [text]

    base_req_code = str(item.get("req_code") or "").strip()
    if not base_req_code:
        base_req_code = f"REQ-{req_order_start + 1:04d}"

    rows: list[dict[str, Any]] = []
    for idx, chunk in enumerate(chunks):
        clause_code, clause_title = _extract_local_clause_code_and_title(chunk)
        promote_clause_section = _should_promote_local_clause_to_section(
            parent_section_code=section_code,
            clause_code=clause_code,
        )
        chunk_section_code = clause_code if promote_clause_section else section_code
        chunk_section_title = (clause_title if promote_clause_section else "") or section_title_hint
        chunk_section_key = _join_section_key(appendix_scope, chunk_section_code)
        if not chunk_section_key and chunk_section_title:
            chunk_section_key = _join_title_section_key(appendix_scope, chunk_section_title)
        chunk_anchor = str(item.get("source_anchor") or "")
        if clause_code and clause_code != section_code:
            anchor_title = clause_title or chunk_section_title or ""
            clause_anchor = f"Clause {clause_code}"
            if anchor_title:
                clause_anchor = f"{clause_anchor} {anchor_title}"
            chunk_anchor = f"{chunk_anchor} | {clause_anchor}" if chunk_anchor else clause_anchor

        req_code = base_req_code if idx == 0 else f"{base_req_code}-{idx + 1}"
        cleaned_chunk = str(chunk or "").strip()
        rows.append(
            {
                "req_code": req_code,
                "entry_id": req_code,
                "entry_kind": "REQUIREMENT",
                "appendix_scope": appendix_scope,
                "section_code": chunk_section_code,
                "section_key": chunk_section_key,
                "section_level": _infer_section_level(chunk_section_code),
                "source_page": source_page,
                "source_page_end": source_page_end,
                "source_anchor": chunk_anchor,
                "section_title": chunk_section_title,
                "requirement_type": item.get("requirement_type", ""),
                "priority": item.get("priority", ""),
                "text_length": len(cleaned_chunk),
                "requirement_text": cleaned_chunk,
                "req_order": req_order_start + idx,
                "source_line_index": _to_int(item.get("source_line_index")),
                "source_line_index_end": _to_int(item.get("source_line_index_end")),
                "section_instance_key": "",
            }
        )
    return rows


def _should_promote_local_clause_to_section(*, parent_section_code: str, clause_code: str) -> bool:
    parent_code = str(parent_section_code or "").strip()
    local_code = str(clause_code or "").strip()
    if not local_code:
        return False
    if not parent_code:
        return True
    if local_code == parent_code:
        return False
    if local_code.startswith(f"{parent_code}."):
        return True
    # Three-level clause numbering like 8.1.2 is specific enough to treat as a
    # real local clause anchor even when the parent section token is stale.
    if local_code.count(".") >= 2:
        return True
    return False


def _extract_local_clause_code_and_title(text: str) -> tuple[str, str]:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return "", ""
    match = LOCAL_CLAUSE_RE.match(lines[0])
    if not match:
        return "", ""
    code = str(match.group(1) or "").strip()
    title = str(match.group(2) or "").strip()
    if not code or not title:
        return "", ""
    return code, title


def _dedupe_table_assets(assets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_table_ids: set[str] = set()
    for asset in assets:
        table_id = str(asset.get("table_id") or "").strip()
        if not table_id or table_id in seen_table_ids:
            continue
        seen_table_ids.add(table_id)
        deduped.append(asset)
    return deduped


def _extract_section_code(anchor: str) -> str:
    clause_match = CLAUSE_ANCHOR_RE.search(anchor or "")
    match = SECTION_ANCHOR_RE.search(anchor or "")
    section_code = str(match.group(1) or "").strip() if match else ""
    clause_code = str(clause_match.group(1) or "").strip() if clause_match else ""
    if clause_code and _should_promote_local_clause_to_section(parent_section_code=section_code, clause_code=clause_code):
        return clause_code
    return section_code or clause_code


def _extract_section_title(anchor: str) -> str:
    clause_match = CLAUSE_ANCHOR_RE.search(anchor or "")
    match = SECTION_ANCHOR_RE.search(anchor or "")
    section_code = str(match.group(1) or "").strip() if match else ""
    clause_code = str(clause_match.group(1) or "").strip() if clause_match else ""
    if clause_match and _should_promote_local_clause_to_section(parent_section_code=section_code, clause_code=clause_code):
        title = str(clause_match.group(2) or "").strip()
        title = re.sub(r"\s+", " ", title)
        if title:
            return title[:120]
    if match:
        title = str(match.group(2) or "").strip()
        title = re.sub(r"\s+", " ", title)
        return title[:120]
    if clause_match:
        title = str(clause_match.group(2) or "").strip()
        title = re.sub(r"\s+", " ", title)
        if title:
            return title[:120]
    title_match = TITLE_ANCHOR_RE.search(anchor or "")
    if not title_match:
        return ""
    title = str(title_match.group(1) or "").strip()
    title = re.sub(r"\s+", " ", title)
    return title[:120]


def _extract_appendix_scope(anchor: str) -> str:
    match = APPENDIX_SCOPE_RE.search(anchor or "")
    return match.group(1).upper() if match else ""


def _join_section_key(appendix_scope: str, section_code: str) -> str:
    scope = str(appendix_scope or "").strip().upper()
    code = str(section_code or "").strip()
    if not code:
        return ""
    return f"{scope}:{code}" if scope else code


def _join_title_section_key(appendix_scope: str, section_title: str) -> str:
    title = str(section_title or "").strip()
    token = _entry_token(title)
    if not token:
        return ""
    scope = str(appendix_scope or "").strip().upper()
    body = f"@{token}"
    return f"{scope}:{body}" if scope else body


def _split_section_key(section_key: str) -> tuple[str, str]:
    value = str(section_key or "").strip()
    if not value:
        return ("", "")
    if ":" not in value:
        if value.startswith("@"):
            return ("", "")
        return ("", value)
    left, right = value.split(":", 1)
    if right.strip().startswith("@"):
        return (left.strip().upper(), "")
    return (left.strip().upper(), right.strip())


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _build_missing_test_outline_scope_requirements(
    segments: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    existing_requirements: list[dict[str, Any]],
    start_req_order: int,
) -> list[dict[str, Any]]:
    if not segments or not markers:
        return []

    scope_has_requirements = {
        str(row.get("appendix_scope") or "").strip().upper()
        for row in existing_requirements
        if str(row.get("appendix_scope") or "").strip()
    }
    pages_text: dict[int, str] = {}
    for seg in segments:
        page = _to_int(seg.get("source_page"))
        text = str(seg.get("text") or "").strip()
        if page <= 0 or not text:
            continue
        pages_text[page] = text

    out: list[dict[str, Any]] = []
    req_order = max(-1, int(start_req_order) - 1)
    for marker in markers:
        scope = str(marker.get("appendix_scope") or "").strip().upper()
        if not scope:
            continue
        if scope in scope_has_requirements:
            continue
        start_page = _to_int(marker.get("source_page_start"))
        end_page = _to_int(marker.get("source_page_end")) or start_page
        if start_page <= 0:
            continue
        if end_page < start_page:
            end_page = start_page

        chunk_lines: list[str] = []
        for page in range(start_page, end_page + 1):
            text = pages_text.get(page, "")
            if not text:
                continue
            chunk_lines.append(text)
        requirement_text = "\n".join(chunk_lines).strip()
        if len(requirement_text) < 12:
            continue

        section_code = scope
        section_key = _join_section_key(scope, section_code)
        section_title = str(marker.get("section_title") or f"附录{scope}").strip()[:160]
        req_order += 1
        out.append(
            {
                "req_code": f"REQ-{scope}{req_order:04d}",
                "entry_id": f"REQ-{scope}{req_order:04d}",
                "entry_kind": "REQUIREMENT",
                "appendix_scope": scope,
                "section_code": section_code,
                "section_key": section_key,
                "section_level": 1,
                "source_page": start_page,
                "source_page_end": end_page,
                "source_anchor": f"Appendix {scope} | Synthetic Marker Requirement",
                "section_title": section_title,
                "requirement_type": "",
                "priority": "",
                "text_length": len(requirement_text),
                "requirement_text": requirement_text,
                "req_order": req_order,
                "source_line_index": 0,
                "source_line_index_end": 0,
                "section_instance_key": "",
            }
        )
        scope_has_requirements.add(scope)
    return out


def _outline_order_sort_key(value: Any) -> int:
    if value is None:
        return 10**9
    text = str(value).strip()
    if not text:
        return 10**9
    try:
        parsed = int(float(text))
    except ValueError:
        return 10**9
    if parsed < 0:
        return 10**9
    return parsed


def _optional_nonnegative_int_sort_key(value: Any, *, allow_zero: bool = True) -> int:
    if value is None:
        return 10**9
    text = str(value).strip()
    if not text:
        return 10**9
    try:
        parsed = int(float(text))
    except ValueError:
        return 10**9
    if parsed < 0:
        return 10**9
    if not allow_zero and parsed == 0:
        return 10**9
    return parsed


def _preview_section_row_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, int, int, str]:
    tree_order = _outline_order_sort_key(row.get("tree_order"))
    source_page = _optional_nonnegative_int_sort_key(row.get("source_page_start"), allow_zero=False)
    source_line = _optional_nonnegative_int_sort_key(row.get("source_line_index_start"))
    first_req_order = _optional_nonnegative_int_sort_key(row.get("first_req_order"))
    outline_order = _outline_order_sort_key(row.get("outline_order"))
    section_level = _optional_nonnegative_int_sort_key(row.get("section_level"))
    structural_kind = str(row.get("structural_kind") or "").strip().lower()
    kind_rank = 0 if structural_kind == "outline" or row.get("outline_order") is not None else 1
    section_instance_key = str(row.get("section_instance_key") or row.get("section_key") or "")
    return (
        0 if tree_order < 10**9 else 1,
        tree_order,
        1 if source_page >= 10**9 else 0,
        source_page,
        kind_rank,
        outline_order,
        source_line,
        first_req_order,
        section_level,
        section_instance_key,
    )


def _build_canonical_section_rows(
    sections: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not sections:
        return [], {
            "zero_direct_requirement_parent_count": 0,
            "synthetic_section_count": 0,
            "orphan_section_count": 0,
        }

    copied = [dict(row) for row in sections]
    node_key_by_section_key: dict[str, str] = {}
    any_node_key_by_section_key: dict[str, str] = {}
    children_by_parent_key: dict[str, list[dict[str, Any]]] = {}
    root_nodes: list[dict[str, Any]] = []

    for index, node in enumerate(copied):
        node_key = str(node.get("section_instance_key") or node.get("section_key") or "").strip()
        if not node_key:
            node_key = f"section-node-{index}"
        node["_structural_node_key"] = node_key
        structural_kind = "outline" if node.get("outline_order") is not None else "synthetic"
        node["structural_kind"] = structural_kind
        section_key = str(node.get("section_key") or "").strip()
        if section_key and section_key not in any_node_key_by_section_key:
            any_node_key_by_section_key[section_key] = node_key
        if structural_kind == "outline" and section_key and section_key not in node_key_by_section_key:
            node_key_by_section_key[section_key] = node_key

    for node in copied:
        parent_key = _resolve_structural_parent_node_key(
            node,
            outline_node_key_by_section_key=node_key_by_section_key,
            any_node_key_by_section_key=any_node_key_by_section_key,
        )
        if parent_key == node.get("_structural_node_key"):
            parent_key = ""
        node["structural_parent_key"] = parent_key
        if parent_key:
            children_by_parent_key.setdefault(parent_key, []).append(node)
        else:
            root_nodes.append(node)

    ordered: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        node["tree_order"] = len(ordered)
        ordered.append(node)
        node_key = str(node.get("_structural_node_key") or "").strip()
        children = children_by_parent_key.get(node_key, [])
        for child in sorted(children, key=_section_tree_child_sort_key):
            visit(child)

    for node in sorted(root_nodes, key=_section_tree_child_sort_key):
        visit(node)

    for node in ordered:
        node.pop("_structural_node_key", None)

    stats = {
        "zero_direct_requirement_parent_count": sum(
            1 for row in ordered if bool(row.get("has_children")) and _to_int(row.get("requirement_count")) <= 0
        ),
        "synthetic_section_count": sum(1 for row in ordered if str(row.get("structural_kind") or "") == "synthetic"),
        "orphan_section_count": sum(
            1
            for row in ordered
            if str(row.get("structural_kind") or "") == "synthetic"
            and not str(row.get("structural_parent_key") or "").strip()
        ),
    }
    return ordered, stats


def _resolve_structural_parent_node_key(
    node: dict[str, Any],
    *,
    outline_node_key_by_section_key: dict[str, str],
    any_node_key_by_section_key: dict[str, str],
) -> str:
    appendix_scope = str(node.get("appendix_scope") or "").strip().upper()
    section_code = str(node.get("section_code") or "").strip()
    parent_code = str(node.get("parent_section_code") or "").strip()
    if not parent_code:
        parent_code = _parent_section_code(section_code)

    while parent_code:
        parent_section_key = _join_section_key(appendix_scope, parent_code)
        parent_key = outline_node_key_by_section_key.get(parent_section_key) or any_node_key_by_section_key.get(
            parent_section_key
        )
        if parent_key:
            return parent_key
        parent_code = _parent_section_code(parent_code)
    return ""


def _section_tree_child_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    structural_kind = str(row.get("structural_kind") or "").strip().lower()
    outline_order = _outline_order_sort_key(row.get("outline_order"))
    source_page = _optional_nonnegative_int_sort_key(row.get("source_page_start"), allow_zero=False)
    source_line = _optional_nonnegative_int_sort_key(row.get("source_line_index_start"))
    first_req_order = _optional_nonnegative_int_sort_key(row.get("first_req_order"))
    section_level = _optional_nonnegative_int_sort_key(row.get("section_level"))
    section_instance_key = str(row.get("section_instance_key") or row.get("section_key") or "")
    if structural_kind == "outline":
        return (
            0,
            outline_order,
            source_page,
            source_line,
            first_req_order,
            section_level,
            section_instance_key,
        )
    return (
        1,
        source_page,
        source_line,
        first_req_order,
        outline_order,
        section_level,
        section_instance_key,
    )


def _section_sort_key(section_code: str) -> tuple[int, ...]:
    parts = []
    for token in section_code.split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(9999)
    return tuple(parts)


def _scope_sort_key(scope: str) -> tuple[int, int]:
    normalized = str(scope or "").strip().upper()
    if not normalized:
        return (0, 0)
    first = normalized[:1]
    if "A" <= first <= "Z":
        return (1, ord(first) - ord("A"))
    return (2, 0)


def _parent_section_code(section_code: str) -> str:
    if not section_code or "." not in section_code:
        return ""
    return section_code.rsplit(".", 1)[0]


def _collect_appendix_markers(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    markers: dict[str, dict[str, Any]] = {}
    ordered_segments = sorted(
        segments,
        key=lambda seg: (
            seg.get("source_page") is None,
            seg.get("source_page") if seg.get("source_page") is not None else 10**9,
            seg.get("source_anchor") or "",
        ),
    )
    for seg in ordered_segments:
        source_page = _to_int(seg.get("source_page"))
        for raw_line in str(seg.get("text", "")).splitlines():
            line = " ".join(str(raw_line or "").strip().split())
            if not line:
                continue
            match = APPENDIX_MARKER_LINE_RE.match(line)
            if not match:
                continue
            scope = _normalize_appendix_token(match.group(1) or "")
            if not scope:
                continue
            if scope in markers:
                continue
            markers[scope] = {
                "appendix_scope": scope,
                "section_title": line,
                "source_page_start": source_page if source_page > 0 else None,
                "source_page_end": source_page if source_page > 0 else None,
            }
    return sorted(markers.values(), key=lambda row: _scope_sort_key(str(row.get("appendix_scope", ""))))


def _collect_test_outline_markers(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_segments = sorted(
        segments,
        key=lambda seg: (
            seg.get("source_page") is None,
            seg.get("source_page") if seg.get("source_page") is not None else 10**9,
            seg.get("source_anchor") or "",
        ),
    )
    markers_by_scope: dict[str, dict[str, Any]] = {}
    for seg in ordered_segments:
        source_page = _to_int(seg.get("source_page"))
        for raw_line in str(seg.get("text", "")).splitlines():
            line = " ".join(str(raw_line or "").strip().split())
            if not line:
                continue
            for scope, candidate in _iter_test_outline_candidates(line):
                if not scope or scope in markers_by_scope:
                    continue
                markers_by_scope[scope] = {
                    "appendix_scope": scope,
                    "section_title": candidate,
                    "source_page_start": source_page if source_page > 0 else None,
                    "source_page_end": source_page if source_page > 0 else None,
                }
    return sorted(markers_by_scope.values(), key=lambda row: _scope_sort_key(str(row.get("appendix_scope", ""))))


def _iter_test_outline_candidates(line: str) -> list[tuple[str, str]]:
    candidates: list[tuple[str, str]] = []
    raw = str(line or "").strip()
    if not raw:
        return candidates

    if ELECTRIC_TEST_OUTLINE_HEADER_RE.search(raw):
        candidates.append(("Y", "用电器试验大纲"))

    cells = [raw]
    if "|" in raw:
        cells = [part.strip() for part in raw.split("|") if part.strip()]

    for cell in cells:
        value = " ".join(cell.split())
        if not value:
            continue
        if len(value) < 4 or len(value) > 120:
            continue
        if APPENDIX_MARKER_LINE_RE.match(value):
            continue
        if TEST_OUTLINE_EXCLUDE_RE.search(value):
            continue
        for pattern, scope, canonical in TEST_OUTLINE_MARKER_PATTERNS:
            if pattern.search(value):
                candidates.append((scope, canonical))
                break
    return candidates


def _normalize_marker_key(value: str) -> str:
    text = re.sub(r"\s+", "", str(value or "").lower())
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text


def _inject_appendix_marker_rows(
    section_outline: list[dict[str, Any]],
    appendix_markers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not section_outline and not appendix_markers:
        return []
    marker_by_scope = {
        str(row.get("appendix_scope") or "").strip().upper(): row
        for row in appendix_markers
        if str(row.get("appendix_scope") or "").strip()
    }
    injected: set[str] = set()
    out: list[dict[str, Any]] = []

    for row in section_outline:
        scope = str(row.get("appendix_scope") or "").strip().upper()
        marker = marker_by_scope.get(scope)
        if scope and marker and scope not in injected:
            out.append(_build_appendix_outline_row(marker))
            injected.add(scope)
        merged = dict(row)
        merged["is_appendix_marker"] = False
        out.append(merged)

    for scope, marker in marker_by_scope.items():
        if scope in injected:
            continue
        out.append(_build_appendix_outline_row(marker))
    return out


def _build_appendix_outline_row(marker: dict[str, Any]) -> dict[str, Any]:
    title = str(marker.get("section_title") or "")
    return {
        "appendix_scope": str(marker.get("appendix_scope") or "").strip().upper(),
        "section_code": "",
        "section_title": title,
        "section_level": 0,
        "parent_section_code": "",
        "has_children": True,
        "is_leaf": False,
        "source_page_start": marker.get("source_page_start"),
        "source_page_end": marker.get("source_page_end"),
        "line_count": 1,
        "char_count": len(title),
        "text_preview": title[:280],
        "requirement_like": False,
        "is_appendix_marker": True,
    }


def _normalize_appendix_token(value: str) -> str:
    token = str(value or "").strip().upper()
    if not token:
        return ""
    token = token.translate(
        str.maketrans(
            "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        )
    )
    return token if re.fullmatch(r"[A-Z]", token) else ""


def _infer_section_level(section_code: str) -> int:
    code = str(section_code or "").strip()
    if not code:
        return 0
    if re.fullmatch(r"\d+(?:\.\d+){0,8}", code):
        return code.count(".") + 1
    return 1


def _collect_section_instance_groups(
    requirements: list[dict[str, Any]],
    outline_nodes: list[dict[str, Any]] | None = None,
) -> dict[str, list[tuple[str, str]]]:
    grouped: dict[str, dict[str, str]] = {}

    def add_title(section_key: str, title: str) -> None:
        key = str(section_key or "").strip()
        if not key:
            return
        raw_title = str(title or "").strip()
        if not raw_title:
            return
        title_norm = _normalize_instance_title(raw_title) or "untitled"
        bucket = grouped.setdefault(key, {})
        if title_norm not in bucket:
            bucket[title_norm] = raw_title

    for row in requirements:
        section_key = str(row.get("section_key") or "").strip()
        if not section_key:
            continue
        title = str(row.get("section_title") or "").strip()
        if not title:
            title = _derive_section_title_from_requirement(
                text=str(row.get("requirement_text") or ""),
                section_code=str(row.get("section_code") or ""),
            )
        add_title(section_key, title)

    for node in outline_nodes or []:
        appendix_scope = str(node.get("appendix_scope") or "").strip().upper()
        section_code = str(node.get("section_code") or "").strip()
        section_key = str(node.get("section_key") or "").strip()
        if not section_key:
            section_key = _join_section_key(appendix_scope, section_code)
        if not section_key:
            section_key = _join_title_section_key(appendix_scope, str(node.get("section_title") or ""))
        add_title(section_key, str(node.get("section_title") or ""))

    out: dict[str, list[tuple[str, str]]] = {}
    for section_key, bucket in grouped.items():
        ordered = sorted(
            bucket.items(),
            key=lambda item: (
                0 if item[0] != "untitled" else 1,
                item[0],
            ),
        )
        out[section_key] = ordered
    return out


def _assign_section_instance_keys(
    requirements: list[dict[str, Any]],
    outline_nodes: list[dict[str, Any]] | None = None,
) -> None:
    groups = _collect_section_instance_groups(requirements, outline_nodes)
    mapping: dict[tuple[str, str], str] = {}
    for section_key, title_pairs in groups.items():
        if len(title_pairs) <= 1:
            title_norm = title_pairs[0][0] if title_pairs else "untitled"
            mapping[(section_key, title_norm)] = section_key
            continue
        for title_norm, raw_title in title_pairs:
            suffix = _entry_token(raw_title) or _entry_token(title_norm) or "entry"
            mapping[(section_key, title_norm)] = f"{section_key}#{suffix}"

    for row in requirements:
        section_key = str(row.get("section_key") or "").strip()
        if not section_key:
            row["section_instance_key"] = ""
            continue
        title = str(row.get("section_title") or "").strip()
        if not title:
            title = _derive_section_title_from_requirement(
                text=str(row.get("requirement_text") or ""),
                section_code=str(row.get("section_code") or ""),
            )
        title_norm = _normalize_instance_title(title) or "untitled"
        row["section_instance_key"] = mapping.get((section_key, title_norm), section_key)


def _resolve_outline_section_instance_key(
    node: dict[str, Any],
    section_key: str,
    grouped_instances: dict[str, list[tuple[str, str]]],
) -> str:
    if not section_key:
        return ""
    pairs = grouped_instances.get(section_key, [])
    if not pairs:
        return section_key
    if len(pairs) == 1:
        return section_key
    section_title = str(node.get("section_title") or "").strip()
    title_norm = _normalize_instance_title(section_title)
    if title_norm:
        for item_norm, raw_title in pairs:
            if title_norm == item_norm or title_norm in _normalize_instance_title(raw_title):
                suffix = _entry_token(raw_title) or _entry_token(item_norm) or "entry"
                return f"{section_key}#{suffix}"
    # Keep base key for outline when no title match; concrete requirement rows will be emitted
    # by synthetic heading fallback.
    return section_key


def _normalize_instance_title(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "", text)
    return text[:120]


def _entry_token(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"\s+", "_", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:48]


def _derive_section_title_from_requirement(text: str, section_code: str) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = lines[0]
    code = str(section_code or "").strip()
    if code:
        first_norm = re.sub(r"\s+", "", first)
        code_norm = re.sub(r"\s+", "", code)
        if first_norm.startswith(code_norm):
            tail = first[len(code):].strip(" .、:：)\t")
            if tail:
                return tail[:120]
    return first[:120]


def _align_section_node_to_first_requirement_position(
    node: dict[str, Any],
    grouped: list[dict[str, Any]],
) -> dict[str, Any]:
    if not grouped:
        return node

    source_page = _to_int(node.get("source_page_start"))
    source_page_end = _to_int(node.get("source_page_end")) or source_page
    source_line = _to_int(node.get("source_line_index_start"))
    line_count = _to_int(node.get("line_count"))
    first_req = grouped[0]
    req_page = _to_int(first_req.get("source_page"))
    req_line = _to_int(first_req.get("source_line_index"))

    if req_page <= 0 or req_line <= 0:
        return node
    if req_page != source_page:
        return node
    if source_line > 0:
        return node

    # Synthetic split-clause section nodes rely on first_req_order for stable ordering.
    # If we align them by a later spanning requirement line, cross-page children like
    # "4）外观要求" can jump ahead of earlier siblings "2）/3）".
    has_real_outline_position = bool(node.get("outline_order") is not None or line_count > 0)
    if not has_real_outline_position:
        return node

    is_large_spanning_section = source_page_end > source_page or line_count >= 12
    if not is_large_spanning_section:
        return node

    aligned = dict(node)
    aligned["source_page_start"] = req_page
    aligned["source_line_index_start"] = req_line
    source_line_end = _to_int(aligned.get("source_line_index_end"))
    if source_line_end <= 0:
        aligned["source_line_index_end"] = req_line
    return aligned


def _build_complete_requirement_entries(
    requirements: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    req_groups: dict[str, list[dict[str, Any]]] = {}
    orphan_requirements: list[dict[str, Any]] = []
    for req in requirements:
        section_instance_key = str(req.get("section_instance_key") or req.get("section_key") or "").strip()
        if section_instance_key:
            req_groups.setdefault(section_instance_key, []).append(req)
        else:
            orphan_requirements.append(req)

    for group in req_groups.values():
        group.sort(
            key=lambda row: (
                _to_int(row.get("req_order")),
                _to_int(row.get("source_page")),
                _to_int(row.get("source_page_end")),
                str(row.get("req_code") or ""),
            )
        )

    entries: list[dict[str, Any]] = []
    seen_entry_ids: set[str] = set()
    emitted_section_instance_keys: set[str] = set()
    heading_seq = 0
    preferred_section_rows = _select_preferred_section_rows_for_requirements(sections, req_groups)
    aligned_section_nodes = {
        id(node): _align_section_node_to_first_requirement_position(
            node,
            req_groups.get(str(node.get("section_instance_key") or node.get("section_key") or "").strip(), []),
        )
        for node in sections
    }

    for node in sorted(sections, key=lambda row: _preview_section_row_sort_key(aligned_section_nodes.get(id(row), row))):
        section_instance_key = str(node.get("section_instance_key") or node.get("section_key") or "").strip()
        grouped = req_groups.get(section_instance_key, [])
        if (
            not grouped
            and not bool(node.get("is_appendix_marker", False))
            and _has_instance_children(req_groups, str(node.get("section_key") or "").strip())
            and section_instance_key == str(node.get("section_key") or "").strip()
        ):
            # Hide empty base node when all concrete requirements are split by section instance key.
            continue
        heading_seq += 1
        heading = _build_heading_entry(node=aligned_section_nodes.get(id(node), node), heading_seq=heading_seq, synthetic=False)
        entry_id = str(heading.get("entry_id") or "")
        if entry_id and entry_id not in seen_entry_ids:
            entries.append(heading)
            seen_entry_ids.add(entry_id)

        if not section_instance_key:
            continue
        if not grouped:
            continue
        preferred_row = preferred_section_rows.get(section_instance_key)
        if preferred_row is not None and preferred_row is not node:
            continue
        emitted_section_instance_keys.add(section_instance_key)
        section_code = str(node.get("section_code") or "")
        heading_title = str(heading.get("section_title") or "")
        for req in grouped:
            normalized_text = _strip_heading_prefix_from_requirement(
                text=str(req.get("requirement_text") or ""),
                section_code=section_code,
                heading_title=heading_title,
            )
            if not normalized_text.strip():
                normalized_text = str(req.get("requirement_text") or "").strip()
            if not normalized_text.strip():
                continue
            if bool(node.get("has_children")) and _looks_like_parent_heading_spill(normalized_text, section_code):
                continue
            entries.append(_copy_requirement_entry(req, normalized_text))

    missing_section_instance_keys = [key for key in req_groups.keys() if key not in emitted_section_instance_keys]
    missing_section_instance_keys.sort(
        key=lambda key: min((_to_int(row.get("req_order")) for row in req_groups.get(key, [])), default=10**9)
    )

    for section_instance_key in missing_section_instance_keys:
        grouped = req_groups.get(section_instance_key, [])
        if not grouped:
            continue
        seed = grouped[0]
        heading_seq += 1
        section_key = str(seed.get("section_key") or "")
        synthetic_node = _find_section_row_for_key(sections, section_key) or {
            "appendix_scope": seed.get("appendix_scope", ""),
            "section_code": seed.get("section_code", ""),
            "section_title": seed.get("section_title", ""),
            "section_level": seed.get("section_level") or _infer_section_level(str(seed.get("section_code") or "")),
            "parent_section_code": _parent_section_code(str(seed.get("section_code") or "")),
            "source_page_start": seed.get("source_page"),
            "source_page_end": seed.get("source_page_end"),
            "is_appendix_marker": False,
            "section_instance_key": section_instance_key,
        }
        synthetic_node["section_instance_key"] = section_instance_key
        heading = _build_heading_entry(node=synthetic_node, heading_seq=heading_seq, synthetic=True)
        entries.append(heading)
        section_code = str(synthetic_node.get("section_code") or "")
        heading_title = str(heading.get("section_title") or "")
        for req in grouped:
            normalized_text = _strip_heading_prefix_from_requirement(
                text=str(req.get("requirement_text") or ""),
                section_code=section_code,
                heading_title=heading_title,
            )
            if not normalized_text.strip():
                normalized_text = str(req.get("requirement_text") or "").strip()
            if not normalized_text.strip():
                continue
            if bool(synthetic_node.get("has_children")) and _looks_like_parent_heading_spill(normalized_text, section_code):
                continue
            entries.append(_copy_requirement_entry(req, normalized_text))

    orphan_requirements.sort(
        key=lambda row: (
            _to_int(row.get("req_order")),
            _to_int(row.get("source_page")),
            _to_int(row.get("source_page_end")),
            str(row.get("req_code") or ""),
        )
    )
    for req in orphan_requirements:
        entries.append(_copy_requirement_entry(req, str(req.get("requirement_text") or "")))

    return entries


def _select_preferred_section_rows_for_requirements(
    sections: list[dict[str, Any]],
    req_groups: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, Any]]:
    by_instance: dict[str, list[dict[str, Any]]] = {}
    for node in sections:
        section_instance_key = str(node.get("section_instance_key") or node.get("section_key") or "").strip()
        if not section_instance_key:
            continue
        by_instance.setdefault(section_instance_key, []).append(node)

    preferred: dict[str, dict[str, Any]] = {}
    for section_instance_key, nodes in by_instance.items():
        if len(nodes) <= 1:
            if nodes:
                preferred[section_instance_key] = nodes[0]
            continue
        grouped = req_groups.get(section_instance_key) or []
        if not grouped:
            preferred[section_instance_key] = nodes[0]
            continue
        target_title = _normalize_instance_title(
            str(grouped[0].get("section_title") or _derive_section_title_from_requirement(
                text=str(grouped[0].get("requirement_text") or ""),
                section_code=str(grouped[0].get("section_code") or ""),
            ))
        )
        target_page = _to_int(grouped[0].get("source_page"))

        def sort_key(node: dict[str, Any]) -> tuple[int, int, int, int, str]:
            title = _normalize_instance_title(str(node.get("section_title") or ""))
            page = _to_int(node.get("source_page_start"))
            title_penalty = 0 if target_title and title == target_title else 1
            page_penalty = abs(page - target_page) if target_page > 0 and page > 0 else 10**6
            outline_order = _outline_order_sort_key(node.get("outline_order"))
            return (
                title_penalty,
                page_penalty,
                0 if page > 0 else 1,
                outline_order,
                str(node.get("section_title") or ""),
            )

        preferred[section_instance_key] = min(nodes, key=sort_key)
    return preferred


def _has_instance_children(req_groups: dict[str, list[dict[str, Any]]], section_key: str) -> bool:
    if not section_key:
        return False
    prefix = f"{section_key}#"
    return any(key.startswith(prefix) for key in req_groups.keys())


def _find_section_row_for_key(sections: list[dict[str, Any]], section_key: str) -> dict[str, Any] | None:
    for row in sections:
        if str(row.get("section_key") or "").strip() == section_key:
            return row
    return None


def _build_heading_entry(node: dict[str, Any], heading_seq: int, synthetic: bool) -> dict[str, Any]:
    scope = str(node.get("appendix_scope") or "").strip().upper()
    section_code = str(node.get("section_code") or "").strip()
    section_title = str(node.get("section_title") or "").strip()
    section_key = str(node.get("section_key") or "").strip() or _join_section_key(scope, section_code)
    if not section_key and section_title:
        section_key = _join_title_section_key(scope, section_title)
    section_instance_key = str(node.get("section_instance_key") or section_key or "").strip()
    if not section_title:
        if section_code:
            section_title = section_code
        elif scope:
            section_title = f"附录{scope}"
        else:
            section_title = "正文（Main）"

    source_page = _to_int(node.get("source_page_start"))
    source_page_end = _to_int(node.get("source_page_end")) or source_page
    source_line_index = _to_int(node.get("source_line_index_start"))
    source_line_index_end = _to_int(node.get("source_line_index_end")) or source_line_index
    section_level = _to_int(node.get("section_level")) or _infer_section_level(section_code)
    parent_section_code = str(node.get("parent_section_code") or "").strip()

    scope_token = scope or "MAIN"
    code_token = section_code or "ROOT"
    instance_token = _entry_token(section_instance_key) if section_instance_key else f"{scope_token}-{code_token}"
    entry_id = f"H-{instance_token}-{heading_seq}"

    return {
        "entry_id": entry_id,
        "entry_kind": "HEADING",
        "req_code": "",
        "appendix_scope": scope,
        "section_code": section_code,
        "section_key": section_key,
        "section_instance_key": section_instance_key,
        "section_level": section_level,
        "parent_section_code": parent_section_code,
        "source_page": source_page,
        "source_page_end": source_page_end,
        "source_line_index": source_line_index,
        "source_line_index_end": source_line_index_end,
        "source_anchor": "",
        "section_title": section_title,
        "requirement_type": "",
        "priority": "",
        "text_length": len(section_title),
        "requirement_text": section_title,
        "is_appendix_marker": bool(node.get("is_appendix_marker", False)),
        "is_synthetic_heading": bool(synthetic),
    }


def _copy_requirement_entry(row: dict[str, Any], requirement_text: str) -> dict[str, Any]:
    copied = dict(row)
    copied["entry_kind"] = "REQUIREMENT"
    copied["entry_id"] = str(copied.get("entry_id") or copied.get("req_code") or f"REQ-{_to_int(copied.get('req_order'))}")
    copied["section_instance_key"] = str(copied.get("section_instance_key") or copied.get("section_key") or "")
    cleaned_text = str(requirement_text or "").strip()
    copied["requirement_text"] = cleaned_text
    copied["display_text"] = cleaned_text
    copied["raw_text"] = str(row.get("requirement_text") or cleaned_text).strip()
    copied["heading_text"] = ""
    copied["text_relation"] = "distinct"
    copied["is_deduped"] = False
    copied["merged_from_entry_ids"] = [str(copied.get("entry_id") or "")]
    copied["merged_from_req_codes"] = [str(copied.get("req_code") or "")] if str(copied.get("req_code") or "") else []
    copied["dedupe_version"] = "heading_requirement_v1"
    copied["text_length"] = len(cleaned_text)
    copied["is_appendix_marker"] = False
    copied["is_synthetic_heading"] = False
    return copied


def _attach_requirement_section_context(requirements: list[dict[str, Any]], sections: list[dict[str, Any]]) -> None:
    if not requirements or not sections:
        return
    by_instance_key: dict[str, dict[str, Any]] = {}
    by_section_key: dict[str, dict[str, Any]] = {}
    for row in sections:
        instance_key = str(row.get("section_instance_key") or "").strip()
        section_key = str(row.get("section_key") or "").strip()
        if instance_key and instance_key not in by_instance_key:
            by_instance_key[instance_key] = row
        if section_key and section_key not in by_section_key:
            by_section_key[section_key] = row

    for req in requirements:
        section_instance_key = str(req.get("section_instance_key") or "").strip()
        section_key = str(req.get("section_key") or "").strip()
        appendix_scope = str(req.get("appendix_scope") or "").strip().upper()
        section_row = by_instance_key.get(section_instance_key) or by_section_key.get(section_key)
        section_code = str(req.get("section_code") or "").strip()
        section_title = str(req.get("section_title") or "").strip()
        if section_row:
            if not section_code:
                section_code = str(section_row.get("section_code") or "").strip()
            if not section_title:
                section_title = str(section_row.get("section_title") or "").strip()
        if section_code:
            req["parent_section_code"] = section_code
            req["parent_section_title"] = section_title
            req["parent_section_label"] = _format_section_label(section_code, section_title)
            continue
        if section_title:
            req["parent_section_code"] = ""
            req["parent_section_title"] = section_title
            req["parent_section_label"] = _format_section_label("", section_title)
            continue

        parent_code = ""
        parent_title = ""
        if section_row:
            parent_code = str(section_row.get("parent_section_code") or "").strip()
        if not parent_code:
            parent_code = _parent_section_code(str(req.get("section_code") or "").strip())
        if parent_code:
            parent_key = _join_section_key(appendix_scope, parent_code)
            parent_row = by_section_key.get(parent_key) or by_instance_key.get(parent_key)
            if parent_row:
                parent_title = str(parent_row.get("section_title") or "").strip()
        req["parent_section_code"] = parent_code
        req["parent_section_title"] = parent_title
        req["parent_section_label"] = _format_section_label(parent_code, parent_title)


def _format_section_label(section_code: str, section_title: str) -> str:
    code = str(section_code or "").strip()
    title = str(section_title or "").strip()
    if code and title:
        return f"{code} {title}"
    return code or title


def _build_section_unified_entries(entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not entries:
        return entries, {"dedup_exact_count": 0, "dedup_prefix_count": 0, "dedup_requirement_prefix_count": 0}

    grouped: dict[str, dict[str, Any]] = {}
    for row in entries:
        section_key = str(row.get("section_instance_key") or row.get("section_key") or "").strip()
        if not section_key:
            continue
        bucket = grouped.setdefault(section_key, {"heading": None, "requirements": []})
        kind = str(row.get("entry_kind") or "").upper()
        if kind == "HEADING":
            if bucket.get("heading") is None:
                bucket["heading"] = row
            continue
        if kind in {"REQUIREMENT", "HYBRID"}:
            bucket["requirements"].append(row)

    heading_skip_sections: set[str] = set()
    exact_primary_entry_ids: dict[str, dict[str, Any]] = {}
    exact_extra_drop_ids: set[str] = set()
    relation_meta_by_entry_id: dict[str, dict[str, Any]] = {}

    dedup_exact_count = 0
    dedup_prefix_count = 0
    dedup_requirement_prefix_count = 0

    for section_key, bucket in grouped.items():
        heading = bucket.get("heading")
        requirements = list(bucket.get("requirements") or [])
        if not heading or not requirements:
            continue
        heading_text = str(heading.get("section_title") or heading.get("requirement_text") or "").strip()
        section_code = str(heading.get("section_code") or "")
        if not heading_text:
            continue

        section_exact_rows: list[dict[str, Any]] = []
        for req in requirements:
            entry_id = str(req.get("entry_id") or "")
            if not entry_id:
                continue
            req_text = str(req.get("requirement_text") or "").strip()
            relation_info = _classify_heading_requirement_relation(
                heading_text=heading_text,
                requirement_text=req_text,
                section_code=section_code,
            )
            relation_meta_by_entry_id[entry_id] = {
                "section_key": section_key,
                "heading_entry_id": str(heading.get("entry_id") or ""),
                "heading_text": heading_text,
                **relation_info,
            }
            relation = str(relation_info.get("relation") or "")
            if relation == "exact_match":
                section_exact_rows.append(req)
            elif relation == "heading_prefix":
                delta_text = str(relation_info.get("delta_text") or "").strip()
                if delta_text and delta_text != req_text:
                    dedup_prefix_count += 1
            elif relation == "requirement_prefix":
                dedup_requirement_prefix_count += 1

        if section_exact_rows:
            heading_skip_sections.add(section_key)
            dedup_exact_count += len(section_exact_rows)
            primary = section_exact_rows[0]
            primary_entry_id = str(primary.get("entry_id") or "")
            if primary_entry_id:
                exact_primary_entry_ids[primary_entry_id] = {
                    "heading": heading,
                    "meta": relation_meta_by_entry_id.get(primary_entry_id, {}),
                }
            for row in section_exact_rows[1:]:
                extra_id = str(row.get("entry_id") or "")
                if extra_id:
                    exact_extra_drop_ids.add(extra_id)

    out: list[dict[str, Any]] = []
    for row in entries:
        kind = str(row.get("entry_kind") or "").upper()
        section_key = str(row.get("section_instance_key") or row.get("section_key") or "").strip()
        entry_id = str(row.get("entry_id") or "")

        if kind == "HEADING":
            if section_key and section_key in heading_skip_sections:
                continue
            out.append(_decorate_heading_entry(row))
            continue

        if kind not in {"REQUIREMENT", "HYBRID"}:
            out.append(dict(row))
            continue

        if entry_id and entry_id in exact_extra_drop_ids:
            continue
        if entry_id and entry_id in exact_primary_entry_ids:
            exact_bundle = exact_primary_entry_ids[entry_id]
            out.append(_build_hybrid_entry_from_exact(row, exact_bundle.get("heading", {}), exact_bundle.get("meta", {})))
            continue

        meta = relation_meta_by_entry_id.get(entry_id, {})
        relation = str(meta.get("relation") or "distinct")
        if relation == "heading_prefix":
            out.append(_build_requirement_entry_with_delta(row, meta))
            continue

        out.append(_decorate_requirement_entry(row, relation=relation, heading_text=str(meta.get("heading_text") or "")))

    stats = {
        "dedup_exact_count": dedup_exact_count,
        "dedup_prefix_count": dedup_prefix_count,
        "dedup_requirement_prefix_count": dedup_requirement_prefix_count,
    }
    return out, stats


def _decorate_heading_entry(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    text = str(out.get("requirement_text") or out.get("section_title") or "").strip()
    out["display_text"] = text
    out["raw_text"] = text
    out["heading_text"] = text
    out["text_relation"] = "distinct"
    out["is_deduped"] = False
    out["merged_from_entry_ids"] = [str(out.get("entry_id") or "")]
    out["merged_from_req_codes"] = []
    out["dedupe_version"] = "heading_requirement_v1"
    return out


def _decorate_requirement_entry(row: dict[str, Any], *, relation: str, heading_text: str = "") -> dict[str, Any]:
    out = dict(row)
    raw = str(out.get("raw_text") or out.get("requirement_text") or "").strip()
    display = str(out.get("display_text") or out.get("requirement_text") or raw).strip() or raw
    out["entry_kind"] = str(out.get("entry_kind") or "REQUIREMENT").upper()
    out["display_text"] = display
    out["raw_text"] = raw
    out["requirement_text"] = display
    out["text_length"] = len(display)
    out["heading_text"] = heading_text or str(out.get("heading_text") or "")
    out["text_relation"] = relation or "distinct"
    out["is_deduped"] = bool(relation and relation != "distinct")
    merged_ids = out.get("merged_from_entry_ids")
    if not isinstance(merged_ids, list) or not merged_ids:
        merged_ids = [str(out.get("entry_id") or "")]
    out["merged_from_entry_ids"] = [str(item) for item in merged_ids if str(item).strip()]
    merged_codes = out.get("merged_from_req_codes")
    if not isinstance(merged_codes, list):
        merged_codes = []
    if not merged_codes and str(out.get("req_code") or "").strip():
        merged_codes = [str(out.get("req_code") or "").strip()]
    out["merged_from_req_codes"] = [str(item) for item in merged_codes if str(item).strip()]
    out["dedupe_version"] = "heading_requirement_v1"
    return out


def _build_hybrid_entry_from_exact(
    requirement_row: dict[str, Any],
    heading_row: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    out = dict(requirement_row)
    heading_text = str(heading_row.get("section_title") or heading_row.get("requirement_text") or "").strip()
    req_raw = str(requirement_row.get("raw_text") or requirement_row.get("requirement_text") or "").strip()
    display = heading_text or req_raw
    out["entry_kind"] = "HYBRID"
    out["heading_text"] = heading_text
    out["display_text"] = display
    out["raw_text"] = req_raw or display
    out["requirement_text"] = display
    out["text_length"] = len(display)
    out["text_relation"] = "exact_match"
    out["is_deduped"] = True
    merged_ids = [str(heading_row.get("entry_id") or ""), str(requirement_row.get("entry_id") or "")]
    out["merged_from_entry_ids"] = [item for item in merged_ids if item]
    merged_codes: list[str] = []
    req_code = str(requirement_row.get("req_code") or "").strip()
    if req_code:
        merged_codes.append(req_code)
    for item in requirement_row.get("merged_from_req_codes") or []:
        token = str(item).strip()
        if token and token not in merged_codes:
            merged_codes.append(token)
    out["merged_from_req_codes"] = merged_codes
    out["dedupe_version"] = "heading_requirement_v1"
    return out


def _build_requirement_entry_with_delta(requirement_row: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    out = dict(requirement_row)
    raw = str(requirement_row.get("raw_text") or requirement_row.get("requirement_text") or "").strip()
    delta = str(meta.get("delta_text") or "").strip()
    display = delta or raw
    out["entry_kind"] = "REQUIREMENT"
    out["display_text"] = display
    out["raw_text"] = raw
    out["requirement_text"] = display
    out["text_length"] = len(display)
    out["heading_text"] = str(meta.get("heading_text") or "")
    out["text_relation"] = "heading_prefix"
    out["is_deduped"] = bool(raw and display != raw)
    merged_ids = out.get("merged_from_entry_ids")
    if not isinstance(merged_ids, list) or not merged_ids:
        merged_ids = [str(out.get("entry_id") or "")]
    out["merged_from_entry_ids"] = [str(item) for item in merged_ids if str(item).strip()]
    merged_codes = out.get("merged_from_req_codes")
    if not isinstance(merged_codes, list):
        merged_codes = []
    if not merged_codes and str(out.get("req_code") or "").strip():
        merged_codes = [str(out.get("req_code") or "").strip()]
    out["merged_from_req_codes"] = [str(item) for item in merged_codes if str(item).strip()]
    out["dedupe_version"] = "heading_requirement_v1"
    return out


def _classify_heading_requirement_relation(
    *,
    heading_text: str,
    requirement_text: str,
    section_code: str,
) -> dict[str, str]:
    heading_raw = str(heading_text or "").strip()
    requirement_raw = str(requirement_text or "").strip()
    if not heading_raw or not requirement_raw:
        return {"relation": "distinct", "delta_text": requirement_raw}

    heading_norm = _normalize_compare_text(heading_raw, section_code)
    requirement_norm = _normalize_compare_text(requirement_raw, section_code)
    if not heading_norm or not requirement_norm:
        return {"relation": "distinct", "delta_text": requirement_raw}
    if heading_norm == requirement_norm:
        return {"relation": "exact_match", "delta_text": ""}
    if requirement_norm.startswith(heading_norm):
        delta = _build_delta_text(requirement_raw, heading_raw, section_code)
        return {"relation": "heading_prefix", "delta_text": delta}
    if heading_norm.startswith(requirement_norm):
        return {"relation": "requirement_prefix", "delta_text": requirement_raw}
    return {"relation": "distinct", "delta_text": requirement_raw}


def _build_delta_text(requirement_text: str, heading_text: str, section_code: str) -> str:
    req = str(requirement_text or "").replace("\r\n", "\n").strip()
    if not req:
        return ""
    heading = str(heading_text or "").strip()
    stripped = _strip_heading_prefix_from_requirement(
        text=req,
        section_code=section_code,
        heading_title=heading,
    ).strip()
    if stripped and stripped != req:
        return stripped

    req_no_code = _remove_section_code_prefix(req, section_code).strip()
    heading_no_code = _remove_section_code_prefix(heading, section_code).strip()
    if req_no_code and heading_no_code and req_no_code.startswith(heading_no_code):
        tail = req_no_code[len(heading_no_code) :].lstrip(" ：:，,;；。.")
        if tail:
            return tail
    return req


def _remove_section_code_prefix(text: str, section_code: str) -> str:
    value = str(text or "").strip()
    code = str(section_code or "").strip()
    if not value or not code:
        return value
    pattern = rf"^\s*{re.escape(code)}\s*[\.\)\]）】、:：-]*\s*"
    return re.sub(pattern, "", value, count=1).strip()


def _normalize_compare_text(text: str, section_code: str) -> str:
    value = _remove_section_code_prefix(text, section_code)
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("：", ":").replace("，", ",").replace("；", ";")
    value = value.replace("。", ".").replace("、", ",")
    value = value.lower().strip()
    value = re.sub(r"\s+", "", value)
    value = re.sub(r"[\(\)\[\]{}<>\"'`]+", "", value)
    return value


def _strip_heading_prefix_from_requirement(text: str, section_code: str, heading_title: str) -> str:
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return ""

    lines = [line.strip() for line in raw.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        return ""

    first = lines[0]
    remainder = "\n".join(lines[1:]).strip()
    code = str(section_code or "").strip()
    title = str(heading_title or "").strip()

    # Completeness-first guard:
    # if heading itself is sentence-like, stripping the first line often removes
    # real requirement content (seen in long wrapped chapters such as 6.9).
    if _is_sentence_like_heading(title) or _is_sentence_like_heading(first):
        return raw

    def _normalize_line(value: str) -> str:
        out = str(value or "").strip()
        out = re.sub(r"\s+", "", out)
        out = out.replace("（", "(").replace("）", ")")
        out = out.replace("：", ":").replace("，", ",").replace("；", ";")
        out = out.replace("。", ".").replace("、", ",")
        return out.lower()

    first_norm = _normalize_line(first)
    title_norm = _normalize_line(title)
    code_norm = _normalize_line(code)

    if title_norm and first_norm == title_norm:
        return remainder

    if code_norm:
        first_no_code = first_norm
        if first_no_code.startswith(code_norm):
            first_no_code = first_no_code[len(code_norm) :]
        first_no_code = re.sub(r"^[\.\)\]、,:;，。．\s]+", "", first_no_code)
        if title_norm and first_no_code == title_norm:
            return remainder

        if title_norm and first_no_code.startswith(title_norm):
            suffix = first_no_code[len(title_norm) :].strip()
            if not suffix or re.fullmatch(r"[\.,;:，。、；：\(\)\[\]\-_/]*", suffix):
                return remainder

    return raw


def _is_sentence_like_heading(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if len(text) >= 18 and re.search(r"[，。；;:：]", text):
        return True
    if len(text) >= 34 and re.search(r"(?:应|需|必须|shall|must|不得|不能|要求)", text, re.I):
        return True
    return False


def _looks_like_parent_heading_spill(text: str, section_code: str) -> bool:
    raw = str(text or "").strip()
    if not raw:
        return False
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return False
    if len(lines) > 2:
        return False
    compact = " ".join(lines)
    if len(compact) > 120:
        return False
    if not re.search(r"(?:GB/?T|QC/T|Q/JQ|ISO|SAE|USCAR)\s*[\d\.]", compact, re.I):
        if not re.search(r"^\s*\d{4,6}(?:\.\d+){1,3}\b", compact):
            return False
    if re.search(r"(?:应|需|必须|shall|must|不得|should)", compact, re.I):
        return False
    # Parent chapter headings should not absorb isolated standard-id lines.
    return bool(section_code)
