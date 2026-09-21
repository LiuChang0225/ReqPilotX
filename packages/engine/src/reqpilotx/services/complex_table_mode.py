from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any, Callable


_GENERIC_COMPLEX_TABLE_CUES = (
    "对应gb",
    "要求解读",
    "cybersecurity activity",
    "生命周期",
    "矩阵",
    "网络安全活动",
)

_GENERIC_COMPLEX_TABLE_SECTION_REF_PATTERN = re.compile(r"\b\d+(?:\.\d+){1,3}\b")


@dataclass(frozen=True)
class ComplexTableGroup:
    group_id: str
    label: str
    sort_order: int
    page_start: int
    page_end: int
    section_code: str
    section_title: str
    start_label: str = ""
    next_label_hint: str = ""
    snapshot_mode: str = "anchor_window"


@dataclass(frozen=True)
class ComplexTableZone:
    zone_id: str
    family: str
    page_start: int
    page_end: int
    review_title: str
    groups: list[ComplexTableGroup] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    source_only: bool = True
    summary_review_only: bool = True


def detect_complex_table_zones(
    *,
    segments: list[dict[str, Any]],
    source_file_path: str = "",
) -> list[dict[str, Any]]:
    # ``source_file_path`` remains in the compatibility signature, but a file
    # name is never structure evidence.  Detection is deliberately limited to
    # source-bound, non-destructive generic metadata.
    _ = source_file_path
    page_text = _collect_page_text(segments)
    zones = _detect_generic_complex_table_zones(
        page_text,
        reserved_page_ranges=[],
    )

    return [asdict(zone) for zone in sorted(zones, key=lambda item: (item.page_start, item.page_end, item.family))]


def rewrite_preview_rows_for_complex_tables(
    rows: list[dict[str, Any]],
    *,
    zones: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not rows or not zones:
        return [dict(row) for row in rows]

    copied_rows = [dict(row) for row in rows]
    rewrite_plans = _collect_zone_rewrite_plans(
        copied_rows,
        zones,
        matcher=_row_matches_zone_collapse,
        signature_builder=_row_shadow_signature,
    )
    if not rewrite_plans:
        return copied_rows

    output: list[dict[str, Any]] = []
    inserted_zone_ids: set[str] = set()
    matched_index_to_zone_id = _rewrite_match_index_map(rewrite_plans)
    for index, row in enumerate(copied_rows):
        zone_id = matched_index_to_zone_id.get(index)
        if not zone_id:
            output.append(row)
            continue
        if zone_id in inserted_zone_ids:
            continue
        inserted_zone_ids.add(zone_id)
        plan = rewrite_plans.get(zone_id) or {}
        zone = plan.get("zone")
        if isinstance(zone, dict):
            output.extend(
                _build_zone_preview_rows(
                    zone,
                    shadow_signatures=plan.get("signatures"),
                )
            )
    return output


def rewrite_preview_sections_for_complex_tables(
    sections: list[dict[str, Any]],
    *,
    zones: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not sections or not zones:
        return [dict(row) for row in sections]

    copied_sections = [dict(row) for row in sections]
    rewrite_plans = _collect_zone_rewrite_plans(
        copied_sections,
        zones,
        matcher=_section_matches_zone_collapse,
    )
    if not rewrite_plans:
        return copied_sections

    output: list[dict[str, Any]] = []
    matched_index_to_zone_id = _rewrite_match_index_map(rewrite_plans)
    emitted_zone_ids: set[str] = set()
    for index, row in enumerate(copied_sections):
        zone_id = matched_index_to_zone_id.get(index)
        if not zone_id:
            output.append(row)
            continue
        if zone_id in emitted_zone_ids:
            continue
        plan = rewrite_plans.get(zone_id) or {}
        zone = plan.get("zone")
        if not isinstance(zone, dict):
            output.append(row)
            continue
        output.extend(_build_zone_section_rows(zone))
        emitted_zone_ids.add(zone_id)
    return output


def is_complex_table_row(row: dict[str, Any] | None, *, include_summary: bool = True) -> bool:
    if not isinstance(row, dict):
        return False
    if not bool(row.get("complex_table_mode")):
        return False
    role = str(row.get("complex_role") or "").strip().lower()
    if include_summary:
        return role in {"zone_summary", "group"}
    return role == "group"


def is_complex_review_only_row(row: dict[str, Any] | None) -> bool:
    return bool(is_complex_table_row(row) and str((row or {}).get("complex_row_policy") or "").strip().lower() == "review_only")


def hydrate_complex_row_metadata(row: dict[str, Any]) -> dict[str, Any]:
    copied = dict(row)
    refs = copied.get("table_refs") if isinstance(copied.get("table_refs"), list) else []
    complex_ref = next(
        (
            item
            for item in refs
            if isinstance(item, dict) and bool(item.get("complex_table_mode"))
        ),
        None,
    )
    if bool(copied.get("complex_table_mode")):
        copied["complex_table_mode"] = True
    elif complex_ref is not None:
        copied["complex_table_mode"] = True
    else:
        copied["complex_table_mode"] = False

    if not copied["complex_table_mode"]:
        copied["complex_zone_id"] = str(copied.get("complex_zone_id") or "")
        copied["complex_family"] = str(copied.get("complex_family") or "")
        copied["complex_role"] = str(copied.get("complex_role") or "")
        copied["complex_group_label"] = str(copied.get("complex_group_label") or "")
        copied["complex_split_level"] = str(copied.get("complex_split_level") or "")
        copied["complex_warnings"] = _normalize_string_list(copied.get("complex_warnings"))
        copied["complex_row_policy"] = str(copied.get("complex_row_policy") or "")
        copied["complex_shadow_signatures"] = _normalize_string_list(copied.get("complex_shadow_signatures"))
        return copied

    if complex_ref is not None:
        copied.setdefault("complex_zone_id", complex_ref.get("complex_zone_id"))
        copied.setdefault("complex_family", complex_ref.get("complex_family"))
        copied.setdefault("complex_split_level", complex_ref.get("complex_split_level"))
        copied.setdefault("complex_group_label", complex_ref.get("complex_group_label"))
        copied.setdefault("complex_warnings", complex_ref.get("complex_warnings"))
        copied.setdefault("complex_shadow_signatures", complex_ref.get("complex_shadow_signatures"))

    copied["complex_zone_id"] = str(copied.get("complex_zone_id") or "")
    copied["complex_family"] = str(copied.get("complex_family") or "")
    copied["complex_role"] = str(copied.get("complex_role") or ("group" if complex_ref else "")).strip()
    copied["complex_group_label"] = str(
        copied.get("complex_group_label")
        or copied.get("section_title")
        or copied.get("parent_section_title")
        or ""
    ).strip()
    copied["complex_split_level"] = str(copied.get("complex_split_level") or "group").strip()
    copied["complex_warnings"] = _normalize_string_list(copied.get("complex_warnings"))
    copied["complex_row_policy"] = str(copied.get("complex_row_policy") or ("exportable_group" if copied["complex_role"] == "group" else "")).strip()
    copied["complex_shadow_signatures"] = _normalize_string_list(copied.get("complex_shadow_signatures"))
    return copied


def row_is_shadowed_by_complex_group(
    row: dict[str, Any],
    complex_rows: list[dict[str, Any]],
) -> bool:
    if not complex_rows:
        return False
    signature = _row_shadow_signature(row)
    if not signature:
        return False

    for item in complex_rows:
        if not is_complex_table_row(item, include_summary=False):
            continue
        shadow_signatures = _complex_row_shadow_signatures(item)
        if signature in shadow_signatures:
            return True
    return False


def _collect_page_text(segments: list[dict[str, Any]]) -> dict[int, str]:
    pages: dict[int, list[str]] = {}
    for segment in segments or []:
        page = _positive_int(segment.get("source_page"))
        text = str(segment.get("text") or "").strip()
        if page <= 0 or not text:
            continue
        pages.setdefault(page, []).append(text)
    return {page: "\n".join(chunks) for page, chunks in pages.items()}


def _detect_generic_complex_table_zones(
    page_text: dict[int, str],
    *,
    reserved_page_ranges: list[tuple[int, int]],
) -> list[ComplexTableZone]:
    if not page_text:
        return []

    candidate_pages = [
        page
        for page, text in sorted(page_text.items())
        if not _page_in_reserved_ranges(page, reserved_page_ranges) and _looks_like_generic_complex_table_page(text)
    ]
    if not candidate_pages:
        return []

    grouped_pages: list[list[int]] = []
    current_group: list[int] = []
    for page in candidate_pages:
        if current_group and page != current_group[-1] + 1:
            if len(current_group) >= 2:
                grouped_pages.append(current_group)
            current_group = []
        current_group.append(page)
    if len(current_group) >= 2:
        grouped_pages.append(current_group)

    zones: list[ComplexTableZone] = []
    for pages in grouped_pages:
        page_start = pages[0]
        page_end = pages[-1]
        zones.append(
            ComplexTableZone(
                zone_id=f"cx-generic-complex-table-p{page_start}-p{page_end}",
                family="generic_complex_table",
                page_start=page_start,
                page_end=page_end,
                review_title=f"复杂跨页表候选（第 {page_start}-{page_end} 页）",
                groups=[],
                warnings=["复杂表候选，仅输出 metadata；V1 不做 destructive collapse。"],
                source_only=True,
                summary_review_only=True,
            )
        )
    return zones


def _page_in_reserved_ranges(page: int, reserved_page_ranges: list[tuple[int, int]]) -> bool:
    return any(start <= page <= end for start, end in reserved_page_ranges)


def _looks_like_generic_complex_table_page(text: str) -> bool:
    compact = str(text or "").strip()
    if not compact:
        return False
    lowered = compact.lower()
    pipe_count = compact.count("|")
    section_ref_count = len(_GENERIC_COMPLEX_TABLE_SECTION_REF_PATTERN.findall(compact))
    cue_hits = sum(1 for cue in _GENERIC_COMPLEX_TABLE_CUES if cue in lowered)
    return (
        (pipe_count >= 8 and section_ref_count >= 2)
        or (pipe_count >= 6 and cue_hits >= 1 and section_ref_count >= 2)
        or (cue_hits >= 2 and section_ref_count >= 3)
    )


def _find_zone_for_row(row: dict[str, Any], zones: list[dict[str, Any]]) -> dict[str, Any] | None:
    page_start = _positive_int(row.get("source_page"))
    page_end = _positive_int(row.get("source_page_end")) or page_start
    if page_start <= 0:
        return None
    for zone in zones:
        if _range_within_zone(page_start, page_end, zone):
            return zone
    return None


def _find_zone_for_section(row: dict[str, Any], zones: list[dict[str, Any]]) -> dict[str, Any] | None:
    page_start = _positive_int(row.get("source_page_start"))
    page_end = _positive_int(row.get("source_page_end")) or page_start
    if page_start <= 0:
        return None
    for zone in zones:
        if _range_within_zone(page_start, page_end, zone):
            return zone
    return None


def _range_within_zone(page_start: int, page_end: int, zone: dict[str, Any]) -> bool:
    zone_start = _positive_int(zone.get("page_start"))
    zone_end = _positive_int(zone.get("page_end")) or zone_start
    return zone_start > 0 and page_start >= zone_start and page_end <= zone_end


def _zone_supports_collapse(zone: dict[str, Any]) -> bool:
    # Automatic destructive collapse has been retired.  Historical rows may
    # still carry compatibility metadata, but no detected zone can authorize
    # replacing source-derived rows with synthetic requirements.
    _ = zone
    return False


def _collect_zone_rewrite_plans(
    items: list[dict[str, Any]],
    zones: list[dict[str, Any]],
    *,
    matcher: Callable[[dict[str, Any], dict[str, Any]], bool],
    signature_builder: Callable[[dict[str, Any]], str] | None = None,
) -> dict[str, dict[str, Any]]:
    plans: dict[str, dict[str, Any]] = {}
    claimed_indexes: set[int] = set()
    for zone in sorted(zones, key=lambda item: (_positive_int(item.get("page_start")), _positive_int(item.get("page_end")))):
        if not _zone_supports_collapse(zone):
            continue
        zone_id = str(zone.get("zone_id") or "").strip()
        if not zone_id:
            continue
        matched_indexes: list[int] = []
        signatures: list[str] = []
        for index, item in enumerate(items):
            if index in claimed_indexes:
                continue
            if not matcher(item, zone):
                continue
            matched_indexes.append(index)
            claimed_indexes.add(index)
            if signature_builder is not None:
                signature = signature_builder(item)
                if signature and signature not in signatures:
                    signatures.append(signature)
        if matched_indexes:
            plans[zone_id] = {
                "zone": zone,
                "indexes": matched_indexes,
                "signatures": signatures,
            }
    return plans


def _rewrite_match_index_map(plans: dict[str, dict[str, Any]]) -> dict[int, str]:
    matched_indexes: dict[int, str] = {}
    for zone_id, plan in plans.items():
        for index in plan.get("indexes") or []:
            matched_indexes[int(index)] = zone_id
    return matched_indexes


def _build_zone_preview_rows(zone: dict[str, Any], *, shadow_signatures: list[str] | None = None) -> list[dict[str, Any]]:
    return [_build_zone_primary_row(zone, shadow_signatures=shadow_signatures)]


def _build_zone_section_rows(zone: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scope = "CX"
    section_title = str(zone.get("review_title") or "").strip() or "复杂跨页表"
    section_key = f"{scope}:{_slug(str(zone.get('zone_id') or 'zone'))}:zone"
    rows.append(
        {
            "appendix_scope": scope,
            "section_code": "",
            "section_key": section_key,
            "section_instance_key": section_key,
            "section_title": section_title,
            "section_level": 1,
            "parent_section_code": "",
            "has_children": False,
            "is_leaf": True,
            "source_page_start": _positive_int(zone.get("page_start")),
            "source_page_end": _positive_int(zone.get("page_end")) or _positive_int(zone.get("page_start")),
            "source_line_index_start": 0,
            "source_line_index_end": 0,
            "line_count": 0,
            "section_char_count": len(section_title),
            "requirement_like": True,
            "is_appendix_marker": False,
            "requirement_count": 1,
            "total_chars": len(section_title),
            "text_preview": section_title,
            "first_req_order": 0,
            "outline_order": None,
            "complex_table_mode": True,
            "complex_zone_id": str(zone.get("zone_id") or ""),
            "complex_family": str(zone.get("family") or ""),
        }
    )
    return rows


def _build_zone_primary_row(zone: dict[str, Any], *, shadow_signatures: list[str] | None = None) -> dict[str, Any]:
    review_title = str(zone.get("review_title") or "").strip() or "复杂跨页表"
    zone_id = str(zone.get("zone_id") or "")
    page_start = _positive_int(zone.get("page_start"))
    page_end = _positive_int(zone.get("page_end")) or page_start
    text = f"{review_title}\n复杂跨页表默认保留整表原图，不再自动拆分分组，请结合原文截图人工处理。"
    section_key = f"CX:{_slug(zone_id)}:zone"
    return {
        "entry_id": f"REQ-CX-{_slug(zone_id)}",
        "req_code": f"REQ-CX-{_slug(zone_id)}",
        "entry_kind": "REQUIREMENT",
        "appendix_scope": "CX",
        "section_code": "",
        "section_key": section_key,
        "section_instance_key": section_key,
        "section_level": 1,
        "parent_section_code": "",
        "parent_section_title": review_title,
        "parent_section_label": review_title,
        "source_page": page_start,
        "source_page_end": page_end,
        "source_line_index": 0,
        "source_line_index_end": 0,
        "source_anchor": f"Complex Table Zone {zone_id}",
        "section_title": review_title,
        "requirement_type": "OTHER",
        "priority": "MEDIUM",
        "text_length": len(text),
        "requirement_text": text,
        "display_text": text,
        "raw_text": text,
        "heading_text": "",
        "text_relation": "distinct",
        "is_deduped": False,
        "merged_from_entry_ids": [f"REQ-CX-{_slug(zone_id)}"],
        "merged_from_req_codes": [f"REQ-CX-{_slug(zone_id)}"],
        "dedupe_version": "heading_requirement_v1",
        "is_appendix_marker": False,
        "is_synthetic_heading": False,
        "keep": True,
        "complex_table_mode": True,
        "complex_zone_id": zone_id,
        "complex_family": str(zone.get("family") or ""),
        "complex_role": "group",
        "complex_group_label": review_title,
        "complex_split_level": "zone",
        "complex_warnings": _normalize_string_list(zone.get("warnings")),
        "complex_row_policy": "exportable_group",
        "complex_snapshot_mode": "full_page_span",
        "complex_snapshot_title": review_title,
        "complex_after_label_hint": "",
        "complex_shadow_signatures": _normalize_string_list(shadow_signatures),
    }


def _build_zone_summary_row(zone: dict[str, Any], *, shadow_signatures: list[str] | None = None) -> dict[str, Any]:
    review_title = str(zone.get("review_title") or "").strip() or "复杂跨页表"
    zone_id = str(zone.get("zone_id") or "")
    page_start = _positive_int(zone.get("page_start"))
    page_end = _positive_int(zone.get("page_end")) or page_start
    text = f"{review_title}\n复杂跨页表整表总览，内部条目未自动细拆，请结合原文截图人工处理。"
    return {
        "entry_id": f"REQ-CX-{_slug(zone_id)}-SUMMARY",
        "req_code": f"REQ-CX-{_slug(zone_id)}-SUMMARY",
        "entry_kind": "REQUIREMENT",
        "appendix_scope": "CX",
        "section_code": "",
        "section_key": f"CX:{_slug(zone_id)}:summary",
        "section_instance_key": f"CX:{_slug(zone_id)}:summary",
        "section_level": 1,
        "parent_section_code": "",
        "parent_section_title": review_title,
        "parent_section_label": review_title,
        "source_page": page_start,
        "source_page_end": page_end,
        "source_line_index": 0,
        "source_line_index_end": 0,
        "source_anchor": f"Complex Table Zone {zone_id}",
        "section_title": review_title,
        "requirement_type": "OTHER",
        "priority": "MEDIUM",
        "text_length": len(text),
        "requirement_text": text,
        "display_text": text,
        "raw_text": text,
        "heading_text": "",
        "text_relation": "distinct",
        "is_deduped": False,
        "merged_from_entry_ids": [f"REQ-CX-{_slug(zone_id)}-SUMMARY"],
        "merged_from_req_codes": [],
        "dedupe_version": "heading_requirement_v1",
        "is_appendix_marker": False,
        "is_synthetic_heading": False,
        "keep": False,
        "complex_table_mode": True,
        "complex_zone_id": zone_id,
        "complex_family": str(zone.get("family") or ""),
        "complex_role": "zone_summary",
        "complex_group_label": "",
        "complex_split_level": "zone",
        "complex_warnings": _normalize_string_list(zone.get("warnings")),
        "complex_row_policy": "review_only",
        "complex_snapshot_mode": "full_page_span",
        "complex_snapshot_title": review_title,
        "complex_after_label_hint": "",
        "complex_shadow_signatures": _normalize_string_list(shadow_signatures),
    }


def _build_zone_group_row(
    zone: dict[str, Any],
    group: dict[str, Any],
    *,
    shadow_signatures: list[str] | None = None,
) -> dict[str, Any]:
    label = str(group.get("label") or "").strip() or str(group.get("section_title") or "")
    section_code = str(group.get("section_code") or "").strip()
    section_title = str(group.get("section_title") or label).strip()
    page_start = _positive_int(group.get("page_start"))
    page_end = _positive_int(group.get("page_end")) or page_start
    group_id = str(group.get("group_id") or "")
    text = f"{label}\n复杂跨页表分组，内部条目未自动细拆，请结合原文截图人工处理。"
    section_key = f"CX:{_slug(str(zone.get('zone_id') or 'zone'))}:{_slug(section_code or label)}"
    return {
        "entry_id": f"REQ-CX-{_slug(group_id)}",
        "req_code": f"REQ-CX-{_slug(group_id)}",
        "entry_kind": "REQUIREMENT",
        "appendix_scope": "CX",
        "section_code": section_code,
        "section_key": section_key,
        "section_instance_key": section_key,
        "section_level": 1,
        "parent_section_code": section_code,
        "parent_section_title": section_title,
        "parent_section_label": _compose_section_label(section_code, section_title),
        "source_page": page_start,
        "source_page_end": page_end,
        "source_line_index": 0,
        "source_line_index_end": 0,
        "source_anchor": f"Complex Table Group {group_id}",
        "section_title": section_title,
        "requirement_type": "OTHER",
        "priority": "MEDIUM",
        "text_length": len(text),
        "requirement_text": text,
        "display_text": text,
        "raw_text": text,
        "heading_text": "",
        "text_relation": "distinct",
        "is_deduped": False,
        "merged_from_entry_ids": [f"REQ-CX-{_slug(group_id)}"],
        "merged_from_req_codes": [f"REQ-CX-{_slug(group_id)}"],
        "dedupe_version": "heading_requirement_v1",
        "is_appendix_marker": False,
        "is_synthetic_heading": False,
        "keep": True,
        "complex_table_mode": True,
        "complex_zone_id": str(zone.get("zone_id") or ""),
        "complex_family": str(zone.get("family") or ""),
        "complex_role": "group",
        "complex_group_label": label,
        "complex_split_level": "group",
        "complex_warnings": _normalize_string_list(zone.get("warnings")),
        "complex_row_policy": "exportable_group",
        "complex_snapshot_mode": str(group.get("snapshot_mode") or "anchor_window"),
        "complex_snapshot_title": str(group.get("start_label") or label),
        "complex_after_label_hint": str(group.get("next_label_hint") or ""),
        "complex_shadow_signatures": _normalize_string_list(shadow_signatures),
    }


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


def _positive_int(value: Any) -> int:
    try:
        token = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return token if token > 0 else 0


def _slug(value: str) -> str:
    token = re.sub(r"[^0-9A-Za-z]+", "-", str(value or "").strip()).strip("-").lower()
    return token or "cx"


def _row_matches_zone_collapse(row: dict[str, Any], zone: dict[str, Any]) -> bool:
    _ = row, zone
    return False


def _section_matches_zone_collapse(section: dict[str, Any], zone: dict[str, Any]) -> bool:
    _ = section, zone
    return False


def _row_shadow_signature(row: dict[str, Any]) -> str:
    page_start = _positive_int(row.get("source_page"))
    page_end = _positive_int(row.get("source_page_end")) or page_start
    if page_start <= 0:
        return ""
    entry_kind = str(row.get("entry_kind") or "").strip().upper()
    section_code = str(row.get("section_code") or "").strip()
    source_anchor = _normalize_signature_text(row.get("source_anchor"))
    text = _normalize_signature_text(
        row.get("raw_text")
        or row.get("display_text")
        or row.get("requirement_text")
        or row.get("heading_text")
        or row.get("section_title")
        or ""
    )
    return "|".join(
        [
            entry_kind,
            str(page_start),
            str(page_end),
            section_code,
            source_anchor[:120],
            text[:240],
        ]
    )


def _normalize_signature_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _complex_row_shadow_signatures(row: dict[str, Any]) -> set[str]:
    signatures = _normalize_string_list(row.get("complex_shadow_signatures"))
    refs = row.get("table_refs") if isinstance(row.get("table_refs"), list) else []
    for ref in refs:
        if not isinstance(ref, dict) or not bool(ref.get("complex_table_mode")):
            continue
        signatures.extend(_normalize_string_list(ref.get("complex_shadow_signatures")))
    return set(_normalize_string_list(signatures))


def _compose_section_label(section_code: str, section_title: str) -> str:
    code = str(section_code or "").strip()
    title = str(section_title or "").strip()
    if not code:
        return title
    if not title:
        return code
    normalized_code = re.sub(r"\s+", " ", code)
    normalized_title = re.sub(r"\s+", " ", title)
    if normalized_title == normalized_code or normalized_title.startswith(f"{normalized_code} "):
        return normalized_title
    return f"{normalized_code} {normalized_title}"
