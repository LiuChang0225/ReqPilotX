from __future__ import annotations

from collections import defaultdict
from contextlib import nullcontext
import hashlib
import re
from pathlib import Path
from statistics import mean
from typing import Any

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    fitz = None

from app.runtime_paths import resolve_storage_dir
from app.services.appendix_scopes import (
    APPENDIX_EVIDENCE_EXPLICIT,
    APPENDIX_EVIDENCE_HEADING,
    APPENDIX_EVIDENCE_TABLE_HEADER,
    APPENDIX_EVIDENCE_TOC,
    MAIN_SCOPE_ID,
    MAIN_SCOPE_TITLE,
    TEST_OUTLINE_INFERRED_GENERIC_TITLE,
    infer_generic_test_outline_title,
    is_test_outline_scope_value,
    normalize_scope_id,
    normalize_scope_text,
    normalize_test_outline_scope,
    scope_evidence_rank,
    scope_sort_key,
    test_outline_scope_identity,
    TEST_OUTLINE_SCOPE_ACCUMULATOR,
    TEST_OUTLINE_SCOPE_ELECTRIC,
    TEST_OUTLINE_SCOPE_FRONT_DAMPER,
    TEST_OUTLINE_SCOPE_GROUPS,
    TEST_OUTLINE_SCOPE_REAR_DAMPER,
    TEST_OUTLINE_SCOPE_VEHICLE_WIRING,
)

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
from app.services.table_assets import (
    _build_context_anchor_fragments,
    _build_ai_projection,
    _build_table_id,
    _best_context_anchor_in_page,
    _collect_anchor_window_content_bboxes,
    _compose_vertical_images,
    _load_pdf_page_line_records,
    _looks_like_markdown_table_row,
    _normalize_table_ref,
    _parse_markdown_cells,
    _render_pdf_crop_plan,
    _split_text_blocks,
    _snapshot_file_is_usable,
    _summarize_pdf_crop_plan_features,
    _write_full_page_span_source_snapshot,
    _write_png_atomically,
    enrich_entries_with_table_assets,
    render_table_png,
)


SECTION_ANCHOR_RE = re.compile(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})(?:\s+([^|]+))?", re.I)
CLAUSE_ANCHOR_RE = re.compile(r"Clause\s+([0-9]+(?:\.[0-9]+){0,8})\s*[）)]?\s*([^|]+)?", re.I)
TITLE_ANCHOR_RE = re.compile(r"(?:Heading|Title)\s+([^|]+)", re.I)
APPENDIX_SCOPE_RE = re.compile(r"Appendix\s+([^|]+?)(?=\s*\|\s*(?:Section|Heading|Title|Clause)\b|\s*$)", re.I)
APPENDIX_MARKER_LINE_RE = re.compile(
    r"^\s*(?:附件|附录|appendix|annex|attachment)\s*([A-Za-zＡ-Ｚａ-ｚ])\b.*$",
    re.I,
)
LOCAL_CLAUSE_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\s*[）)]\s*(.+)$")
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
    (re.compile(r"主动前减振器试验大纲", re.I), TEST_OUTLINE_SCOPE_FRONT_DAMPER, TEST_OUTLINE_SCOPE_FRONT_DAMPER),
    (re.compile(r"主动后减振器试验大纲", re.I), TEST_OUTLINE_SCOPE_REAR_DAMPER, TEST_OUTLINE_SCOPE_REAR_DAMPER),
    (re.compile(r"蓄能器试验大纲", re.I), TEST_OUTLINE_SCOPE_ACCUMULATOR, TEST_OUTLINE_SCOPE_ACCUMULATOR),
    (re.compile(r"整车用电器与线束匹配评估表", re.I), TEST_OUTLINE_SCOPE_VEHICLE_WIRING, TEST_OUTLINE_SCOPE_VEHICLE_WIRING),
    (re.compile(r"测试分组\s*/\s*test groups", re.I), TEST_OUTLINE_SCOPE_GROUPS, TEST_OUTLINE_SCOPE_GROUPS),
    (re.compile(r"用电器试验大纲", re.I), TEST_OUTLINE_SCOPE_ELECTRIC, TEST_OUTLINE_SCOPE_ELECTRIC),
]
VISUAL_SNAPSHOT_CUE_RE = re.compile(r"(?:如下图|图例|原理图|框图|流程图|示意图|效果图|产品效果图)", re.I)
EXPLICIT_VISUAL_TITLE_LINE_RE = re.compile(r"^\s*((?:表|图)\s*\d+[^\n]*)$")
EXPLICIT_CLAUSE_LINE_RE = re.compile(r"^\s*([0-9]+|[A-Za-z])\s*[)）]\s*(.+)$")
MULTI_VISUAL_REFERENCE_TITLE_RE = re.compile(r"^(?:表|图)\s*\d+\s*[、,，/]\s*(?:表|图)?\s*\d+")
FORMAL_SECTION_HEADING_LINE_RE = re.compile(r"^\s*\d+(?:\.\d+){0,5}\s+.+$")
_SECTION_WINDOW_SNAPSHOT_SIGNATURE_VERSION = "section-window-v2"
_SIGNIFICANT_VISUAL_MIN_AREA = 3000.0
_SIGNIFICANT_VISUAL_MIN_WIDTH = 100.0
_SIGNIFICANT_VISUAL_MIN_HEIGHT = 28.0
_LAYOUT_TABLE_MIN_LABEL_BLOCKS = 3
_LAYOUT_TABLE_MIN_BODY_BLOCKS = 6
_LAYOUT_TABLE_MIN_PAGE_SPAN = 2
_LAYOUT_TABLE_MAX_TEXT_CHARS = 1800
_LAYOUT_TABLE_MAX_TEXT_LINES = 28
_LAYOUT_TABLE_MAX_LABEL_CHARS = 20
_LAYOUT_TABLE_LEFT_COLUMN_MAX_OFFSET = 96.0
_LAYOUT_TABLE_PRELABEL_GAP = 24.0
_VISUAL_BAND_MAX_GAP = 28.0
_PROTOTYPE_DIR = Path(__file__).resolve().parents[2]


def _ensure_snapshot_root_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    probe = path / f".write_probe_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:8]}"
    try:
        probe.write_bytes(b"")
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        pass
    return True


def _default_table_snapshot_root(file_path: str) -> Path:
    source_path = Path(str(file_path or "")).expanduser()
    if str(file_path or "").strip():
        preferred = source_path.parent / "table_snapshots"
        if _ensure_snapshot_root_writable(preferred):
            return preferred

    storage_root = resolve_storage_dir(_PROTOTYPE_DIR)
    doc_token = hashlib.sha1(str(source_path).encode("utf-8")).hexdigest()[:12]
    fallback = storage_root / "table_snapshots" / "adhoc" / doc_token
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _raw_markdown_table_body_row_count(raw_text: str) -> int:
    lines = [str(line or "").strip() for line in str(raw_text or "").splitlines() if str(line or "").strip()]
    markdown_rows = [line for line in lines if line.startswith("|") and line.count("|") >= 2]
    if len(markdown_rows) <= 1:
        return 0
    return max(0, len(markdown_rows) - 1)


def _content_blocks_with_single_table(content_blocks: list[dict[str, Any]], table_id: str) -> list[dict[str, Any]]:
    paragraphs = [
        {"kind": "paragraph", "text": str(block.get("text") or "").strip()}
        for block in content_blocks or []
        if str(block.get("kind") or "").strip().lower() == "paragraph" and str(block.get("text") or "").strip()
    ]
    if not paragraphs:
        return [{"kind": "table", "table_id": table_id}]
    return [paragraphs[0], {"kind": "table", "table_id": table_id}, *paragraphs[1:]]


def _snapshot_fallback_text_lines(row: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for block in row.get("content_blocks") or []:
        if str(block.get("kind") or "").strip().lower() != "paragraph":
            continue
        for part in str(block.get("text") or "").splitlines():
            cleaned = str(part or "").strip()
            if cleaned:
                lines.append(cleaned)
    if not lines:
        text = str(row.get("display_text") or row.get("raw_text") or row.get("section_title") or "").strip()
        lines = [str(part or "").strip() for part in text.splitlines() if str(part or "").strip()]
    deduped: list[str] = []
    seen: set[str] = set()
    for line in lines:
        token = re.sub(r"\s+", " ", line).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(line)
    return deduped


def _compact_snapshot_fallback_lines(row: dict[str, Any], *, suffix: str) -> list[str]:
    lines = _snapshot_fallback_text_lines(row)
    if not lines:
        return []
    raw_text = str(row.get("raw_text") or "")
    if suffix in {"table_fallback", "layout_table_fallback"}:
        return lines[:1]
    if suffix == "visual_override" and _raw_markdown_table_body_row_count(raw_text) > 0:
        return lines[:1]
    if suffix == "visual_fallback":
        if len(lines) <= 3:
            return lines
        cue_lines = [line for line in lines[1:] if VISUAL_SNAPSHOT_CUE_RE.search(line)]
        if cue_lines:
            return [lines[0], *cue_lines[:3]]
        return lines[:2]
    return lines


def _snapshot_fallback_title(row: dict[str, Any], *, suffix: str) -> str:
    compact_lines = _compact_snapshot_fallback_lines(row, suffix=suffix)
    if compact_lines:
        return "\n".join(compact_lines)
    title = str(row.get("section_label") or row.get("section_code") or row.get("section_title") or "").strip()
    return title or str(row.get("entry_id") or "视觉快照").strip()


def _content_blocks_for_snapshot_fallback(
    row: dict[str, Any],
    table_id: str,
    *,
    suffix: str,
) -> list[dict[str, Any]]:
    compact_lines = _compact_snapshot_fallback_lines(row, suffix=suffix)
    if not compact_lines:
        return [{"kind": "table", "table_id": table_id}]
    return [
        *({"kind": "paragraph", "text": line} for line in compact_lines),
        {"kind": "table", "table_id": table_id},
    ]


def _normalize_table_header_token(value: str) -> str:
    token = re.sub(r"\s+", " ", str(value or "").strip()).strip("：:;/ ")
    return token.lower()


def _normalize_preview_title_token(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").strip()).strip("：:").lower()


def _is_cross_visual_reference_title(value: str) -> bool:
    return bool(MULTI_VISUAL_REFERENCE_TITLE_RE.match(str(value or "").strip()))


def _looks_like_formal_section_heading_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value or EXPLICIT_VISUAL_TITLE_LINE_RE.match(value):
        return False
    return bool(FORMAL_SECTION_HEADING_LINE_RE.match(value))


def _looks_like_table_note_marker_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value in {"注", "Note", "NOTE"}:
        return True
    return bool(re.match(r"^(?:注|Note|NOTE)\s*\d*[：:]?$", value))


def _is_running_header_or_footer_line(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.fullmatch(r"\d{1,3}", value):
        return True
    return value.startswith(("Q/SK ", "Q-SK ", "Q／SK ", "Q／SKJ", "Q-SKJ", "Q/SKJ"))


def _looks_like_table_explanatory_paragraph_line(
    text: str,
    *,
    bbox: tuple[float, float, float, float],
    page_width: float,
) -> bool:
    value = str(text or "").strip()
    if not value or len(value) < 20:
        return False
    if value.startswith(("注", "Note", "NOTE")):
        return False
    if re.match(r"^[0-9A-Za-zＡ-Ｚａ-ｚ]+[：:]", value):
        return False
    if _looks_like_formal_section_heading_line(value) or EXPLICIT_VISUAL_TITLE_LINE_RE.match(value):
        return False
    width = max(0.0, float(bbox[2]) - float(bbox[0]))
    return width >= max(240.0, page_width * 0.62)


def _find_explicit_visual_cutoff_y(
    page_specs: list[dict[str, Any]],
    *,
    kind: str,
    page_width: float,
    start_line_index: int,
) -> float | None:
    data_lines_seen = 0
    for spec in page_specs[start_line_index:]:
        text = str(spec.get("text") or "").strip()
        bbox = tuple(spec.get("bbox") or ())
        if len(bbox) < 4 or not text:
            continue
        if _looks_like_formal_section_heading_line(text):
            return max(0.0, float(bbox[1]) - 8.0)
        if kind != "table":
            continue
        if _looks_like_table_explanatory_paragraph_line(
            text,
            bbox=tuple(float(value) for value in bbox[:4]),
            page_width=page_width,
        ) and data_lines_seen >= 4:
            return max(0.0, float(bbox[1]) - 8.0)
        data_lines_seen += 1
    return None


def _page_segment_has_non_title_content(
    page_specs: list[dict[str, Any]],
    *,
    title_token: str,
) -> bool:
    for spec in page_specs:
        text = str(spec.get("text") or "").strip()
        if not text:
            continue
        token = _normalize_preview_title_token(text)
        if not token or token == title_token:
            continue
        if _looks_like_formal_section_heading_line(text):
            continue
        if _is_running_header_or_footer_line(text):
            continue
        return True
    return False


def _find_last_meaningful_spec_before_index(
    page_specs: list[dict[str, Any]],
    *,
    end_index: int,
    title_token: str,
) -> dict[str, Any] | None:
    for idx in range(min(end_index, len(page_specs)) - 1, -1, -1):
        spec = page_specs[idx]
        text = str(spec.get("text") or "").strip()
        if not text:
            continue
        token = _normalize_preview_title_token(text)
        if not token or token == title_token:
            continue
        if _is_running_header_or_footer_line(text):
            continue
        if _looks_like_formal_section_heading_line(text):
            continue
        if EXPLICIT_VISUAL_TITLE_LINE_RE.match(text):
            continue
        return spec
    return None


def _score_explicit_visual_candidate(
    pdf_document,
    *,
    page_index: int,
    bbox: tuple[float, float, float, float],
) -> tuple[int, float]:
    if bbox[3] <= bbox[1] + 16.0 or bbox[2] <= bbox[0] + 16.0:
        return (0, 0.0)
    summary = _summarize_section_window_visual_features(pdf_document, [(page_index, bbox)])
    return (
        int(summary.get("significant_visual_count") or 0),
        float(bbox[3]) - float(bbox[1]),
    )


def _collapse_preview_table_render_mode(refs: list[dict[str, Any]]) -> str:
    normalized = [
        str(ref.get("slice_render_mode") or ref.get("render_mode") or "").strip().lower()
        for ref in refs or []
        if isinstance(ref, dict)
    ]
    if any(mode == "structured" for mode in normalized):
        return "structured"
    if any(mode == "snapshot_only" for mode in normalized):
        return "snapshot_only"
    return ""


def _is_suspicious_structured_table_ref(ref: dict[str, Any]) -> bool:
    if str(ref.get("slice_render_mode") or "").strip().lower() != "structured":
        return False
    headers = [str(cell or "").strip() for cell in (ref.get("inherited_headers") or []) if str(cell or "").strip()]
    if len(headers) < 3:
        return False
    placeholder_count = sum(
        1
        for cell in headers
        if cell in {"/", "-", "--"} or re.fullmatch(r"col\d+", cell, re.I)
    )
    colon_like_count = sum(1 for cell in headers if cell.endswith(("：", ":")))
    normalized = [_normalize_table_header_token(cell) for cell in headers if _normalize_table_header_token(cell)]
    duplicate_count = len(normalized) - len(set(normalized))
    if len(headers) >= 4 and placeholder_count + colon_like_count >= len(headers) - 1:
        return True
    if placeholder_count >= 1 and colon_like_count >= 2:
        return True
    if placeholder_count >= 1 and duplicate_count >= 1:
        return True
    return False


def _looks_like_synthetic_local_clause_header_ref(ref: dict[str, Any]) -> bool:
    if str(ref.get("slice_render_mode") or "").strip().lower() != "structured":
        return False
    headers = [str(cell or "").strip() for cell in (ref.get("inherited_headers") or []) if str(cell or "").strip()]
    if len(headers) != 2:
        return False
    title = _normalize_preview_title_token(ref.get("table_title") or "")
    if not title or _normalize_preview_title_token(headers[0]) != title:
        return False
    if _normalize_table_header_token(headers[1]) not in {"说明", "内容", "description"}:
        return False
    rows = [
        [str(cell or "").strip() for cell in row]
        for row in (ref.get("slice_rows") or [])
        if isinstance(row, list)
    ]
    return _looks_like_headerless_key_value_rows(rows)


def _insert_missing_table_title_block(
    content_blocks: list[dict[str, Any]],
    *,
    table_id: str,
    table_title: str,
) -> list[dict[str, Any]]:
    if not table_id or not table_title:
        return list(content_blocks or [])
    updated: list[dict[str, Any]] = []
    inserted = False
    for block in content_blocks or []:
        kind = str(block.get("kind") or "").strip().lower()
        block_table_id = str(block.get("table_id") or "").strip()
        if kind == "table" and block_table_id == table_id:
            previous = updated[-1] if updated else None
            previous_kind = str(previous.get("kind") or "").strip().lower() if isinstance(previous, dict) else ""
            previous_text = str(previous.get("text") or "").strip() if isinstance(previous, dict) else ""
            if previous_kind != "paragraph" or previous_text != table_title:
                updated.append({"kind": "paragraph", "text": table_title})
                inserted = True
        updated.append(block)
    return updated if inserted else list(content_blocks or [])


def _strip_synthetic_local_clause_headers(row: dict[str, Any]) -> dict[str, Any]:
    refs = list(row.get("table_refs") or [])
    if not refs:
        return row
    changed = False
    updated_refs: list[dict[str, Any]] = []
    content_blocks = list(row.get("content_blocks") or [])
    for ref in refs:
        if not _looks_like_synthetic_local_clause_header_ref(ref):
            updated_refs.append(ref)
            continue
        changed = True
        normalized_ref = dict(ref)
        normalized_ref["inherited_headers"] = []
        normalized_ref["headerless"] = True
        table_id = str(normalized_ref.get("table_id") or "").strip()
        table_title = str(normalized_ref.get("table_title") or "").strip()
        if content_blocks:
            content_blocks = _insert_missing_table_title_block(
                content_blocks,
                table_id=table_id,
                table_title=table_title,
            )
        updated_refs.append(_normalize_table_ref(normalized_ref))
    if not changed:
        return row
    updated = dict(row)
    updated["table_refs"] = updated_refs
    if content_blocks:
        updated["content_blocks"] = content_blocks
    updated["table_render_mode"] = "structured"
    updated["has_table_asset"] = True
    return updated


def _filter_content_blocks_by_table_ids(
    content_blocks: list[dict[str, Any]],
    allowed_table_ids: set[str],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for block in content_blocks or []:
        if str(block.get("kind") or "").strip().lower() != "table":
            filtered.append(block)
            continue
        table_id = str(block.get("table_id") or "").strip()
        if table_id and table_id in allowed_table_ids:
            filtered.append(block)
    return filtered


def _count_nonempty_text_lines(text: str) -> int:
    return sum(1 for line in str(text or "").splitlines() if str(line or "").strip())


def _looks_like_local_clause_label(text: str) -> bool:
    value = str(text or "").strip()
    if not value or len(value) > 24:
        return False
    if not value.endswith(("：", ":")):
        return False
    if value[0].isdigit():
        return False
    return True


def _infer_pipe_table_subject_token(rows: list[list[str]]) -> str:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        if len(row) < 2:
            continue
        for cell in row[1:]:
            for token in re.findall(r"\b[A-Z]{2,}(?:_[A-Z0-9]+)+\b", str(cell or "")):
                if token.startswith("VIU_") or len(token) > 16:
                    continue
                counts[token] += 1
    if not counts:
        return ""
    token, hits = max(counts.items(), key=lambda item: (item[1], -len(item[0]), item[0]))
    return token if hits >= 2 else ""


def _looks_like_headerless_key_value_rows(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return False
    normalized_rows = [
        [str(cell or "").strip() for cell in row]
        for row in rows
        if isinstance(row, list)
    ]
    if not normalized_rows or any(len(row) != 2 for row in normalized_rows):
        return False
    first_cells = [row[0] for row in normalized_rows if row and row[0]]
    second_cells = [row[1] for row in normalized_rows if len(row) > 1]
    if len(first_cells) < len(normalized_rows) - 1:
        return False
    label_like_count = sum(
        1
        for cell in first_cells
        if cell.endswith(("：", ":")) or (len(cell) <= 12 and not re.search(r"[。；;!?！？]$", cell))
    )
    informative_value_count = sum(
        1
        for cell in second_cells
        if cell in {"/", "-", "--"} or len(cell) >= 4 or re.search(r"[A-Za-z\u4e00-\u9fff0-9]", cell)
    )
    return label_like_count >= max(2, len(first_cells) - 1) and informative_value_count >= len(normalized_rows) - 1


def _compact_layout_table_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").strip())


def _normalize_layout_table_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _looks_like_layout_table_list_item(text: str) -> bool:
    return bool(re.match(r"^(?:\d+\.|[A-Za-z]\.)", str(text or "").strip()))


def _looks_like_layout_table_body_prefill(text: str) -> bool:
    value = str(text or "").strip()
    return bool(re.match(r"^(?:\d+\.|[A-Za-z]\.)", value))


def _looks_like_layout_table_label_line(
    text: str,
    *,
    x0: float,
    clip_x0: float,
    section_heading_tokens: set[str],
) -> bool:
    compact = _compact_layout_table_text(text)
    if not compact or compact in section_heading_tokens:
        return False
    if re.fullmatch(r"\d{1,3}", compact):
        return False
    if re.fullmatch(r"[A-Za-z]\.?", compact):
        return False
    if float(x0) > float(clip_x0) + _LAYOUT_TABLE_LEFT_COLUMN_MAX_OFFSET:
        return False
    if len(compact) > _LAYOUT_TABLE_MAX_LABEL_CHARS:
        return False
    if not (compact.endswith(("：", ":")) or re.search(r"[\u4e00-\u9fff]", compact)):
        return False
    if re.search(r"[，,；;。]", compact) and not compact.endswith(("：", ":")):
        return False
    return compact.endswith(("：", ":")) or len(compact) <= 12


def _extract_layout_table_subject_and_label(text: str) -> tuple[str, str]:
    compact = _compact_layout_table_text(text)
    match = re.match(r"^(?P<subject>[A-Z]{2,}(?:_[A-Z0-9]+)+)(?P<label>.*)$", compact)
    if not match:
        return "", compact
    return str(match.group("subject") or "").strip(), str(match.group("label") or "").strip()


def _normalize_layout_table_label_text(text: str) -> str:
    value = _compact_layout_table_text(text)
    if not value:
        return ""
    if value in {"件：", "件:"}:
        return value
    value = re.sub(r"触发条：?件：?$", "触发条件：", value)
    value = re.sub(r"^[（(]+", "", value)
    if value.endswith(("：", ":")):
        return value[:-1] + "："
    return f"{value}：" if re.search(r"[\u4e00-\u9fff]", value) else value


def _append_layout_table_text_fragment(base: str, piece: str) -> str:
    left = str(base or "").strip()
    right = str(piece or "").strip()
    if not right:
        return left
    if not left:
        return right
    if right in {"、", ",", "，", "；", ";", "）", ")"}:
        return left + right
    if right.startswith(("、", ",", "，", "）", ")", "：", ":", "；", ";")):
        return left + right
    if left.endswith(("（", "(", "/", "=", "0x", "：", ":")):
        return left + right
    return f"{left} {right}"


def _collapse_layout_table_body_lines(lines: list[str]) -> str:
    normalized = [_normalize_layout_table_text(line) for line in lines if _normalize_layout_table_text(line)]
    if not normalized:
        return ""

    if any(_looks_like_layout_table_list_item(line) for line in normalized):
        items: list[str] = []
        current = ""
        for line in normalized:
            if _looks_like_layout_table_list_item(line):
                if current:
                    items.append(current)
                current = line
                continue
            current = _append_layout_table_text_fragment(current, line)
        if current:
            items.append(current)
        return "\n".join(item for item in items if item)

    value = ""
    for line in normalized:
        value = _append_layout_table_text_fragment(value, line)
    return value


def _build_layout_table_line_specs(
    *,
    pdf_document,
    source_key: str,
    plan: list[tuple[int, tuple[float, float, float, float] | None]],
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
    section_heading_tokens: set[str],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for page_index, bbox in plan:
        if bbox is None:
            continue
        clip_x0 = float(bbox[0])
        lines = _load_pdf_page_line_records(
            pdf_document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=line_detection_cache,
        )
        for line in lines:
            bbox_value = tuple(line.get("bbox") or ())
            if len(bbox_value) < 4:
                continue
            x0, y0, _x1, y1 = bbox_value
            if float(y1) < float(bbox[1]) or float(y0) > float(bbox[3]):
                continue
            text = _normalize_layout_table_text(line.get("text") or "")
            compact = _compact_layout_table_text(text)
            if not compact:
                continue
            if compact in section_heading_tokens:
                continue
            if re.fullmatch(r"\d{1,3}", compact):
                continue
            if "重庆赛力斯汽车有限公司" in text:
                continue
            specs.append(
                {
                    "page_index": page_index,
                    "bbox": bbox_value,
                    "text": text,
                    "clip_x0": clip_x0,
                }
            )
    specs.sort(key=lambda item: (int(item["page_index"]), float(item["bbox"][1]), float(item["bbox"][0])))
    return specs


def _build_section_visual_line_specs(
    *,
    pdf_document,
    source_key: str,
    plan: list[tuple[int, tuple[float, float, float, float] | None]],
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for page_index, bbox in plan:
        if bbox is None:
            continue
        lines = _load_pdf_page_line_records(
            pdf_document,
            source_key=source_key,
            page_index=page_index,
            detection_cache=line_detection_cache,
        )
        for line in lines:
            bbox_value = tuple(line.get("bbox") or ())
            if len(bbox_value) < 4:
                continue
            x0, y0, _x1, y1 = bbox_value
            if float(y1) < float(bbox[1]) or float(y0) > float(bbox[3]):
                continue
            text = _normalize_layout_table_text(line.get("text") or "")
            compact = _compact_layout_table_text(text)
            if not compact:
                continue
            if re.fullmatch(r"\d{1,3}", compact):
                continue
            if "重庆赛力斯汽车有限公司" in text:
                continue
            specs.append(
                {
                    "page_index": page_index,
                    "bbox": bbox_value,
                    "text": text,
                    "clip_x0": float(bbox[0]),
                }
            )
    specs.sort(key=lambda item: (int(item["page_index"]), float(item["bbox"][1]), float(item["bbox"][0])))
    return specs


def _recover_layout_headerless_tables_from_line_specs(
    line_specs: list[dict[str, Any]],
    *,
    section_heading_tokens: set[str],
) -> list[dict[str, Any]]:
    if not line_specs:
        return []

    label_events: list[dict[str, Any]] = []
    for idx, spec in enumerate(line_specs):
        text = str(spec.get("text") or "")
        bbox = tuple(spec.get("bbox") or ())
        if len(bbox) < 4:
            continue
        if not _looks_like_layout_table_label_line(
            text,
            x0=float(bbox[0]),
            clip_x0=float(spec.get("clip_x0") or 0.0),
            section_heading_tokens=section_heading_tokens,
        ):
            continue
        subject, label = _extract_layout_table_subject_and_label(text)
        normalized_label = _normalize_layout_table_label_text(label or text)
        if normalized_label in {"件：", "件:"} and label_events:
            label_events[-1]["label"] = _normalize_layout_table_label_text(
                str(label_events[-1].get("label") or "") + normalized_label
            )
            label_events[-1]["consumed_indexes"].add(idx)
            continue
        inherited_subject = subject or str(label_events[-1].get("subject") or "") if label_events else subject
        label_events.append(
            {
                "anchor_index": idx,
                "consumed_indexes": {idx},
                "subject": inherited_subject,
                "label": normalized_label,
            }
        )

    if not label_events:
        return []

    subject_groups: list[dict[str, Any]] = []
    current_group: dict[str, Any] | None = None
    for event in label_events:
        subject = str(event.get("subject") or "").strip()
        if subject:
            if current_group and current_group.get("subject") != subject:
                subject_groups.append(current_group)
                current_group = None
            if current_group is None:
                current_group = {"subject": subject, "events": []}
        elif current_group is None:
            continue
        assert current_group is not None
        current_group["events"].append(event)
    if current_group:
        subject_groups.append(current_group)

    recovered: list[dict[str, Any]] = []
    for group_index, group in enumerate(subject_groups):
        subject = str(group.get("subject") or "").strip()
        events = list(group.get("events") or [])
        if not subject or len(events) < 2:
            continue
        group_start = events[0]["anchor_index"]
        previous_group = subject_groups[group_index - 1] if group_index > 0 else None
        if previous_group is None:
            group_start = 0
        group_end = (
            subject_groups[group_index + 1]["events"][0]["anchor_index"]
            if group_index + 1 < len(subject_groups)
            else len(line_specs)
        )
        consumed_indexes = {
            consumed_idx
            for event in events
            for consumed_idx in set(event.get("consumed_indexes") or set())
        }
        prefills: dict[int, list[tuple[str, int]]] = defaultdict(list)
        rows: list[list[str]] = []
        row_specs: list[dict[str, Any]] = []
        for event_index, event in enumerate(events):
            anchor_index = int(event.get("anchor_index") or 0)
            next_anchor = int(events[event_index + 1]["anchor_index"]) if event_index + 1 < len(events) else group_end
            segment_indexes = list(range(group_start if event_index == 0 else anchor_index, next_anchor))
            candidate_entries = [
                (position, line_specs[position])
                for position in segment_indexes
                if position not in consumed_indexes
            ]
            if event_index + 1 < len(events) and candidate_entries:
                split_index: int | None = None
                current_specs = prefills.pop(event_index, [])
                for position in range(1, len(candidate_entries)):
                    previous = candidate_entries[position - 1][1]
                    current = candidate_entries[position][1]
                    previous_y = float(previous.get("bbox", (0.0, 0.0, 0.0, 0.0))[1])
                    current_y = float(current.get("bbox", (0.0, 0.0, 0.0, 0.0))[1])
                    if int(previous.get("page_index") or 0) != int(current.get("page_index") or 0):
                        continue
                    gap = current_y - previous_y
                    if gap >= _LAYOUT_TABLE_PRELABEL_GAP and _looks_like_layout_table_body_prefill(str(current.get("text") or "")):
                        split_index = position
                if split_index is not None:
                    prefills[event_index + 1].extend(
                        (str(item.get("text") or ""), candidate_position)
                        for candidate_position, item in candidate_entries[split_index:]
                    )
                    candidate_entries = candidate_entries[:split_index]
                body_specs = current_specs + [
                    (str(item.get("text") or ""), candidate_position) for candidate_position, item in candidate_entries
                ]
            else:
                body_specs = prefills.pop(event_index, []) + [
                    (str(item.get("text") or ""), candidate_position) for candidate_position, item in candidate_entries
                ]

            label_text = _normalize_layout_table_label_text(event.get("label") or "")
            body_text = _collapse_layout_table_body_lines([text for text, _ in body_specs])
            if not label_text or not body_text:
                continue
            rows.append([label_text, body_text])
            row_specs.append(
                {
                    "values": [label_text, body_text],
                    "used_indexes": sorted(
                        {
                            *set(event.get("consumed_indexes") or set()),
                            *(position for _text, position in body_specs),
                        }
                    ),
                }
            )
        if len(rows) < 2:
            continue
        recovered.append(
            {
                "title": f"{subject}:",
                "rows": rows,
                "row_specs": row_specs,
            }
        )
    return recovered


def _extract_section_level_tail_notes_from_layout_groups(
    groups: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    if len(groups) < 2:
        return groups, []
    subjects = [
        _normalize_preview_title_token(group.get("title") or "")
        for group in groups
        if _normalize_preview_title_token(group.get("title") or "")
    ]
    if len(subjects) < 2:
        return groups, []
    updated_groups = [dict(group) for group in groups]
    last_group = dict(updated_groups[-1])
    rows = [list(row) for row in (last_group.get("rows") or []) if isinstance(row, list)]
    row_specs = [dict(spec) for spec in (last_group.get("row_specs") or []) if isinstance(spec, dict)]
    if not rows or not row_specs or len(rows) != len(row_specs):
        return groups, []
    tail_row = rows[-1]
    tail_spec = row_specs[-1]
    label = _normalize_layout_table_label_text(tail_row[0] if tail_row else "")
    body = str(tail_row[1] if len(tail_row) > 1 else "").strip()
    if label != "备注：" or not body:
        return groups, []
    compact_body = _normalize_preview_title_token(body)
    subject_hits = [
        subject
        for subject in subjects
        if subject and subject in compact_body
    ]
    if len(set(subject_hits)) < 2:
        return groups, []
    rows.pop()
    row_specs.pop()
    if len(rows) < 2:
        return groups, []
    last_group["rows"] = rows
    last_group["row_specs"] = row_specs
    updated_groups[-1] = last_group
    return updated_groups, [f"{label}\n{body}"]


def _build_layout_group_crop_plan(
    pdf_document,
    *,
    line_specs: list[dict[str, Any]],
    used_indexes: list[int],
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    valid_specs = [
        line_specs[position]
        for position in sorted({int(position) for position in used_indexes})
        if 0 <= int(position) < len(line_specs)
    ]
    if not valid_specs:
        return []
    by_page: dict[int, list[tuple[float, float, float, float]]] = defaultdict(list)
    for spec in valid_specs:
        bbox = tuple(spec.get("bbox") or ())
        if len(bbox) < 4:
            continue
        by_page[int(spec.get("page_index") or 0)].append(tuple(float(value) for value in bbox[:4]))
    crop_plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    for page_index in sorted(by_page):
        page = pdf_document.load_page(page_index)
        page_boxes = by_page.get(page_index) or []
        if not page_boxes:
            continue
        window_top = max(0.0, min(float(box[1]) for box in page_boxes) - 8.0)
        window_bottom = min(float(page.rect.height), max(float(box[3]) for box in page_boxes) + 8.0)
        if window_bottom <= window_top + 4.0:
            continue
        content_bboxes = _collect_anchor_window_content_bboxes(
            page,
            window_top=window_top,
            window_bottom=window_bottom,
            include_drawings=True,
        )
        crop_bbox = _expand_section_window_crop_bbox(page, content_bboxes)
        if crop_bbox is None:
            crop_bbox = (
                max(0.0, min(float(box[0]) for box in page_boxes) - 12.0),
                window_top,
                min(float(page.rect.width), max(float(box[2]) for box in page_boxes) + 12.0),
                window_bottom,
            )
        if crop_bbox[3] <= crop_bbox[1] + 16.0:
            continue
        crop_plan.append((page_index, crop_bbox))
    return crop_plan


def _extract_explicit_visual_titles_from_text(text: str) -> list[dict[str, Any]]:
    titles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = _normalize_layout_table_text(raw_line)
        if not line:
            continue
        match = EXPLICIT_VISUAL_TITLE_LINE_RE.match(line)
        if not match:
            continue
        title = str(match.group(1) or "").strip()
        if _is_cross_visual_reference_title(title):
            continue
        token = _normalize_preview_title_token(title)
        if not token or token in seen:
            continue
        seen.add(token)
        titles.append(
            {
                "title": title,
                "kind": "table" if title.startswith("表") else "figure",
                "token": token,
            }
        )
    return titles


def _score_explicit_visual_title_match(title: str, line_text: str) -> int:
    title_token = _normalize_preview_title_token(title)
    line_token = _normalize_preview_title_token(line_text)
    if not title_token or not line_token:
        return 0
    if title_token == line_token:
        return 100
    if title_token in line_token or line_token in title_token:
        return 85
    title_head = re.match(r"^(表|图)\s*(\d+)", title.strip())
    line_head = re.match(r"^(表|图)\s*(\d+)", str(line_text or "").strip())
    if title_head and line_head and title_head.groups() == line_head.groups():
        return 65
    return 0


def _match_explicit_visual_titles_to_line_specs(
    titles: list[dict[str, Any]],
    *,
    line_specs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    cursor = 0
    used_indexes: set[int] = set()
    for title in titles:
        best_index: int | None = None
        best_score = 0
        for idx in range(cursor, len(line_specs)):
            if idx in used_indexes:
                continue
            score = _score_explicit_visual_title_match(str(title.get("title") or ""), str(line_specs[idx].get("text") or ""))
            if score <= 0:
                continue
            if score > best_score:
                best_index = idx
                best_score = score
                if score >= 100:
                    break
        if best_index is None:
            continue
        used_indexes.add(best_index)
        cursor = best_index + 1
        matched.append(
            {
                **title,
                "line_index": best_index,
                "page_index": int(line_specs[best_index].get("page_index") or 0),
                "bbox": tuple(line_specs[best_index].get("bbox") or ()),
            }
        )
    return matched


def _build_explicit_visual_event_crop_plan(
    pdf_document,
    *,
    line_specs: list[dict[str, Any]],
    start_index: int,
    next_index: int | None,
    kind: str = "table",
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    if start_index < 0 or start_index >= len(line_specs):
        return []
    start_spec = line_specs[start_index]
    next_spec = line_specs[next_index] if next_index is not None and 0 <= next_index < len(line_specs) else None
    start_bbox = tuple(start_spec.get("bbox") or ())
    next_bbox = tuple(next_spec.get("bbox") or ()) if next_spec else ()
    if len(start_bbox) < 4:
        return []

    segment_specs = line_specs[start_index : next_index if next_index is not None else len(line_specs)]
    start_page = int(start_spec.get("page_index") or 0)
    end_page = int(next_spec.get("page_index") or start_page) if next_spec else int(segment_specs[-1].get("page_index") or start_page)
    title_token = _normalize_preview_title_token(start_spec.get("text") or "")

    if kind == "figure":
        title_page = pdf_document.load_page(start_page)
        title_page_specs = [
            spec
            for spec in line_specs
            if int(spec.get("page_index") or 0) == start_page
        ]
        title_page_position = next(
            (
                idx
                for idx, spec in enumerate(title_page_specs)
                if _normalize_preview_title_token(spec.get("text") or "") == title_token
            ),
            len(title_page_specs),
        )
        previous_spec = _find_last_meaningful_spec_before_index(
            title_page_specs,
            end_index=title_page_position,
            title_token=title_token,
        )
        above_top = (
            min(float(title_page.rect.height) - 24.0, float(tuple(previous_spec.get("bbox") or (0.0, 0.0, 0.0, 0.0))[3]) + 10.0)
            if previous_spec is not None and len(tuple(previous_spec.get("bbox") or ())) >= 4
            else 24.0
        )
        above_bbox = (
            24.0,
            max(0.0, above_top),
            max(25.0, float(title_page.rect.width) - 24.0),
            min(float(title_page.rect.height), float(start_bbox[3]) + 10.0),
        )
        below_top = max(0.0, float(start_bbox[1]) - 6.0)
        below_bottom = float(title_page.rect.height)
        if next_spec is not None and int(next_spec.get("page_index") or -1) == start_page and len(next_bbox) >= 4:
            below_bottom = min(below_bottom, max(below_top + 24.0, float(next_bbox[1]) - 8.0))
        below_bbox = (
            24.0,
            below_top,
            max(25.0, float(title_page.rect.width) - 24.0),
            below_bottom,
        )
        above_score = _score_explicit_visual_candidate(pdf_document, page_index=start_page, bbox=above_bbox)
        below_score = _score_explicit_visual_candidate(pdf_document, page_index=start_page, bbox=below_bbox)
        prefer_above = above_score[0] > below_score[0] or (
            above_score[0] == below_score[0] and above_score[1] > below_score[1] + 48.0
        )
        prefer_below = below_score[0] > above_score[0] or (
            above_score[0] == below_score[0] and below_score[1] > above_score[1] + 48.0
        )
        if prefer_above:
            if above_bbox[3] > above_bbox[1] + 16.0:
                return [(start_page, above_bbox)]
        elif prefer_below and below_bbox[3] > below_bbox[1] + 16.0:
            return [(start_page, below_bbox)]

    crop_plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    for page_index in range(start_page, end_page + 1):
        page = pdf_document.load_page(page_index)
        page_specs = [spec for spec in segment_specs if int(spec.get("page_index") or 0) == page_index]
        page_boxes = [
            tuple(float(value) for value in (spec.get("bbox") or ())[:4])
            for spec in page_specs
            if len(tuple(spec.get("bbox") or ())) >= 4
        ]
        window_top = max(0.0, float(start_bbox[1]) - 6.0) if page_index == start_page else 0.0
        if next_spec is not None and page_index == int(next_spec.get("page_index") or -1) and len(next_bbox) >= 4:
            window_bottom = max(window_top + 24.0, float(next_bbox[1]) - 8.0)
        else:
            window_bottom = float(page.rect.height)
        cutoff_y = _find_explicit_visual_cutoff_y(
            page_specs,
            kind=kind,
            page_width=float(page.rect.width),
            start_line_index=1 if page_index == start_page else 0,
        )
        if cutoff_y is not None:
            window_bottom = min(window_bottom, cutoff_y)
        elif kind == "table" and page_boxes and (
            next_spec is None or int(next_spec.get("page_index") or -1) != page_index
        ):
            observed_bottom = max(float(box[3]) for box in page_boxes) + 12.0
            window_bottom = min(window_bottom, observed_bottom)
        if window_bottom <= window_top + 8.0:
            continue
        if page_index == start_page and end_page > start_page and not _page_segment_has_non_title_content(
            page_specs,
            title_token=title_token,
        ):
            continue
        content_bboxes = _collect_anchor_window_content_bboxes(
            page,
            window_top=window_top,
            window_bottom=window_bottom,
            include_drawings=True,
        )
        if kind == "figure":
            crop_bbox = (
                24.0,
                window_top,
                max(25.0, float(page.rect.width) - 24.0),
                window_bottom,
            )
        else:
            crop_bbox = _expand_section_window_crop_bbox(page, content_bboxes)
            if crop_bbox is None and page_boxes:
                crop_bbox = (
                    max(0.0, min(float(box[0]) for box in page_boxes) - 16.0),
                    window_top,
                    min(float(page.rect.width), max(float(box[2]) for box in page_boxes) + 16.0),
                    min(float(page.rect.height), max(window_bottom, max(float(box[3]) for box in page_boxes) + 8.0)),
                )
            elif crop_bbox is None:
                crop_bbox = (
                    24.0,
                    window_top,
                    max(25.0, float(page.rect.width) - 24.0),
                    window_bottom,
                )
        crop_bbox = (
            max(0.0, float(crop_bbox[0])),
            max(window_top, float(crop_bbox[1])),
            min(float(page.rect.width), float(crop_bbox[2])),
            min(window_bottom, float(crop_bbox[3])),
        )
        if end_page > start_page and float(crop_bbox[3]) - float(crop_bbox[1]) < 56.0 and len(page_specs) <= 2:
            continue
        if crop_bbox[3] <= crop_bbox[1] + 16.0:
            continue
        crop_plan.append((page_index, crop_bbox))
    return crop_plan


def _build_preview_snapshot_ref_and_asset(
    row: dict[str, Any],
    *,
    document_id: str,
    title: str,
    table_id: str,
    source_snapshot_path: str,
    source_snapshot_meta: dict[str, Any],
    existing_ref: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_page = int(source_snapshot_meta.get("resolved_page_start") or row.get("source_page") or 0)
    source_page_end = int(source_snapshot_meta.get("resolved_page_end") or row.get("source_page_end") or source_page)
    image_block_count = int(source_snapshot_meta.get("image_block_count") or 0)
    if existing_ref is not None:
        ref = dict(existing_ref)
        ref["table_title"] = title
        ref["source_snapshot_path"] = source_snapshot_path
        ref["source_page"] = source_page
        ref["source_page_end"] = source_page_end
        ref["source_image_block_count"] = image_block_count
        if not str(ref.get("slice_snapshot_path") or "").strip() and str(ref.get("slice_render_mode") or "").strip().lower() == "snapshot_only":
            ref["slice_snapshot_path"] = source_snapshot_path
        if str(ref.get("slice_render_mode") or "").strip().lower() == "snapshot_only":
            ref["export_variant_locked"] = True
            ref["export_variant"] = "source"
            ref["recommended_export_variant"] = "source"
            ref["export_variant_reasons"] = ["原文更忠实"]
            ref["export_variant_score"] = 100
        ref = _normalize_table_ref(ref)
    else:
        ai_projection = _build_ai_projection(
            table_id=table_id,
            title=title,
            headers=[],
            rows=[],
            notes=["visual_fallback"],
            render_mode="snapshot_only",
        )
        ref = _normalize_table_ref(
            {
                "table_id": table_id,
                "table_title": title,
                "row_range": [],
                "row_ids": [],
                "inherited_headers": [],
                "slice_rows": [],
                "inherited_group_title": "",
                "inherited_notes": ["visual_fallback"],
                "slice_ai_projection": ai_projection,
                "slice_snapshot_path": source_snapshot_path,
                "source_snapshot_path": source_snapshot_path,
                "slice_render_key": table_id,
                "slice_render_mode": "snapshot_only",
                "source_page": source_page,
                "source_page_end": source_page_end,
                "structure_confidence": 0.45,
                "source_image_block_count": image_block_count,
                "export_variant_locked": True,
                "export_variant": "source",
                "recommended_export_variant": "source",
                "export_variant_reasons": ["原文更忠实"],
                "export_variant_score": 100,
            }
        )
    asset = {
        "table_id": str(ref.get("table_id") or table_id),
        "document_id": document_id,
        "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
        "page_span": {"start": source_page, "end": source_page_end},
        "table_title": title,
        "headers": [str(cell or "").strip() for cell in (ref.get("inherited_headers") or []) if str(cell or "").strip()],
        "rows": [list(row_cells) for row_cells in (ref.get("slice_rows") or []) if isinstance(row_cells, list)],
        "group_labels": [],
        "notes": list(ref.get("inherited_notes") or []),
        "render_mode": str(ref.get("slice_render_mode") or ref.get("render_mode") or "snapshot_only"),
        "snapshot_path": str(ref.get("slice_snapshot_path") or source_snapshot_path),
        "source_snapshot_path": source_snapshot_path,
        "ai_projection": ref.get("slice_ai_projection") or "",
        "confidence": float(ref.get("structure_confidence") or 0.45),
        "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
        "source_image_block_count": image_block_count,
    }
    return ref, asset


def _match_existing_ref_for_explicit_title(
    refs: list[dict[str, Any]],
    *,
    title: str,
    used_indexes: set[int],
) -> tuple[int, dict[str, Any]] | tuple[None, None]:
    best_index: int | None = None
    best_score = 0
    for idx, ref in enumerate(refs):
        if idx in used_indexes:
            continue
        score = _score_explicit_visual_title_match(title, str(ref.get("table_title") or ""))
        if score > best_score:
            best_index = idx
            best_score = score
    if best_index is None or best_score <= 0:
        return (None, None)
    return best_index, refs[best_index]


def _content_blocks_for_explicit_visual_titles(
    row: dict[str, Any],
    matched_events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not matched_events:
        return list(row.get("content_blocks") or [])
    title_to_refs: dict[str, list[str]] = defaultdict(list)
    for event in matched_events:
        title_to_refs[_normalize_preview_title_token(event.get("title") or "")].append(str(event.get("table_id") or ""))
    lines = [line.strip() for line in str(row.get("raw_text") or row.get("display_text") or "").splitlines() if line.strip()]
    if not lines:
        return [{"kind": "table", "table_id": str(event.get("table_id") or "")} for event in matched_events]
    blocks: list[dict[str, Any]] = []
    paragraph_buffer: list[str] = []
    title_ref_offsets: dict[str, int] = defaultdict(int)
    for line in lines:
        token = _normalize_preview_title_token(line)
        ref_ids = title_to_refs.get(token) or []
        if ref_ids:
            if paragraph_buffer:
                blocks.append({"kind": "paragraph", "text": "\n".join(paragraph_buffer)})
                paragraph_buffer = []
            blocks.append({"kind": "paragraph", "text": line})
            ref_index = title_ref_offsets[token]
            if ref_index < len(ref_ids):
                blocks.append({"kind": "table", "table_id": ref_ids[ref_index]})
                title_ref_offsets[token] += 1
            continue
        paragraph_buffer.append(line)
    if paragraph_buffer:
        blocks.append({"kind": "paragraph", "text": "\n".join(paragraph_buffer)})
    return blocks


def _should_repair_row_with_explicit_visual_titles(
    row: dict[str, Any],
    *,
    titles: list[dict[str, Any]],
) -> bool:
    if not titles or str(row.get("entry_kind") or "").strip().upper() != "REQUIREMENT":
        return False
    kinds = {str(item.get("kind") or "") for item in titles}
    if "figure" in kinds:
        return True
    if len(titles) >= 2:
        return True
    if any("续" in str(item.get("title") or "") for item in titles):
        return True
    if any(str(item.get("kind") or "") == "table" for item in titles) and not list(row.get("table_refs") or []):
        return True
    return False


def _repair_row_explicit_visual_assets(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    pdf_document,
    source_key: str,
    section_window_meta: dict[str, Any] | None,
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if pdf_document is None or not section_window_meta or not section_window_meta.get("plan"):
        return None, []
    titles = _extract_explicit_visual_titles_from_text(str(row.get("raw_text") or row.get("display_text") or ""))
    if not _should_repair_row_with_explicit_visual_titles(row, titles=titles):
        return None, []

    line_specs = _build_section_visual_line_specs(
        pdf_document=pdf_document,
        source_key=source_key,
        plan=list(section_window_meta.get("plan") or []),
        line_detection_cache=line_detection_cache,
    )
    matched_titles = _match_explicit_visual_titles_to_line_specs(titles, line_specs=line_specs)
    if not matched_titles:
        return None, []

    existing_refs = [dict(ref) for ref in (row.get("table_refs") or []) if isinstance(ref, dict)]
    used_ref_indexes: set[int] = set()
    rebuilt_refs: list[dict[str, Any]] = []
    rebuilt_events: list[dict[str, Any]] = []
    extra_assets: list[dict[str, Any]] = []
    for event_index, event in enumerate(matched_titles):
        next_index = int(matched_titles[event_index + 1]["line_index"]) if event_index + 1 < len(matched_titles) else None
        crop_plan = _build_explicit_visual_event_crop_plan(
            pdf_document,
            line_specs=line_specs,
            start_index=int(event.get("line_index") or 0),
            next_index=next_index,
            kind=str(event.get("kind") or "table"),
        )
        if not crop_plan:
            continue
        snapshot_path, snapshot_meta = _write_source_snapshot_from_crop_plan(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            cache_token="preview-explicit-visual",
            cache_key_hint=(
                f"{row.get('section_instance_key') or row.get('section_key') or row.get('entry_id') or row.get('req_code') or 'visual'}"
                f":{event_index}:{str(event.get('title') or '').strip()}"
            ),
            source_snapshot_cache=source_snapshot_cache,
            pdf_document=pdf_document,
            crop_plan=crop_plan,
        )
        if snapshot_path is None:
            continue
        ref_index, existing_ref = _match_existing_ref_for_explicit_title(
            existing_refs,
            title=str(event.get("title") or ""),
            used_indexes=used_ref_indexes,
        )
        if ref_index is not None:
            used_ref_indexes.add(ref_index)
        table_id = str(existing_ref.get("table_id") or "") if existing_ref else _build_table_id(
            row,
            f"preview-explicit::{event.get('kind') or 'visual'}::{event.get('title') or ''}",
            event_index,
        )
        ref, asset = _build_preview_snapshot_ref_and_asset(
            row,
            document_id=document_id,
            title=str(event.get("title") or ""),
            table_id=table_id,
            source_snapshot_path=str(snapshot_path),
            source_snapshot_meta=snapshot_meta,
            existing_ref=existing_ref,
        )
        rebuilt_refs.append(ref)
        rebuilt_events.append({**event, "table_id": str(ref.get("table_id") or "")})
        extra_assets.append(asset)
    if not rebuilt_refs:
        return None, []

    updated = dict(row)
    updated["table_refs"] = rebuilt_refs
    updated["content_blocks"] = _content_blocks_for_explicit_visual_titles(updated, rebuilt_events)
    updated["has_table_asset"] = True
    updated["table_render_mode"] = _collapse_preview_table_render_mode(rebuilt_refs)
    return updated, extra_assets


def _build_layout_group_source_snapshot(
    row: dict[str, Any],
    group: dict[str, Any],
    *,
    group_index: int,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None,
    pdf_document,
    line_specs: list[dict[str, Any]],
    fallback_source_snapshot_path: str,
    fallback_source_image_block_count: int,
) -> tuple[str, int]:
    if pdf_document is None or not source_snapshot_cache or not source_file_path:
        return fallback_source_snapshot_path, fallback_source_image_block_count
    used_indexes: list[int] = []
    for spec in group.get("row_specs") or []:
        used_indexes.extend(int(value) for value in (spec.get("used_indexes") or []) if str(value).strip())
    crop_plan = _build_layout_group_crop_plan(
        pdf_document,
        line_specs=line_specs,
        used_indexes=used_indexes,
    )
    if not crop_plan:
        return fallback_source_snapshot_path, fallback_source_image_block_count
    cache_key_hint = (
        f"{row.get('section_instance_key') or row.get('section_key') or row.get('entry_id') or row.get('req_code') or 'layout-group'}"
        f":{group_index}:{str(group.get('title') or '').strip()}"
    )
    snapshot_path, snapshot_meta = _write_source_snapshot_from_crop_plan(
        snapshot_root=snapshot_root,
        source_file_path=source_file_path,
        cache_token="preview-structured-source-group",
        cache_key_hint=cache_key_hint,
        source_snapshot_cache=source_snapshot_cache,
        pdf_document=pdf_document,
        crop_plan=crop_plan,
    )
    if snapshot_path is None:
        return fallback_source_snapshot_path, fallback_source_image_block_count
    return str(snapshot_path), int(snapshot_meta.get("image_block_count") or 0)


def _build_layout_table_structured_preview(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str = "",
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] | None = None,
    pdf_document,
    source_key: str,
    section_window_meta: dict[str, Any] | None,
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
    source_snapshot_path: str = "",
    source_image_block_count: int = 0,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if pdf_document is None or not section_window_meta:
        return None, []
    plan = list(section_window_meta.get("plan") or [])
    if not plan:
        return None, []
    section_heading_tokens = {
        _compact_layout_table_text(f"{row.get('section_code') or ''}{row.get('section_title') or ''}"),
        _compact_layout_table_text(row.get("section_title") or ""),
        _compact_layout_table_text(row.get("section_label") or ""),
    }
    section_heading_tokens = {token for token in section_heading_tokens if token}
    line_specs = _build_layout_table_line_specs(
        pdf_document=pdf_document,
        source_key=source_key,
        plan=plan,
        line_detection_cache=line_detection_cache,
        section_heading_tokens=section_heading_tokens,
    )
    groups = _recover_layout_headerless_tables_from_line_specs(
        line_specs,
        section_heading_tokens=section_heading_tokens,
    )
    if not groups:
        return None, []
    groups, trailing_paragraphs = _extract_section_level_tail_notes_from_layout_groups(groups)

    rebuilt_refs: list[dict[str, Any]] = []
    rebuilt_assets: list[dict[str, Any]] = []
    rebuilt_blocks: list[dict[str, Any]] = [
        {"kind": "paragraph", "text": f"{row.get('section_label') or row.get('section_code') or ''} {row.get('section_title') or ''}".strip()}
    ]
    for block_index, group in enumerate(groups):
        group_source_snapshot_path, group_source_image_block_count = _build_layout_group_source_snapshot(
            row,
            group,
            group_index=block_index,
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_snapshot_cache=source_snapshot_cache,
            pdf_document=pdf_document,
            line_specs=line_specs,
            fallback_source_snapshot_path=source_snapshot_path,
            fallback_source_image_block_count=source_image_block_count,
        )
        ref, asset = _build_preview_structured_table_ref_and_asset(
            row,
            document_id=document_id,
            snapshot_root=snapshot_root,
            block_index=block_index,
            table_title=str(group.get("title") or "").strip(),
            headers=[],
            rows=[list(item) for item in (group.get("rows") or [])],
            headerless=True,
            source_snapshot_path=group_source_snapshot_path,
            source_image_block_count=group_source_image_block_count,
        )
        if ref is None or asset is None:
            return None, []
        rebuilt_blocks.append({"kind": "paragraph", "text": str(group.get("title") or "").strip()})
        rebuilt_blocks.append({"kind": "table", "table_id": str(ref.get("table_id") or "")})
        rebuilt_refs.append(ref)
        rebuilt_assets.append(asset)
    for paragraph in trailing_paragraphs:
        if str(paragraph or "").strip():
            rebuilt_blocks.append({"kind": "paragraph", "text": str(paragraph).strip()})

    updated = dict(row)
    updated["table_refs"] = rebuilt_refs
    updated["content_blocks"] = rebuilt_blocks
    updated["has_table_asset"] = True
    updated["table_render_mode"] = "structured"
    return updated, rebuilt_assets


def _render_preview_structured_table_snapshot(
    *,
    table_id: str,
    snapshot_root: Path,
    headers: list[str],
    rows: list[list[str]],
) -> Path | None:
    target = snapshot_root / f"{table_id}.png"
    if _snapshot_file_is_usable(target):
        return target
    try:
        image_bytes = render_table_png(headers, rows, include_header=bool(headers))
        _write_png_atomically(target, image_bytes)
    except Exception:
        return None
    return target if _snapshot_file_is_usable(target) else None


def _build_preview_structured_table_ref_and_asset(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    block_index: int,
    table_title: str,
    headers: list[str],
    rows: list[list[str]],
    headerless: bool = False,
    source_snapshot_path: str = "",
    source_image_block_count: int = 0,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    table_id = _build_table_id(row, f"preview-local-clause::{block_index}:{table_title}", block_index)
    snapshot_path = _render_preview_structured_table_snapshot(
        table_id=table_id,
        snapshot_root=snapshot_root,
        headers=headers,
        rows=rows,
    )
    if snapshot_path is None:
        return None, None
    source_page = _to_int(row.get("source_page"))
    source_page_end = _to_int(row.get("source_page_end")) or source_page
    ai_projection = _build_ai_projection(
        table_id=table_id,
        title=table_title,
        headers=headers,
        rows=rows,
        notes=[table_title] if table_title else [],
        render_mode="structured",
    )
    ref = _normalize_table_ref(
        {
            "table_id": table_id,
            "table_title": table_title,
            "row_range": [1, len(rows)] if rows else [],
            "row_ids": [f"r{idx + 1}" for idx in range(len(rows))],
            "inherited_headers": headers,
            "slice_rows": rows,
            "inherited_group_title": "",
            "inherited_notes": [table_title] if table_title else [],
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(snapshot_path),
            "source_snapshot_path": str(source_snapshot_path or ""),
            "slice_render_key": table_id,
            "slice_render_mode": "structured",
            "headerless": headerless,
            "source_page": source_page,
            "source_page_end": source_page_end,
            "structure_confidence": 0.86,
            "source_image_block_count": int(source_image_block_count or 0),
            "export_variant_locked": True,
            "recommended_export_variant": "slice",
            "export_variant": "slice",
        }
    )
    asset = {
        "table_id": table_id,
        "document_id": document_id,
        "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
        "page_span": {"start": source_page, "end": source_page_end},
        "table_title": table_title,
        "headers": headers,
        "rows": rows,
        "group_labels": [],
        "notes": [table_title] if table_title else [],
        "render_mode": "structured",
        "headerless": headerless,
        "snapshot_path": str(snapshot_path),
        "source_snapshot_path": str(source_snapshot_path or ""),
        "ai_projection": ai_projection,
        "confidence": 0.86,
        "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
        "source_image_block_count": int(source_image_block_count or 0),
    }
    return ref, asset


def _repair_local_clause_pipe_table_preview(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_snapshot_path: str = "",
    source_image_block_count: int = 0,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    raw_text = str(row.get("raw_text") or "")
    if not raw_text:
        return None, []
    blocks = _split_text_blocks(raw_text)
    table_indexes = [
        idx
        for idx, block in enumerate(blocks)
        if str(block.get("kind") or "").strip().lower() == "table"
    ]
    if len(table_indexes) < 2:
        return None, []
    if not any(
        _looks_like_local_clause_label(str(blocks[idx].get("text") or "").strip())
        for idx in range(1, len(blocks) - 1)
        if str(blocks[idx].get("kind") or "").strip().lower() == "paragraph"
    ):
        return None, []
    existing_refs = list(row.get("table_refs") or [])
    if existing_refs and len(existing_refs) >= len(table_indexes) and not any(
        _is_suspicious_structured_table_ref(ref) for ref in existing_refs
    ):
        return None, []

    rebuilt_refs: list[dict[str, Any]] = []
    rebuilt_assets: list[dict[str, Any]] = []
    rebuilt_blocks: list[dict[str, Any]] = []
    table_counter = 0

    for idx, block in enumerate(blocks):
        kind = str(block.get("kind") or "").strip().lower()
        if kind != "table":
            text = str(block.get("text") or "").strip()
            if text:
                rebuilt_blocks.append({"kind": "paragraph", "text": text})
            continue
        parsed_rows = [
            _parse_markdown_cells(line)
            for line in (block.get("lines") or [])
            if _looks_like_markdown_table_row(line)
        ]
        if not parsed_rows or any(len(parsed) != 2 for parsed in parsed_rows):
            return None, []
        previous_text = ""
        if idx > 0 and str(blocks[idx - 1].get("kind") or "").strip().lower() == "paragraph":
            previous_text = str(blocks[idx - 1].get("text") or "").strip()
        if previous_text and _looks_like_local_clause_label(previous_text):
            table_title = previous_text
        else:
            subject = _infer_pipe_table_subject_token(parsed_rows)
            if subject:
                table_title = f"{subject}:"
            else:
                table_title = str(row.get("section_label") or row.get("section_code") or row.get("section_title") or "").strip()
        headerless = _looks_like_headerless_key_value_rows(parsed_rows)
        headers = [] if headerless else [table_title or "项目", "说明"]
        if headerless and table_title:
            last_block = rebuilt_blocks[-1] if rebuilt_blocks else None
            last_kind = str(last_block.get("kind") or "").strip().lower() if isinstance(last_block, dict) else ""
            last_text = str(last_block.get("text") or "").strip() if isinstance(last_block, dict) else ""
            if last_kind != "paragraph" or last_text != table_title:
                rebuilt_blocks.append({"kind": "paragraph", "text": table_title})
        ref, asset = _build_preview_structured_table_ref_and_asset(
            row,
            document_id=document_id,
            snapshot_root=snapshot_root,
            block_index=table_counter,
            table_title=table_title,
            headers=headers,
            rows=parsed_rows,
            headerless=headerless,
            source_snapshot_path=source_snapshot_path,
            source_image_block_count=source_image_block_count,
        )
        if ref is None or asset is None:
            return None, []
        rebuilt_refs.append(ref)
        rebuilt_assets.append(asset)
        rebuilt_blocks.append({"kind": "table", "table_id": str(ref.get("table_id") or "")})
        table_counter += 1

    if len(rebuilt_refs) < 2:
        return None, []
    updated = dict(row)
    updated["table_refs"] = rebuilt_refs
    updated["content_blocks"] = rebuilt_blocks
    updated["has_table_asset"] = True
    updated["table_render_mode"] = "structured"
    return updated, rebuilt_assets


def _sanitize_row_table_refs(row: dict[str, Any]) -> dict[str, Any]:
    refs = list(row.get("table_refs") or [])
    if len(refs) < 2:
        return row
    suspicious_ids = {
        str(ref.get("table_id") or "").strip()
        for ref in refs
        if _is_suspicious_structured_table_ref(ref)
    }
    if not suspicious_ids or len(suspicious_ids) >= len(refs):
        return row
    kept_refs = [
        ref
        for ref in refs
        if str(ref.get("table_id") or "").strip() not in suspicious_ids
    ]
    if not kept_refs:
        return row
    updated = dict(row)
    updated["table_refs"] = kept_refs
    allowed_ids = {
        str(ref.get("table_id") or "").strip()
        for ref in kept_refs
        if str(ref.get("table_id") or "").strip()
    }
    if allowed_ids:
        updated["content_blocks"] = _filter_content_blocks_by_table_ids(
            updated.get("content_blocks") or [],
            allowed_ids,
        )
    return updated


def _section_window_lookup_key(row: dict[str, Any]) -> str:
    return str(row.get("section_instance_key") or row.get("section_key") or row.get("section_code") or "").strip()


def _build_section_window_index(sections: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    ordered = [
        section
        for section in sections or []
        if str(section.get("section_instance_key") or section.get("section_key") or section.get("section_code") or "").strip()
    ]
    index: dict[str, dict[str, Any]] = {}
    for position, section in enumerate(ordered):
        key = _section_window_lookup_key(section)
        if not key:
            continue
        index[key] = {
            "section": section,
            "next_section": ordered[position + 1] if position + 1 < len(ordered) else None,
        }
    return index


def _section_anchor_text(section: dict[str, Any]) -> str:
    code = str(section.get("section_label") or section.get("section_code") or "").strip()
    title = str(section.get("section_title") or "").strip()
    preview_first_line = next(
        (line.strip() for line in str(section.get("text_preview") or "").splitlines() if line.strip()),
        "",
    )
    candidates: list[str] = []
    if code and title:
        candidates.append(f"{code} {title}")
    if title:
        candidates.append(title)
    if code:
        candidates.append(code)
    if preview_first_line:
        candidates.append(preview_first_line)
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        token = re.sub(r"\s+", " ", candidate).strip()
        if not token or token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return "\n".join(deduped)


def _expand_section_window_crop_bbox(
    page,
    content_bboxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float] | None:
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


def _is_significant_visual_rect(rect) -> bool:
    width = float(rect.width)
    height = float(rect.height)
    area = float(rect.get_area())
    return area >= _SIGNIFICANT_VISUAL_MIN_AREA or (
        width >= _SIGNIFICANT_VISUAL_MIN_WIDTH and height >= _SIGNIFICANT_VISUAL_MIN_HEIGHT
    )


def _drawing_item_ops(drawing: dict[str, Any]) -> list[str]:
    ops: list[str] = []
    for item in drawing.get("items") or []:
        if isinstance(item, tuple) and item:
            ops.append(str(item[0]))
        else:
            ops.append(type(item).__name__)
    return ops


def _drawing_counts_as_semantic_visual(drawing: dict[str, Any], rect) -> bool:
    if not _is_significant_visual_rect(rect):
        return False
    ops = _drawing_item_ops(drawing)
    if not ops:
        return False
    drawing_type = str(drawing.get("type") or "").strip().lower()
    fill = drawing.get("fill")
    color = drawing.get("color")
    width = drawing.get("width")
    is_plain_fill_rect = (
        drawing_type == "f"
        and color is None
        and all(op == "re" for op in ops)
    )
    if is_plain_fill_rect:
        return False
    has_curve = any(op == "c" for op in ops)
    has_line = any(op == "l" for op in ops)
    if has_curve:
        return True
    if drawing_type == "s" and has_line and color is not None and float(width or 0.0) >= 0.2:
        return True
    if drawing_type in {"fs", "sf"} and has_line:
        return True
    if fill is not None and color is not None and has_line:
        return True
    return False


def _text_block_counts_as_layout_table_label(text: str, block_rect, clip) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if not compact or len(compact) > 24:
        return False
    if float(getattr(block_rect, "width", 0.0) or 0.0) > min(float(getattr(clip, "width", 0.0) or 0.0) * 0.32, 132.0):
        return False
    if float(getattr(block_rect, "x0", 0.0) or 0.0) > float(getattr(clip, "x0", 0.0) or 0.0) + max(
        120.0,
        float(getattr(clip, "width", 0.0) or 0.0) * 0.18,
    ):
        return False
    if float(getattr(block_rect, "height", 0.0) or 0.0) > 52.0:
        return False
    return len(compact) <= 14 or compact.endswith(("：", ":"))


def _text_block_counts_as_layout_table_body(text: str, block_rect, clip) -> bool:
    compact = re.sub(r"\s+", "", str(text or ""))
    if len(compact) < 18:
        return False
    if float(getattr(block_rect, "width", 0.0) or 0.0) < min(float(getattr(clip, "width", 0.0) or 0.0) * 0.42, 220.0):
        return False
    if float(getattr(block_rect, "x0", 0.0) or 0.0) < float(getattr(clip, "x0", 0.0) or 0.0) + max(
        90.0,
        float(getattr(clip, "width", 0.0) or 0.0) * 0.18,
    ):
        return False
    return True


def _find_section_anchor(
    document,
    *,
    source_key: str,
    section: dict[str, Any] | None,
    page_index: int,
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
    prefer_last: bool = False,
) -> dict[str, Any] | None:
    if section is None:
        return None
    candidates = _build_context_anchor_fragments(_section_anchor_text(section), from_end=False)
    if not candidates:
        return None
    line_records = _load_pdf_page_line_records(
        document,
        source_key=source_key,
        page_index=page_index,
        detection_cache=line_detection_cache,
    )
    best = _best_context_anchor_in_page(
        line_records,
        candidates,
        prefer_last=prefer_last,
    )
    code_text = str(section.get("section_label") or section.get("section_code") or "").strip()
    title_text = str(section.get("section_title") or "").strip()
    code_anchor = (
        _best_context_anchor_in_page(
            line_records,
            _build_context_anchor_fragments(code_text, from_end=False),
            prefer_last=prefer_last,
        )
        if code_text
        else None
    )
    title_anchor = (
        _best_context_anchor_in_page(
            line_records,
            _build_context_anchor_fragments(title_text, from_end=False),
            prefer_last=prefer_last,
        )
        if title_text
        else None
    )
    if code_anchor is not None and title_anchor is not None:
        code_bbox = tuple(code_anchor.get("bbox") or ())
        title_bbox = tuple(title_anchor.get("bbox") or ())
        if len(code_bbox) >= 4 and len(title_bbox) >= 4:
            vertical_gap = float(title_bbox[1]) - float(code_bbox[1])
            if 0.0 <= vertical_gap <= 48.0:
                best = code_anchor
    elif code_anchor is not None and best is None:
        best = code_anchor
    if best is None:
        return None
    return {
        "page_index": page_index,
        "text": str(best.get("text") or ""),
        "bbox": tuple(best.get("bbox") or ()),
    }


def _build_section_window_crop_plan(
    document,
    *,
    source_key: str,
    section_window: dict[str, Any],
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
) -> list[tuple[int, tuple[float, float, float, float] | None]]:
    section = section_window.get("section") or {}
    next_section = section_window.get("next_section") or {}
    start_page = _to_int(section.get("source_page_start"))
    end_page = _to_int(section.get("source_page_end")) or start_page
    if start_page <= 0 or end_page <= 0:
        return []
    start_index = start_page - 1
    end_index = max(start_index, end_page - 1)
    if start_index >= document.page_count:
        return []
    end_index = min(document.page_count - 1, end_index)

    start_anchor = _find_section_anchor(
        document,
        source_key=source_key,
        section=section,
        page_index=start_index,
        line_detection_cache=line_detection_cache,
    )

    next_anchor: dict[str, Any] | None = None
    next_start_page = _to_int(next_section.get("source_page_start"))
    if next_start_page > 0:
        next_page_index = next_start_page - 1
        if start_index <= next_page_index <= end_index:
            next_anchor = _find_section_anchor(
                document,
                source_key=source_key,
                section=next_section,
                page_index=next_page_index,
                line_detection_cache=line_detection_cache,
            )

    plan: list[tuple[int, tuple[float, float, float, float] | None]] = []
    for page_index in range(start_index, end_index + 1):
        page = document.load_page(page_index)
        window_top = 0.0
        if page_index == start_index and start_anchor is not None:
            bbox = tuple(start_anchor.get("bbox") or ())
            if len(bbox) >= 4:
                window_top = max(0.0, float(bbox[1]) - 14.0)
        window_bottom = float(page.rect.height)
        if next_anchor is not None and page_index == int(next_anchor.get("page_index", -1)):
            bbox = tuple(next_anchor.get("bbox") or ())
            if len(bbox) >= 4:
                window_bottom = min(window_bottom, max(window_top + 4.0, float(bbox[1]) - 8.0))
        if window_bottom <= window_top + 4.0:
            continue
        content_bboxes = _collect_anchor_window_content_bboxes(
            page,
            window_top=window_top,
            window_bottom=window_bottom,
            include_drawings=True,
        )
        crop_bbox = _expand_section_window_crop_bbox(page, content_bboxes)
        if crop_bbox is None:
            continue
        if page_index == start_index and start_anchor is not None:
            start_bbox = tuple(start_anchor.get("bbox") or ())
            if len(start_bbox) >= 4:
                crop_bbox = (
                    crop_bbox[0],
                    max(crop_bbox[1], float(start_bbox[1]) - 4.0),
                    crop_bbox[2],
                    crop_bbox[3],
                )
        if next_anchor is not None and page_index == int(next_anchor.get("page_index", -1)):
            next_bbox = tuple(next_anchor.get("bbox") or ())
            if len(next_bbox) >= 4:
                crop_bbox = (
                    crop_bbox[0],
                    crop_bbox[1],
                    crop_bbox[2],
                    min(crop_bbox[3], float(next_bbox[1]) - 6.0),
                )
        if crop_bbox[3] <= crop_bbox[1] + 16.0:
            continue
        plan.append((page_index, crop_bbox))
    return plan


def _summarize_section_window_visual_features(
    document,
    plan: list[tuple[int, tuple[float, float, float, float] | None]],
) -> dict[str, Any]:
    image_block_count = 0
    drawing_count = 0
    left_label_block_count = 0
    wide_body_block_count = 0
    for page_index, bbox in plan:
        page = document.load_page(page_index)
        clip = fitz.Rect(*bbox) if bbox is not None else page.rect
        for block in page.get_text("blocks", clip=clip, sort=True) or []:
            if len(block) < 5:
                continue
            x0, y0, x1, y1, text = block[:5]
            cleaned = str(text or "").strip()
            if not cleaned:
                continue
            try:
                block_rect = fitz.Rect(float(x0), float(y0), float(x1), float(y1))
            except (TypeError, ValueError):
                continue
            if _text_block_counts_as_layout_table_label(cleaned, block_rect, clip):
                left_label_block_count += 1
            if _text_block_counts_as_layout_table_body(cleaned, block_rect, clip):
                wide_body_block_count += 1
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
            if not clip.intersects(block_rect):
                continue
            intersection = clip & block_rect
            if _is_significant_visual_rect(intersection):
                image_block_count += 1
        for drawing in page.get_drawings() or []:
            rect = drawing.get("rect") or drawing.get("bbox")
            if rect is None:
                continue
            try:
                drawing_rect = fitz.Rect(rect)
            except (TypeError, ValueError):
                continue
            if not clip.intersects(drawing_rect):
                continue
            intersection = clip & drawing_rect
            if _drawing_counts_as_semantic_visual(drawing, intersection):
                drawing_count += 1
    return {
        "image_block_count": image_block_count,
        "drawing_count": drawing_count,
        "left_label_block_count": left_label_block_count,
        "wide_body_block_count": wide_body_block_count,
        "significant_visual_count": image_block_count + drawing_count,
        "page_span_count": len(plan),
    }


def _get_section_window_snapshot_meta(
    row: dict[str, Any],
    *,
    pdf_document,
    source_key: str,
    section_window_index: dict[str, dict[str, Any]],
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]],
    section_window_meta_cache: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    key = _section_window_lookup_key(row)
    if not key:
        return None
    if key in section_window_meta_cache:
        return section_window_meta_cache[key]
    section_window = section_window_index.get(key)
    if pdf_document is None or not section_window:
        section_window_meta_cache[key] = {}
        return section_window_meta_cache[key]
    plan = _build_section_window_crop_plan(
        pdf_document,
        source_key=source_key,
        section_window=section_window,
        line_detection_cache=line_detection_cache,
    )
    if not plan:
        section_window_meta_cache[key] = {}
        return section_window_meta_cache[key]
    summary = _summarize_section_window_visual_features(pdf_document, plan)
    meta = {
        "plan": plan,
        **summary,
        "visual_block_count": int(summary.get("significant_visual_count") or 0),
    }
    section_window_meta_cache[key] = meta
    return meta


def _write_section_window_source_snapshot(
    *,
    snapshot_root: Path,
    source_file_path: str,
    source_page: int,
    source_page_end: int,
    cache_token: str,
    cache_key_hint: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    pdf_document,
    section_window_meta: dict[str, Any],
    max_height_px: int | None = None,
    max_width_px: int = 1600,
) -> tuple[Path | None, dict[str, Any]]:
    plan = list(section_window_meta.get("plan") or [])
    source_path = Path(str(source_file_path or "").strip())
    if pdf_document is None or not plan or not source_path.exists():
        return None, {}
    page_end = source_page_end if source_page_end >= source_page else source_page
    signature = hashlib.sha1(
        "|".join(
            [
                _SECTION_WINDOW_SNAPSHOT_SIGNATURE_VERSION,
                cache_token,
                cache_key_hint,
            ]
        ).encode("utf-8")
    ).hexdigest()[:10]
    cache_key = (str(source_path.resolve()), source_page, page_end, signature)
    if cache_key in source_snapshot_cache:
        return source_snapshot_cache[cache_key], {
            key: value for key, value in section_window_meta.items() if key != "plan"
        }

    snapshot_root.mkdir(parents=True, exist_ok=True)
    seed = "|".join([cache_key[0], str(cache_key[1]), str(cache_key[2]), cache_key[3]])
    target = snapshot_root / f"src_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}.png"
    if _snapshot_file_is_usable(target):
        source_snapshot_cache[cache_key] = target
        return target, {
            key: value for key, value in section_window_meta.items() if key != "plan"
        }
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    try:
        render_width = max(1, int(max_width_px or 1600))
        images = _render_pdf_crop_plan(
            pdf_document,
            plan,
            max_width_px=render_width,
            max_scale=5.0,
        )
        rendered = _compose_vertical_images(
            images,
            max_width_px=render_width,
            max_height_px=max_height_px,
        )
        _write_png_atomically(target, rendered)
    except Exception:
        source_snapshot_cache[cache_key] = None
        return None, {}

    source_snapshot_cache[cache_key] = target
    return target, {
        key: value for key, value in section_window_meta.items() if key != "plan"
    }


def _page_visual_count(
    parser_diagnostics: dict[str, Any],
    *,
    source_page: int,
    source_page_end: int,
) -> int:
    start = int(source_page or 0)
    end = int(source_page_end or source_page or 0)
    if start <= 0 or end <= 0:
        return 0
    total = 0
    for row in parser_diagnostics.get("page_details", []) or []:
        page_no = int(row.get("page_no") or 0)
        if start <= page_no <= end:
            total += int(row.get("image_count", 0) or 0) + int(row.get("graphics_count", 0) or 0)
    return total


def _visual_row_rank(row: dict[str, Any]) -> tuple[int, int, int]:
    return (
        1 if str(row.get("source_anchor") or "").strip() else 0,
        len(str(row.get("raw_text") or "").strip()),
        len(str(row.get("display_text") or "").strip()),
    )


def _row_needs_visual_snapshot_fallback(
    row: dict[str, Any],
    parser_diagnostics: dict[str, Any],
    *,
    section_window_meta: dict[str, Any] | None = None,
) -> bool:
    if row.get("table_refs"):
        return False
    source_page = _to_int(row.get("source_page"))
    source_page_end = _to_int(row.get("source_page_end")) or source_page
    if source_page <= 0:
        return False
    text = "\n".join(
        part
        for part in (
            str(row.get("section_title") or "").strip(),
            str(row.get("display_text") or "").strip(),
            str(row.get("raw_text") or "").strip(),
        )
        if part
    )
    if VISUAL_SNAPSHOT_CUE_RE.search(text):
        return True
    return int((section_window_meta or {}).get("visual_block_count") or 0) > 0


def _row_prefers_visual_snapshot_over_structured_refs(
    row: dict[str, Any],
    *,
    section_window_meta: dict[str, Any] | None = None,
) -> bool:
    refs = list(row.get("table_refs") or [])
    if len(refs) <= 1:
        return False
    if any(str(ref.get("slice_render_mode") or "").strip().lower() != "structured" for ref in refs):
        return False
    text = "\n".join(
        part
        for part in (
            str(row.get("section_title") or "").strip(),
            str(row.get("display_text") or "").strip(),
            str(row.get("raw_text") or "").strip(),
        )
        if part
    )
    if not VISUAL_SNAPSHOT_CUE_RE.search(text):
        return False
    if section_window_meta is None:
        return True
    return int(section_window_meta.get("visual_block_count") or 0) > 0


def _row_needs_full_page_table_fallback(row: dict[str, Any]) -> bool:
    refs = list(row.get("table_refs") or [])
    if len(refs) != 1:
        return False
    ref = refs[0]
    if str(ref.get("slice_render_mode") or "").strip().lower() != "structured":
        return False
    ref_title = _normalize_preview_title_token(ref.get("table_title") or "")
    context_tokens = {
        _normalize_preview_title_token(row.get("section_code") or ""),
        _normalize_preview_title_token(row.get("section_title") or ""),
        _normalize_preview_title_token(row.get("section_label") or ""),
    }
    first_line = str(row.get("display_text") or row.get("raw_text") or "").splitlines()[0].strip() if str(
        row.get("display_text") or row.get("raw_text") or ""
    ).strip() else ""
    context_tokens.add(_normalize_preview_title_token(first_line))
    context_tokens = {token for token in context_tokens if token}
    if ref_title and context_tokens and not any(ref_title in token or token in ref_title for token in context_tokens):
        return False
    raw_body_rows = _raw_markdown_table_body_row_count(str(row.get("raw_text") or ""))
    if raw_body_rows <= 0:
        return False
    slice_rows = len(ref.get("row_ids") or ref.get("slice_rows") or [])
    return raw_body_rows >= slice_rows + 1


def _row_needs_layout_table_snapshot_fallback(
    row: dict[str, Any],
    *,
    section_window_meta: dict[str, Any] | None = None,
) -> bool:
    if row.get("table_refs"):
        return False
    meta = section_window_meta or {}
    if int(meta.get("visual_block_count") or 0) > 0:
        return False
    if int(meta.get("page_span_count") or 0) < _LAYOUT_TABLE_MIN_PAGE_SPAN:
        return False
    if int(meta.get("left_label_block_count") or 0) < _LAYOUT_TABLE_MIN_LABEL_BLOCKS:
        return False
    if int(meta.get("wide_body_block_count") or 0) < _LAYOUT_TABLE_MIN_BODY_BLOCKS:
        return False
    text = str(row.get("raw_text") or row.get("display_text") or "")
    if len(text.strip()) > _LAYOUT_TABLE_MAX_TEXT_CHARS:
        return False
    if _count_nonempty_text_lines(text) > _LAYOUT_TABLE_MAX_TEXT_LINES:
        return False
    return True


def _write_source_snapshot_from_crop_plan(
    *,
    snapshot_root: Path,
    source_file_path: str,
    cache_token: str,
    cache_key_hint: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    pdf_document,
    crop_plan: list[tuple[int, tuple[float, float, float, float] | None]],
    max_height_px: int | None = None,
    max_width_px: int = 1600,
) -> tuple[Path | None, dict[str, Any]]:
    plan = [(page_index, bbox) for page_index, bbox in crop_plan if bbox is not None]
    source_path = Path(str(source_file_path or "").strip())
    if pdf_document is None or not plan or not source_path.exists():
        return None, {}
    page_numbers = [page_index + 1 for page_index, _ in plan]
    signature_payload = [
        _SECTION_WINDOW_SNAPSHOT_SIGNATURE_VERSION,
        cache_token,
        cache_key_hint,
        *(
            f"{page_index}:{bbox[0]:.1f},{bbox[1]:.1f},{bbox[2]:.1f},{bbox[3]:.1f}"
            for page_index, bbox in plan
        ),
    ]
    signature = hashlib.sha1("|".join(signature_payload).encode("utf-8")).hexdigest()[:10]
    cache_key = (str(source_path.resolve()), page_numbers[0], page_numbers[-1], signature)
    if cache_key in source_snapshot_cache:
        return source_snapshot_cache[cache_key], _summarize_pdf_crop_plan_features(pdf_document, plan)

    snapshot_root.mkdir(parents=True, exist_ok=True)
    seed = "|".join([cache_key[0], str(cache_key[1]), str(cache_key[2]), cache_key[3]])
    target = snapshot_root / f"src_{hashlib.sha1(seed.encode('utf-8')).hexdigest()[:16]}.png"
    meta = _summarize_pdf_crop_plan_features(pdf_document, plan)
    if _snapshot_file_is_usable(target):
        source_snapshot_cache[cache_key] = target
        return target, meta
    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass

    try:
        images = _render_pdf_crop_plan(
            pdf_document,
            plan,
            max_width_px=max(1, int(max_width_px or 1600)),
            max_scale=5.0,
        )
        rendered = _compose_vertical_images(
            images,
            max_width_px=max(1, int(max_width_px or 1600)),
            max_height_px=max_height_px,
        )
        _write_png_atomically(target, rendered)
    except Exception:
        source_snapshot_cache[cache_key] = None
        return None, {}

    source_snapshot_cache[cache_key] = target
    return target, meta


def _collect_section_visual_band_plans(
    document,
    *,
    section_window_meta: dict[str, Any],
) -> list[list[tuple[int, tuple[float, float, float, float] | None]]]:
    plan = list(section_window_meta.get("plan") or [])
    band_plans: list[list[tuple[int, tuple[float, float, float, float] | None]]] = []
    for page_index, bbox in plan:
        if bbox is None:
            continue
        page = document.load_page(page_index)
        clip = fitz.Rect(*bbox)
        rects: list[Any] = []
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
            if not clip.intersects(block_rect):
                continue
            intersection = clip & block_rect
            if _is_significant_visual_rect(intersection):
                rects.append(intersection)
        for drawing in page.get_drawings() or []:
            rect = drawing.get("rect") or drawing.get("bbox")
            if rect is None:
                continue
            try:
                drawing_rect = fitz.Rect(rect)
            except (TypeError, ValueError):
                continue
            if not clip.intersects(drawing_rect):
                continue
            intersection = clip & drawing_rect
            if _drawing_counts_as_semantic_visual(drawing, intersection):
                rects.append(intersection)
        rects.sort(key=lambda item: (float(item.y0), float(item.x0)))
        merged: list[Any] = []
        for rect in rects:
            if not merged:
                merged.append(rect)
                continue
            previous = merged[-1]
            if float(rect.y0) <= float(previous.y1) + _VISUAL_BAND_MAX_GAP:
                merged[-1] = fitz.Rect(
                    min(float(previous.x0), float(rect.x0)),
                    min(float(previous.y0), float(rect.y0)),
                    max(float(previous.x1), float(rect.x1)),
                    max(float(previous.y1), float(rect.y1)),
                )
                continue
            merged.append(rect)
        for rect in merged:
            window_top = max(float(clip.y0), float(rect.y0) - 6.0)
            window_bottom = min(float(clip.y1), float(rect.y1) + 6.0)
            content_bboxes = _collect_anchor_window_content_bboxes(
                page,
                window_top=window_top,
                window_bottom=window_bottom,
                include_drawings=True,
            )
            crop_bbox = _expand_section_window_crop_bbox(page, content_bboxes)
            if crop_bbox is None:
                crop_bbox = (
                    max(0.0, float(rect.x0) - 10.0),
                    max(0.0, float(rect.y0) - 8.0),
                    min(float(page.rect.width), float(rect.x1) + 10.0),
                    min(float(page.rect.height), float(rect.y1) + 8.0),
                )
            if crop_bbox[3] <= crop_bbox[1] + 24.0:
                continue
            band_plans.append([(page_index, crop_bbox)])
    return band_plans


def _build_multi_visual_snapshot_refs(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    pdf_document,
    section_window_meta: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if pdf_document is None or not section_window_meta:
        return [], []
    cue_lines = [line for line in _compact_snapshot_fallback_lines(row, suffix="visual_fallback")[1:] if VISUAL_SNAPSHOT_CUE_RE.search(line)]
    drawing_count = int(section_window_meta.get("drawing_count") or 0)
    if len(cue_lines) < 2 and drawing_count < 6:
        return [], []
    band_plans = _collect_section_visual_band_plans(pdf_document, section_window_meta=section_window_meta)
    if len(band_plans) < 2:
        return [], []
    refs: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    base_title = _snapshot_fallback_title(row, suffix="visual_fallback")
    for index, crop_plan in enumerate(band_plans[:3]):
        title = cue_lines[index] if index < len(cue_lines) else f"{base_title} 图{index + 1}"
        source_snapshot_path, source_snapshot_meta = _write_source_snapshot_from_crop_plan(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            cache_token="preview-fallback-band",
            cache_key_hint=f"{row.get('section_instance_key') or row.get('section_key') or row.get('entry_id') or base_title}:{index}",
            source_snapshot_cache=source_snapshot_cache,
            pdf_document=pdf_document,
            crop_plan=crop_plan,
        )
        if not source_snapshot_path:
            return [], []
        table_id = _build_table_id(row, f"preview-fallback::visual-band::{index}", index)
        ai_projection = _build_ai_projection(
            table_id=table_id,
            title=title,
            headers=[],
            rows=[],
            notes=["visual_fallback"],
            render_mode="snapshot_only",
        )
        source_page = min(page_index + 1 for page_index, _ in crop_plan)
        source_page_end = max(page_index + 1 for page_index, _ in crop_plan)
        ref = _normalize_table_ref(
            {
                "table_id": table_id,
                "row_range": [],
                "row_ids": [],
                "table_title": title,
                "inherited_headers": [],
                "slice_rows": [],
                "inherited_group_title": "",
                "inherited_notes": ["visual_fallback"],
                "slice_ai_projection": ai_projection,
                "slice_snapshot_path": str(source_snapshot_path),
                "source_snapshot_path": str(source_snapshot_path),
                "slice_render_key": table_id,
                "slice_render_mode": "snapshot_only",
                "source_page": source_page,
                "source_page_end": source_page_end,
                "structure_confidence": 0.45,
                "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
                "export_variant_locked": True,
                "export_variant": "source",
            }
        )
        asset = {
            "table_id": table_id,
            "document_id": document_id,
            "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
            "page_span": {"start": source_page, "end": source_page_end},
            "table_title": title,
            "headers": [],
            "rows": [],
            "group_labels": [],
            "notes": ["visual_fallback"],
            "render_mode": "snapshot_only",
            "snapshot_path": str(source_snapshot_path),
            "source_snapshot_path": str(source_snapshot_path),
            "ai_projection": ai_projection,
            "confidence": 0.45,
            "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
        }
        refs.append(ref)
        assets.append(asset)
    return refs, assets


def _content_blocks_for_multi_visual_fallback(
    row: dict[str, Any],
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact_lines = _compact_snapshot_fallback_lines(row, suffix="visual_fallback")
    if not compact_lines:
        return [{"kind": "table", "table_id": str(ref.get("table_id") or "")} for ref in refs]
    cue_lines = [line for line in compact_lines[1:] if VISUAL_SNAPSHOT_CUE_RE.search(line)]
    blocks: list[dict[str, Any]] = [{"kind": "paragraph", "text": compact_lines[0]}]
    if cue_lines and len(cue_lines) >= len(refs):
        for index, ref in enumerate(refs):
            blocks.append({"kind": "paragraph", "text": cue_lines[index]})
            blocks.append({"kind": "table", "table_id": str(ref.get("table_id") or "")})
        return blocks
    for line in compact_lines[1:2]:
        blocks.append({"kind": "paragraph", "text": line})
    for ref in refs:
        blocks.append({"kind": "table", "table_id": str(ref.get("table_id") or "")})
    return blocks


def _build_full_page_snapshot_ref(
    row: dict[str, Any],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    suffix: str,
    pdf_document=None,
    source_key: str = "",
    section_window_index: dict[str, dict[str, Any]] | None = None,
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] | None = None,
    section_window_meta_cache: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    source_page = _to_int(row.get("source_page"))
    source_page_end = _to_int(row.get("source_page_end")) or source_page
    if source_page <= 0 or not source_file_path:
        return None, None
    title = _snapshot_fallback_title(row, suffix=suffix)
    table_id = _build_table_id(row, f"preview-fallback::{suffix}", 0)
    source_snapshot_meta: dict[str, Any] = {}
    section_window_meta = None
    if (
        pdf_document is not None
        and section_window_index is not None
        and line_detection_cache is not None
        and section_window_meta_cache is not None
    ):
        section_window_meta = _get_section_window_snapshot_meta(
            row,
            pdf_document=pdf_document,
            source_key=source_key,
            section_window_index=section_window_index,
            line_detection_cache=line_detection_cache,
            section_window_meta_cache=section_window_meta_cache,
        )
    source_snapshot_path = None
    if section_window_meta and section_window_meta.get("plan"):
        source_snapshot_path, source_snapshot_meta = _write_section_window_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=source_page,
            source_page_end=source_page_end,
            cache_token="preview-fallback",
            cache_key_hint=f"{suffix}:{row.get('section_instance_key') or row.get('section_key') or row.get('entry_id') or title}",
            source_snapshot_cache=source_snapshot_cache,
            pdf_document=pdf_document,
            section_window_meta=section_window_meta,
        )
    if not source_snapshot_path:
        source_snapshot_path, source_snapshot_meta = _write_full_page_span_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=source_page,
            source_page_end=source_page_end,
            cache_token="preview-fallback",
            cache_key_hint=f"{suffix}:{row.get('section_instance_key') or row.get('section_key') or row.get('entry_id') or title}",
            source_snapshot_cache=source_snapshot_cache,
            crop_to_content=True,
        )
    if not source_snapshot_path:
        return None, None
    slice_snapshot_path = source_snapshot_path
    ai_projection = _build_ai_projection(
        table_id=table_id,
        title=title,
        headers=[],
        rows=[],
        notes=[suffix],
        render_mode="snapshot_only",
    )
    ref = _normalize_table_ref(
        {
            "table_id": table_id,
            "row_range": [],
            "row_ids": [],
            "table_title": title,
            "inherited_headers": [],
            "slice_rows": [],
            "inherited_group_title": "",
            "inherited_notes": [suffix],
            "slice_ai_projection": ai_projection,
            "slice_snapshot_path": str(slice_snapshot_path),
            "source_snapshot_path": str(source_snapshot_path),
            "slice_render_key": table_id,
            "slice_render_mode": "snapshot_only",
            "source_page": source_page,
            "source_page_end": source_page_end,
            "structure_confidence": 0.45,
            "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
            "export_variant_locked": True,
            "export_variant": "source",
        }
    )
    asset = {
        "table_id": table_id,
        "document_id": document_id,
        "section_instance_key": str(row.get("section_instance_key") or row.get("section_key") or ""),
        "page_span": {"start": source_page, "end": source_page_end},
        "table_title": title,
        "headers": [],
        "rows": [],
        "group_labels": [],
        "notes": [suffix],
        "render_mode": "snapshot_only",
        "snapshot_path": str(source_snapshot_path),
        "source_snapshot_path": str(source_snapshot_path),
        "ai_projection": ai_projection,
        "confidence": 0.45,
        "source_entry_id": str(row.get("entry_id") or row.get("req_code") or ""),
        "source_image_block_count": int(source_snapshot_meta.get("image_block_count") or 0),
    }
    return ref, asset


def _build_structured_table_source_snapshot(
    row: dict[str, Any],
    *,
    snapshot_root: Path,
    source_file_path: str,
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    pdf_document,
    section_window_meta: dict[str, Any] | None,
) -> tuple[str, int]:
    source_path = Path(str(source_file_path or "").strip())
    if not source_path.exists():
        return "", 0

    source_page = _to_int(row.get("source_page"))
    source_page_end = _to_int(row.get("source_page_end")) or source_page
    cache_key_hint = str(
        row.get("section_instance_key")
        or row.get("section_key")
        or row.get("entry_id")
        or row.get("req_code")
        or row.get("section_code")
        or "structured-table"
    ).strip()
    snapshot_path = None
    snapshot_meta: dict[str, Any] = {}

    if pdf_document is not None and section_window_meta and section_window_meta.get("plan"):
        snapshot_path, snapshot_meta = _write_source_snapshot_from_crop_plan(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            cache_token="preview-structured-source",
            cache_key_hint=cache_key_hint,
            source_snapshot_cache=source_snapshot_cache,
            pdf_document=pdf_document,
            crop_plan=list(section_window_meta.get("plan") or []),
        )
    if snapshot_path is None and source_page > 0:
        snapshot_path, snapshot_meta = _write_full_page_span_source_snapshot(
            snapshot_root=snapshot_root,
            source_file_path=source_file_path,
            source_page=source_page,
            source_page_end=source_page_end,
            cache_token="preview-structured-source",
            cache_key_hint=cache_key_hint,
            source_snapshot_cache=source_snapshot_cache,
            crop_to_content=True,
        )
    if snapshot_path is None:
        return "", 0
    return str(snapshot_path), int(snapshot_meta.get("image_block_count") or 0)


def _apply_preview_fallback_assets(
    entries: list[dict[str, Any]],
    *,
    document_id: str,
    snapshot_root: Path,
    source_file_path: str,
    parser_diagnostics: dict[str, Any],
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None],
    sections: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not entries:
        return entries, []

    updated = [dict(row) for row in entries]
    extra_assets: list[dict[str, Any]] = []
    sections_with_non_heading_rows = {
        str(row.get("section_instance_key") or row.get("section_key") or row.get("section_code") or idx)
        for idx, row in enumerate(updated)
        if str(row.get("entry_kind") or "").strip().upper() != "HEADING"
    }
    section_window_index = _build_section_window_index(sections or [])
    source_path = Path(str(source_file_path or "").strip())
    can_inspect_pdf = fitz is not None and source_path.exists() and source_path.suffix.lower() == ".pdf"
    source_key = str(source_path.resolve()) if can_inspect_pdf else ""
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
    section_window_meta_cache: dict[str, dict[str, Any]] = {}
    document_cm = fitz.open(str(source_path)) if can_inspect_pdf else nullcontext(None)

    with document_cm as pdf_document:
        for idx, row in enumerate(updated):
            row = _sanitize_row_table_refs(row)
            row = _strip_synthetic_local_clause_headers(row)
            row_key = str(row.get("section_instance_key") or row.get("section_key") or row.get("section_code") or idx)
            row_kind = str(row.get("entry_kind") or "").strip().upper()
            section_window_meta = (
                _get_section_window_snapshot_meta(
                    row,
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if pdf_document is not None
                else None
            )
            skip_heading_fallback = row_kind == "HEADING" and row_key in sections_with_non_heading_rows
            structured_source_snapshot_path = ""
            structured_source_image_block_count = 0
            if row_kind != "HEADING":
                structured_source_snapshot_path, structured_source_image_block_count = _build_structured_table_source_snapshot(
                    row,
                    snapshot_root=snapshot_root,
                    source_file_path=source_file_path,
                    source_snapshot_cache=source_snapshot_cache,
                    pdf_document=pdf_document,
                    section_window_meta=section_window_meta,
                )
            if not skip_heading_fallback:
                rebuilt_row, rebuilt_assets = _build_layout_table_structured_preview(
                    row,
                    document_id=document_id,
                    snapshot_root=snapshot_root,
                    source_file_path=source_file_path,
                    source_snapshot_cache=source_snapshot_cache,
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_meta=section_window_meta,
                    line_detection_cache=line_detection_cache,
                    source_snapshot_path=structured_source_snapshot_path,
                    source_image_block_count=structured_source_image_block_count,
                )
                if rebuilt_row is not None:
                    updated[idx] = rebuilt_row
                    extra_assets.extend(rebuilt_assets)
                    continue
            repaired_row, repaired_assets = _repair_local_clause_pipe_table_preview(
                row,
                document_id=document_id,
                snapshot_root=snapshot_root,
                source_snapshot_path=structured_source_snapshot_path,
                source_image_block_count=structured_source_image_block_count,
            )
            if repaired_row is not None:
                updated[idx] = repaired_row
                extra_assets.extend(repaired_assets)
                continue
            if _row_prefers_visual_snapshot_over_structured_refs(
                row,
                section_window_meta=section_window_meta,
            ):
                ref, asset = _build_full_page_snapshot_ref(
                    row,
                    document_id=document_id,
                    snapshot_root=snapshot_root,
                    source_file_path=source_file_path,
                    source_snapshot_cache=source_snapshot_cache,
                    suffix="visual_override",
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if ref and asset:
                    row["table_refs"] = [ref]
                    row["content_blocks"] = _content_blocks_for_snapshot_fallback(
                        row,
                        str(ref.get("table_id") or ""),
                        suffix="visual_override",
                    )
                    row["has_table_asset"] = True
                    row["table_render_mode"] = "snapshot_only"
                    updated[idx] = row
                    extra_assets.append(asset)
                    continue
            if not skip_heading_fallback and _row_needs_full_page_table_fallback(row):
                ref, asset = _build_full_page_snapshot_ref(
                    row,
                    document_id=document_id,
                    snapshot_root=snapshot_root,
                    source_file_path=source_file_path,
                    source_snapshot_cache=source_snapshot_cache,
                    suffix="table_fallback",
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if ref and asset:
                    row["table_refs"] = [ref]
                    row["content_blocks"] = _content_blocks_for_snapshot_fallback(
                        row,
                        str(ref.get("table_id") or ""),
                        suffix="table_fallback",
                    )
                    row["has_table_asset"] = True
                    row["table_render_mode"] = "snapshot_only"
                    updated[idx] = _sanitize_row_table_refs(row)
                    extra_assets.append(asset)
                    continue
            if not skip_heading_fallback and _row_needs_layout_table_snapshot_fallback(
                row,
                section_window_meta=section_window_meta,
            ):
                ref, asset = _build_full_page_snapshot_ref(
                    row,
                    document_id=document_id,
                    snapshot_root=snapshot_root,
                    source_file_path=source_file_path,
                    source_snapshot_cache=source_snapshot_cache,
                    suffix="layout_table_fallback",
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if ref and asset:
                    row["table_refs"] = [ref]
                    row["content_blocks"] = _content_blocks_for_snapshot_fallback(
                        row,
                        str(ref.get("table_id") or ""),
                        suffix="layout_table_fallback",
                    )
                    row["has_table_asset"] = True
                    row["table_render_mode"] = "snapshot_only"
                    updated[idx] = row
                    extra_assets.append(asset)
                    continue
            updated[idx] = row

        for idx, row in enumerate(updated):
            section_window_meta = (
                _get_section_window_snapshot_meta(
                    row,
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if pdf_document is not None
                else None
            )
            repaired_row, repaired_assets = _repair_row_explicit_visual_assets(
                row,
                document_id=document_id,
                snapshot_root=snapshot_root,
                source_file_path=source_file_path,
                source_snapshot_cache=source_snapshot_cache,
                pdf_document=pdf_document,
                source_key=source_key,
                section_window_meta=section_window_meta,
                line_detection_cache=line_detection_cache,
            )
            if repaired_row is None:
                continue
            updated[idx] = repaired_row
            extra_assets.extend(repaired_assets)

        section_has_non_heading_assets: set[str] = set()
        for idx, row in enumerate(updated):
            if str(row.get("entry_kind") or "").strip().upper() == "HEADING":
                continue
            if row.get("table_refs") or row.get("visual_refs"):
                section_has_non_heading_assets.add(
                    str(row.get("section_instance_key") or row.get("section_key") or row.get("section_code") or idx)
                )

        visual_targets: dict[str, int] = {}
        for idx, row in enumerate(updated):
            section_window_meta = (
                _get_section_window_snapshot_meta(
                    row,
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if pdf_document is not None
                else None
            )
            if not _row_needs_visual_snapshot_fallback(
                row,
                parser_diagnostics,
                section_window_meta=section_window_meta,
            ):
                continue
            key = str(row.get("section_instance_key") or row.get("section_key") or row.get("section_code") or idx)
            if (
                str(row.get("entry_kind") or "").strip().upper() == "HEADING"
                and key in section_has_non_heading_assets
            ):
                continue
            if key not in visual_targets or _visual_row_rank(row) > _visual_row_rank(updated[visual_targets[key]]):
                visual_targets[key] = idx

        for idx in sorted(set(visual_targets.values())):
            row = updated[idx]
            if row.get("table_refs"):
                continue
            section_window_meta = (
                _get_section_window_snapshot_meta(
                    row,
                    pdf_document=pdf_document,
                    source_key=source_key,
                    section_window_index=section_window_index,
                    line_detection_cache=line_detection_cache,
                    section_window_meta_cache=section_window_meta_cache,
                )
                if pdf_document is not None
                else None
            )
            multi_refs, multi_assets = _build_multi_visual_snapshot_refs(
                row,
                document_id=document_id,
                snapshot_root=snapshot_root,
                source_file_path=source_file_path,
                source_snapshot_cache=source_snapshot_cache,
                pdf_document=pdf_document,
                section_window_meta=section_window_meta,
            )
            if multi_refs and multi_assets:
                row["table_refs"] = multi_refs
                row["content_blocks"] = _content_blocks_for_multi_visual_fallback(row, multi_refs)
                row["has_table_asset"] = True
                row["table_render_mode"] = "snapshot_only"
                updated[idx] = row
                extra_assets.extend(multi_assets)
                continue
            ref, asset = _build_full_page_snapshot_ref(
                row,
                document_id=document_id,
                snapshot_root=snapshot_root,
                source_file_path=source_file_path,
                source_snapshot_cache=source_snapshot_cache,
                suffix="visual_fallback",
                pdf_document=pdf_document,
                source_key=source_key,
                section_window_index=section_window_index,
                line_detection_cache=line_detection_cache,
                section_window_meta_cache=section_window_meta_cache,
            )
            if ref and asset:
                row["table_refs"] = [ref]
                row["content_blocks"] = _content_blocks_for_snapshot_fallback(
                    row,
                    str(ref.get("table_id") or ""),
                    suffix="visual_fallback",
                )
                row["has_table_asset"] = True
                row["table_render_mode"] = "snapshot_only"
                updated[idx] = row
                extra_assets.append(asset)

    return updated, extra_assets


def _scope_identity(scope: str) -> str:
    value = normalize_scope_text(normalize_test_outline_scope(scope))
    if not value:
        return MAIN_SCOPE_ID
    if re.fullmatch(r"SUB\d+", value, re.I):
        return value.upper()
    if re.fullmatch(r"[A-Z]", value, re.I):
        return value.upper()
    return value.upper()


def _scope_title_candidate(
    *,
    raw_scope: str,
    section_code: str = "",
    title: str = "",
    is_marker: bool = False,
) -> str:
    normalized_scope = normalize_scope_text(normalize_test_outline_scope(raw_scope))
    normalized_title = normalize_scope_text(title)
    if is_marker and normalized_title:
        return normalized_title
    if not section_code and normalized_title:
        return normalized_title
    if normalized_scope and not re.fullmatch(r"(?:SUB\d+|[A-Z])", normalized_scope, re.I):
        return normalized_scope
    return ""


def _scope_title_score(title: str, *, raw_scope: str) -> tuple[int, int]:
    candidate = normalize_scope_text(title)
    normalized_scope = normalize_scope_text(raw_scope)
    score = 0
    if candidate:
        score += 1
    if candidate and candidate != normalized_scope:
        score += 2
    if re.search(r"[A-Za-z\u4e00-\u9fff]{2,}", candidate):
        score += 2
    if candidate.startswith(("附件", "附录")):
        score += 1
    if re.fullmatch(r"(?:SUB\d+|[A-Z])", candidate, re.I):
        score -= 2
    return (score, len(candidate))


def _scope_title_rank(title: str, *, raw_scope: str, evidence: str) -> tuple[int, int, int]:
    base_score, length = _scope_title_score(title, raw_scope=raw_scope)
    return (scope_evidence_rank(evidence), base_score, length)


def _build_scope_catalog(
    *,
    section_outline: list[dict[str, Any]],
    appendix_markers: list[dict[str, Any]],
    test_outline_markers: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {
        MAIN_SCOPE_ID: {
            "raw_scope": "",
            "scope_title": MAIN_SCOPE_TITLE,
            "first_page": 0,
            "discovery_order": -1,
            "scope_evidence": APPENDIX_EVIDENCE_EXPLICIT,
            "scope_title_source": "main_body",
        }
    }

    def remember(
        scope: str,
        *,
        title: str = "",
        section_code: str = "",
        page: Any = None,
        is_marker: bool = False,
        discovery_order: int = 0,
        evidence: str = "",
        title_source: str = "",
    ) -> None:
        identity = _scope_identity(scope)
        bucket = buckets.setdefault(
            identity,
            {
                "raw_scope": normalize_scope_text(normalize_test_outline_scope(scope)),
                "scope_title": "",
                "first_page": 10**9,
                "discovery_order": discovery_order,
                "scope_evidence": "",
                "scope_title_source": "",
            },
        )
        if bucket.get("raw_scope") in (None, ""):
            bucket["raw_scope"] = normalize_scope_text(normalize_test_outline_scope(scope))
        page_int = _to_int(page)
        if page_int > 0 and page_int < int(bucket.get("first_page") or 10**9):
            bucket["first_page"] = page_int
        bucket["discovery_order"] = min(int(bucket.get("discovery_order") or discovery_order), discovery_order)
        current_evidence = str(bucket.get("scope_evidence") or "")
        if evidence and scope_evidence_rank(evidence) >= scope_evidence_rank(current_evidence):
            bucket["scope_evidence"] = evidence
            if title_source:
                bucket["scope_title_source"] = title_source
        candidate_title = _scope_title_candidate(
            raw_scope=scope,
            section_code=section_code,
            title=title,
            is_marker=is_marker,
        )
        if not candidate_title:
            return
        current_title = str(bucket.get("scope_title") or "")
        if _scope_title_rank(candidate_title, raw_scope=scope, evidence=evidence) > _scope_title_rank(
            current_title,
            raw_scope=scope,
            evidence=str(bucket.get("scope_evidence") or ""),
        ):
            bucket["scope_title"] = candidate_title
            if title_source:
                bucket["scope_title_source"] = title_source
            if evidence:
                bucket["scope_evidence"] = evidence

    for index, row in enumerate(section_outline):
        remember(
            str(row.get("appendix_scope") or ""),
            title=str(row.get("section_title") or ""),
            section_code=str(row.get("section_code") or ""),
            page=row.get("source_page_start"),
            is_marker=bool(row.get("is_appendix_marker", False)),
            discovery_order=index,
            evidence=str(row.get("appendix_scope_evidence") or APPENDIX_EVIDENCE_HEADING),
            title_source=str(row.get("appendix_title_source") or "section_outline"),
        )
    base_order = len(section_outline)
    for offset, row in enumerate(appendix_markers):
        remember(
            str(row.get("appendix_scope") or ""),
            title=str(row.get("section_title") or ""),
            page=row.get("source_page_start"),
            is_marker=True,
            discovery_order=base_order + offset,
            evidence=str(row.get("appendix_scope_evidence") or APPENDIX_EVIDENCE_EXPLICIT),
            title_source=str(row.get("appendix_title_source") or "appendix_marker"),
        )
    base_order += len(appendix_markers)
    for offset, row in enumerate(test_outline_markers):
        remember(
            str(row.get("appendix_scope") or ""),
            title=str(row.get("section_title") or ""),
            page=row.get("source_page_start"),
            is_marker=True,
            discovery_order=base_order + offset,
            evidence=str(row.get("appendix_scope_evidence") or APPENDIX_EVIDENCE_HEADING),
            title_source=str(row.get("appendix_title_source") or "test_outline_marker"),
        )
    base_order += len(test_outline_markers)
    for offset, row in enumerate(requirements):
        remember(
            str(row.get("appendix_scope") or _extract_appendix_scope(str(row.get("source_anchor") or "")) or ""),
            title=str(row.get("section_title") or ""),
            section_code=str(row.get("section_code") or ""),
            page=row.get("source_page"),
            discovery_order=base_order + offset,
            evidence=str(row.get("appendix_scope_evidence") or ""),
            title_source=str(row.get("appendix_title_source") or "requirement"),
        )

    catalog: dict[str, dict[str, Any]] = {
        MAIN_SCOPE_ID: {
            "scope_id": MAIN_SCOPE_ID,
            "scope_title": MAIN_SCOPE_TITLE,
            "raw_scope": "",
        }
    }
    reserved_sub_numbers: set[int] = set()
    deferred: list[tuple[str, dict[str, Any]]] = []

    for identity, bucket in buckets.items():
        if identity == MAIN_SCOPE_ID:
            continue
        title = normalize_scope_text(bucket.get("scope_title") or bucket.get("raw_scope") or "")
        sub_match = re.fullmatch(r"SUB(\d+)", identity)
        if sub_match:
            number = int(sub_match.group(1))
            reserved_sub_numbers.add(number)
            catalog[identity] = {
                "scope_id": f"SUB{number}",
                "scope_title": title or f"附录 SUB{number}",
                "raw_scope": bucket.get("raw_scope") or identity,
                "scope_evidence": str(bucket.get("scope_evidence") or ""),
                "scope_title_source": str(bucket.get("scope_title_source") or ""),
            }
            continue
        deferred.append((identity, bucket))

    deferred.sort(
        key=lambda item: (
            0 if re.fullmatch(r"[A-Z]", item[0]) else 1,
            ord(item[0]) - ord("A") if re.fullmatch(r"[A-Z]", item[0]) else 10**9,
            int(item[1].get("first_page") or 10**9),
            int(item[1].get("discovery_order") or 10**9),
            item[0],
        )
    )
    next_sub_number = 1
    for identity, bucket in deferred:
        while next_sub_number in reserved_sub_numbers:
            next_sub_number += 1
        reserved_sub_numbers.add(next_sub_number)
        title = normalize_scope_text(bucket.get("scope_title") or bucket.get("raw_scope") or "")
        catalog[identity] = {
            "scope_id": f"SUB{next_sub_number}",
            "scope_title": title or f"附录 SUB{next_sub_number}",
            "raw_scope": bucket.get("raw_scope") or identity,
            "scope_evidence": str(bucket.get("scope_evidence") or ""),
            "scope_title_source": str(bucket.get("scope_title_source") or ""),
        }
        next_sub_number += 1
    return catalog


def _resolve_scope_info(scope_catalog: dict[str, dict[str, Any]], raw_scope: str, *, title: str = "") -> dict[str, str]:
    identity = _scope_identity(raw_scope)
    if identity in scope_catalog:
        info = scope_catalog[identity]
        return {
            "scope_id": str(info.get("scope_id") or MAIN_SCOPE_ID),
            "scope_title": str(info.get("scope_title") or MAIN_SCOPE_TITLE),
            "scope_evidence": str(info.get("scope_evidence") or ""),
            "scope_title_source": str(info.get("scope_title_source") or ""),
        }
    normalized_scope = normalize_scope_id(normalize_test_outline_scope(raw_scope))
    if normalized_scope == MAIN_SCOPE_ID:
        return {
            "scope_id": MAIN_SCOPE_ID,
            "scope_title": MAIN_SCOPE_TITLE,
            "scope_evidence": APPENDIX_EVIDENCE_EXPLICIT,
            "scope_title_source": "main_body",
        }
    fallback_title = _scope_title_candidate(raw_scope=raw_scope, title=title) or normalize_scope_text(title) or normalized_scope
    return {
        "scope_id": normalized_scope,
        "scope_title": fallback_title,
        "scope_evidence": "",
        "scope_title_source": "",
    }


def _apply_scope_catalog_to_row(
    row: dict[str, Any],
    *,
    scope_catalog: dict[str, dict[str, Any]],
    is_marker: bool = False,
) -> dict[str, Any]:
    mapped = dict(row)
    scope_info = _resolve_scope_info(
        scope_catalog,
        str(row.get("appendix_scope") or ""),
        title=str(row.get("section_title") or ""),
    )
    mapped["appendix_scope"] = scope_info["scope_id"]
    mapped["appendix_title"] = scope_info["scope_title"]
    mapped["appendix_scope_evidence"] = str(mapped.get("appendix_scope_evidence") or scope_info.get("scope_evidence") or "")
    mapped["appendix_title_source"] = str(mapped.get("appendix_title_source") or scope_info.get("scope_title_source") or "")
    if is_marker:
        mapped["is_appendix_marker"] = True
    return mapped


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
    appendix_markers = _collect_appendix_markers(segments)
    test_outline_markers = _collect_test_outline_markers(segments)
    scope_catalog = _build_scope_catalog(
        section_outline=raw_section_outline,
        appendix_markers=appendix_markers,
        test_outline_markers=test_outline_markers,
        requirements=requirements,
    )
    raw_section_outline = [
        _apply_scope_catalog_to_row(row, scope_catalog=scope_catalog, is_marker=bool(row.get("is_appendix_marker", False)))
        for row in raw_section_outline
    ]
    appendix_markers = [_apply_scope_catalog_to_row(row, scope_catalog=scope_catalog, is_marker=True) for row in appendix_markers]
    test_outline_markers = [
        _apply_scope_catalog_to_row(row, scope_catalog=scope_catalog, is_marker=True) for row in test_outline_markers
    ]
    section_consistency = analyze_section_consistency(raw_section_outline)
    # Strict original-order mode: do not inject synthetic marker rows into section flow.
    section_outline = [
        {
            **dict(row),
            "is_appendix_marker": False,
        }
        for row in raw_section_outline
    ]

    req_rows: list[dict[str, Any]] = []
    text_lengths: list[int] = []
    req_pages: set[int] = set()
    max_page = 0

    for item in requirements:
        source_page = _to_int(item.get("source_page"))
        source_page_end = _to_int(item.get("source_page_end")) or source_page
        raw_scope = str(item.get("appendix_scope") or _extract_appendix_scope(item.get("source_anchor", "")) or "")
        scope_info = _resolve_scope_info(scope_catalog, raw_scope, title=str(item.get("section_title") or ""))
        appendix_scope = scope_info["scope_id"]
        explicit_section_code = _extract_explicit_section_code(item.get("source_anchor", ""))
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
            explicit_section_code=explicit_section_code,
            text=text,
            req_order_start=len(req_rows),
        )
        for row in split_rows:
            row["appendix_title"] = scope_info["scope_title"]
            req_pages.update(page for page in range(row["source_page"], row["source_page_end"] + 1) if page > 0)
            if row["source_page_end"] > max_page:
                max_page = row["source_page_end"]
            row["appendix_scope_evidence"] = str(item.get("appendix_scope_evidence") or scope_info.get("scope_evidence") or "")
            row["appendix_title_source"] = str(item.get("appendix_title_source") or scope_info.get("scope_title_source") or "")
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
            scope_catalog=scope_catalog,
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
                "appendix_title": row.get("appendix_title", ""),
                "appendix_scope_evidence": row.get("appendix_scope_evidence", ""),
                "appendix_title_source": row.get("appendix_title_source", ""),
                "section_code": row.get("section_code", ""),
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "parent_section_code": row.get("structural_parent_section_code", ""),
                "parent_section_key": row.get("structural_parent_section_key", ""),
                "section_label": row.get("section_label", ""),
                "is_scoped_local_clause": bool(row.get("is_scoped_local_clause", False)),
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
        if not state.get("appendix_title") and row.get("appendix_title"):
            state["appendix_title"] = row.get("appendix_title")
        if not state.get("appendix_scope_evidence") and row.get("appendix_scope_evidence"):
            state["appendix_scope_evidence"] = row.get("appendix_scope_evidence")
        if not state.get("appendix_title_source") and row.get("appendix_title_source"):
            state["appendix_title_source"] = row.get("appendix_title_source")
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
                "appendix_title": str(node.get("appendix_title") or req_state.get("appendix_title") or ""),
                "appendix_scope_evidence": str(node.get("appendix_scope_evidence") or req_state.get("appendix_scope_evidence") or ""),
                "appendix_title_source": str(node.get("appendix_title_source") or req_state.get("appendix_title_source") or ""),
                "section_code": code,
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "parent_section_key": _join_section_key(appendix_scope, str(node.get("parent_section_code") or "")),
                "section_title": node.get("section_title", "") or req_state.get("section_title", ""),
                "section_label": req_state.get("section_label", ""),
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
                "is_scoped_local_clause": bool(req_state.get("is_scoped_local_clause", False)),
            }
        )

    # Keep visibility for any requirement section not captured in outline.
    for section_instance_key, req_state in section_counter.items():
        if section_instance_key in outline_instance_index:
            continue
        if not _should_surface_scoped_local_clause_as_section(req_state):
            continue
        section_key = str(req_state.get("section_key") or "")
        appendix_scope = str(req_state.get("appendix_scope") or "").strip().upper()
        code = str(req_state.get("section_code") or "").strip()
        if not code:
            _, code = _split_section_key(section_key)
        sections.append(
            {
                "appendix_scope": appendix_scope,
                "appendix_title": str(req_state.get("appendix_title") or ""),
                "appendix_scope_evidence": str(req_state.get("appendix_scope_evidence") or ""),
                "appendix_title_source": str(req_state.get("appendix_title_source") or ""),
                "section_code": code,
                "section_key": section_key,
                "section_instance_key": section_instance_key,
                "section_title": req_state.get("section_title", ""),
                "section_label": req_state.get("section_label", ""),
                "section_level": code.count(".") + 1 if code else 0,
                "parent_section_code": str(req_state.get("parent_section_code") or _parent_section_code(code)),
                "parent_section_key": str(req_state.get("parent_section_key") or ""),
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
                "is_scoped_local_clause": bool(req_state.get("is_scoped_local_clause", False)),
            }
        )

    sections, _ = _build_canonical_section_rows(sections)
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
    complete_entries_raw = _repair_preview_clause_rows_from_pdf(
        complete_entries_raw,
        sections=sections,
        source_file_path=file_path,
    )
    complete_entries = _repair_preview_clause_rows_from_pdf(
        complete_entries,
        sections=sections,
        source_file_path=file_path,
    )
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
    section_tree, section_tree_stats = _build_canonical_section_rows(
        sections,
        include_scope_roots=True,
        exclude_complex_table_mode=True,
    )
    table_snapshot_root = snapshot_root or _default_table_snapshot_root(file_path)
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
    source_snapshot_cache: dict[tuple[str, int, int, str], Path | None] = {}
    complete_entries_raw, extra_raw_table_assets = _apply_preview_fallback_assets(
        complete_entries_raw,
        document_id=table_document_id,
        snapshot_root=table_snapshot_root,
        source_file_path=file_path,
        parser_diagnostics=parsed.get("diagnostics", {}) or {},
        source_snapshot_cache=source_snapshot_cache,
        sections=sections,
    )
    complete_entries, extra_deduped_table_assets = _apply_preview_fallback_assets(
        complete_entries,
        document_id=table_document_id,
        snapshot_root=table_snapshot_root,
        source_file_path=file_path,
        parser_diagnostics=parsed.get("diagnostics", {}) or {},
        source_snapshot_cache=source_snapshot_cache,
        sections=sections,
    )
    table_assets = _dedupe_table_assets(
        (deduped_table_assets or raw_table_assets)
        + extra_raw_table_assets
        + extra_deduped_table_assets
    )

    diagnostics = parsed.get("diagnostics", {}) or {}
    page_details = diagnostics.get("page_details", []) if isinstance(diagnostics, dict) else []
    quality_scores = [
        float(detail.get("quality_score", 0.0)) for detail in page_details if isinstance(detail, dict)
    ]
    complete_requirement_entries_count = sum(
        1 for row in complete_entries if str(row.get("entry_kind")) == "REQUIREMENT"
    )
    heading_entries_count = sum(1 for row in complete_entries if str(row.get("entry_kind")) == "HEADING")
    hybrid_entries_count = sum(1 for row in complete_entries if str(row.get("entry_kind")) == "HYBRID")

    summary = {
        "requirements_count": complete_requirement_entries_count,
        "raw_requirement_rows_count": len(req_rows),
        "complete_entries_count": len(complete_entries),
        "complete_entries_raw_count": len(complete_entries_raw),
        "complete_requirement_entries_count": complete_requirement_entries_count,
        "heading_entries_count": heading_entries_count,
        "hybrid_entries_count": hybrid_entries_count,
        "sections_count": sum(1 for row in sections if counts_toward_fidelity_section(row)),
        "section_outline_count": len(raw_section_outline),
        "section_tree_count": len(section_tree),
        "appendix_marker_count": len(appendix_markers),
        "test_outline_marker_count": len(test_outline_markers),
        "section_consistency_issues_total": section_consistency.get("total_issues", 0),
        "section_consistency_duplicate_code_count": section_consistency.get("duplicate_code_count", 0),
        "section_consistency_source_duplicate_code_count": section_consistency.get("source_duplicate_code_count", 0),
        "section_consistency_missing_scope_root_count": section_consistency.get("missing_scope_root_count", 0),
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
    explicit_section_code: str,
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
    explicit_parent_code = str(explicit_section_code or "").strip()
    explicit_parent_key = _join_section_key(appendix_scope, explicit_parent_code)
    for idx, chunk in enumerate(chunks):
        clause_code, clause_title = _extract_local_clause_code_and_title(chunk)
        has_explicit_local_clause = bool(clause_code)
        promote_clause_section = _should_promote_local_clause_to_section(
            parent_section_code=section_code,
            clause_code=clause_code,
            allow_same_code=has_explicit_local_clause,
        )
        chunk_section_code = clause_code if promote_clause_section else section_code
        chunk_section_title = (clause_title if promote_clause_section else "") or section_title_hint
        scoped_local_clause = _should_scope_local_clause_to_parent(
            parent_section_code=explicit_parent_code,
            clause_code=chunk_section_code,
            allow_same_code=has_explicit_local_clause,
        )
        if scoped_local_clause and explicit_parent_key:
            chunk_section_key = _join_scoped_local_clause_key(explicit_parent_key, chunk_section_code)
            chunk_parent_section_code = _scoped_local_clause_parent_code(
                parent_section_code=explicit_parent_code,
                clause_code=chunk_section_code,
            )
            chunk_parent_section_key = _scoped_local_clause_parent_key(
                parent_section_key=explicit_parent_key,
                clause_code=chunk_section_code,
            )
        else:
            chunk_section_key = _join_section_key(appendix_scope, chunk_section_code)
            if not chunk_section_key and chunk_section_title:
                chunk_section_key = _join_title_section_key(appendix_scope, chunk_section_title)
            chunk_parent_section_code = _parent_section_code(chunk_section_code)
            chunk_parent_section_key = _join_section_key(appendix_scope, chunk_parent_section_code)
        chunk_anchor = str(item.get("source_anchor") or "")
        if clause_code and promote_clause_section:
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
                "section_label": _format_clause_section_label(chunk_section_code, clause_code=clause_code),
                "owner_section_code": chunk_section_code,
                "owner_section_title": chunk_section_title,
                "owner_section_label": _format_owner_section_label(
                    chunk_section_code,
                    chunk_section_title,
                    _format_clause_section_label(chunk_section_code, clause_code=clause_code),
                ),
                "requirement_type": item.get("requirement_type", ""),
                "priority": item.get("priority", ""),
                "text_length": len(cleaned_chunk),
                "requirement_text": cleaned_chunk,
                "req_order": req_order_start + idx,
                "source_line_index": _to_int(item.get("source_line_index")),
                "source_line_index_end": _to_int(item.get("source_line_index_end")),
                "section_instance_key": "",
                "structural_parent_section_code": chunk_parent_section_code,
                "structural_parent_section_key": chunk_parent_section_key,
                "structural_parent_section_title": "",
                "structural_parent_section_label": "",
                "is_scoped_local_clause": scoped_local_clause,
            }
        )
    return rows


def _update_preview_requirement_row_text(row: dict[str, Any], text: str) -> dict[str, Any]:
    updated = dict(row)
    cleaned = str(text or "").strip()
    updated["requirement_text"] = cleaned
    updated["display_text"] = cleaned
    updated["raw_text"] = cleaned
    updated["text_length"] = len(cleaned)
    updated["content_blocks"] = [{"kind": "paragraph", "text": cleaned}] if cleaned else []
    return updated


def _recover_clause_sequence_from_line_specs(
    line_specs: list[dict[str, Any]],
    *,
    first_numeric_label: str,
) -> list[dict[str, str]]:
    recovered: list[dict[str, str]] = []
    current: dict[str, Any] | None = None
    started = False
    for spec in line_specs:
        text = _normalize_layout_table_text(spec.get("text") or "")
        if not text:
            continue
        clause_match = EXPLICIT_CLAUSE_LINE_RE.match(text)
        if clause_match:
            label = str(clause_match.group(1) or "").strip()
            body = str(clause_match.group(2) or "").strip()
            if not started:
                if label != first_numeric_label:
                    continue
                started = True
            if current is not None:
                recovered.append(
                    {
                        "label": str(current.get("label") or ""),
                        "text": "\n".join(str(line).strip() for line in current.get("lines") or [] if str(line).strip()),
                    }
                )
            current = {"label": label, "lines": [text]}
            continue
        if not started:
            continue
        if re.match(r"^\d+(?:\.\d+){1,}\s+\S+", text):
            break
        if current is None:
            continue
        current.setdefault("lines", []).append(text)
    if current is not None:
        recovered.append(
            {
                "label": str(current.get("label") or ""),
                "text": "\n".join(str(line).strip() for line in current.get("lines") or [] if str(line).strip()),
            }
        )
    return [item for item in recovered if str(item.get("label") or "").strip() and str(item.get("text") or "").strip()]


def _build_preview_alpha_clause_row(
    *,
    base_row: dict[str, Any],
    parent_section_code: str,
    parent_section_key: str,
    parent_section_title: str,
    label: str,
    text: str,
    sequence: int,
) -> dict[str, Any]:
    copied = _update_preview_requirement_row_text(base_row, text)
    suffix = re.sub(r"[^A-Za-z0-9]+", "", str(label or "").strip()) or str(sequence)
    base_req_code = str(base_row.get("req_code") or base_row.get("entry_id") or "REQ").strip()
    copied["req_code"] = f"{base_req_code}-preview-{suffix.lower()}"
    copied["entry_id"] = copied["req_code"]
    copied["section_code"] = parent_section_code
    copied["section_key"] = parent_section_key
    copied["section_instance_key"] = parent_section_key
    copied["section_title"] = parent_section_title
    copied["section_label"] = f"{label}）"
    copied["owner_section_code"] = parent_section_code
    copied["owner_section_title"] = parent_section_title
    copied["owner_section_label"] = _format_owner_section_label(parent_section_code, parent_section_title)
    copied["parent_section_code"] = parent_section_code
    copied["parent_section_title"] = parent_section_title
    copied["parent_section_label"] = _format_section_label(parent_section_code, parent_section_title)
    copied["structural_parent_section_code"] = parent_section_code
    copied["structural_parent_section_key"] = parent_section_key
    copied["structural_parent_section_title"] = parent_section_title
    copied["structural_parent_section_label"] = _format_owner_section_label(parent_section_code, parent_section_title)
    copied["is_scoped_local_clause"] = False
    copied["source_anchor"] = f"{str(base_row.get('source_anchor') or '').strip()} | Clause {label}".strip()
    copied["table_refs"] = []
    copied["has_table_asset"] = False
    copied["table_render_mode"] = ""
    return copied


def _repair_preview_clause_rows_from_pdf(
    entries: list[dict[str, Any]],
    *,
    sections: list[dict[str, Any]],
    source_file_path: str,
) -> list[dict[str, Any]]:
    if not entries:
        return entries
    source_path = Path(str(source_file_path or "").strip())
    if fitz is None or not source_path.exists() or source_path.suffix.lower() != ".pdf":
        return entries

    updated = [dict(row) for row in entries]
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for idx, row in enumerate(updated):
        if str(row.get("entry_kind") or "").strip().upper() != "REQUIREMENT":
            continue
        if not bool(row.get("is_scoped_local_clause", False)):
            continue
        label = str(row.get("section_code") or "").strip()
        if not re.fullmatch(r"\d+", label):
            continue
        parent_key = str(
            row.get("structural_parent_section_key")
            or row.get("parent_section_key")
            or row.get("section_instance_key")
            or ""
        ).strip()
        if not parent_key:
            continue
        grouped[parent_key].append((idx, row))

    if not grouped:
        return updated

    section_window_index = _build_section_window_index(sections or [])
    source_key = str(source_path.resolve())
    line_detection_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}
    section_window_meta_cache: dict[str, dict[str, Any]] = {}
    insertions: dict[int, list[dict[str, Any]]] = defaultdict(list)

    with fitz.open(str(source_path)) as pdf_document:
        for parent_key, indexed_rows in grouped.items():
            indexed_rows.sort(key=lambda item: (_to_int(item[1].get("req_order")), item[0]))
            first_label = str(indexed_rows[0][1].get("section_code") or "").strip()
            parent_code = str(indexed_rows[0][1].get("owner_section_code") or "").strip()
            parent_title = str(indexed_rows[0][1].get("owner_section_title") or "").strip()
            if first_label != "1" or not parent_code:
                continue
            synthetic_parent_row = {
                "section_instance_key": parent_key,
                "section_key": parent_key,
                "section_code": parent_code,
                "section_title": parent_title,
            }
            section_window_meta = _get_section_window_snapshot_meta(
                synthetic_parent_row,
                pdf_document=pdf_document,
                source_key=source_key,
                section_window_index=section_window_index,
                line_detection_cache=line_detection_cache,
                section_window_meta_cache=section_window_meta_cache,
            )
            if not section_window_meta or not section_window_meta.get("plan"):
                continue
            line_specs = _build_section_visual_line_specs(
                pdf_document=pdf_document,
                source_key=source_key,
                plan=list(section_window_meta.get("plan") or []),
                line_detection_cache=line_detection_cache,
            )
            recovered = _recover_clause_sequence_from_line_specs(line_specs, first_numeric_label=first_label)
            if len(recovered) < len(indexed_rows):
                continue
            recovered_by_label_sequence: dict[str, list[str]] = defaultdict(list)
            recovered_numeric_sequence: list[str] = []
            for item in recovered:
                label = str(item.get("label") or "").strip()
                text = str(item.get("text") or "").strip()
                if not label or not text:
                    continue
                if not re.fullmatch(r"\d+", label):
                    continue
                recovered_by_label_sequence[label].append(text)
                recovered_numeric_sequence.append(label)
            if len(recovered_numeric_sequence) < len(indexed_rows):
                continue
            expected_numeric_sequence = [
                str(row.get("section_code") or "").strip()
                for _, row in indexed_rows
            ]
            if recovered_numeric_sequence[: len(indexed_rows)] != expected_numeric_sequence:
                continue
            for idx, row in indexed_rows:
                label = str(row.get("section_code") or "").strip()
                label_recovered = recovered_by_label_sequence.get(label) or []
                if not label_recovered:
                    continue
                repaired_text = label_recovered.pop(0)
                if not repaired_text:
                    continue
                current_text = str(row.get("requirement_text") or "").strip()
                if _normalize_preview_title_token(repaired_text) == _normalize_preview_title_token(current_text):
                    continue
                updated[idx] = _update_preview_requirement_row_text(row, repaired_text)

            numeric_position = 0
            alpha_sequence = 0
            for clause in recovered:
                label = str(clause.get("label") or "").strip()
                text = str(clause.get("text") or "").strip()
                if not label or not text:
                    continue
                if re.fullmatch(r"\d+", label):
                    numeric_position += 1
                    continue
                if not re.fullmatch(r"[A-Za-z]", label):
                    continue
                if numeric_position <= 0 or numeric_position > len(indexed_rows):
                    continue
                alpha_sequence += 1
                anchor_index, anchor_row = indexed_rows[numeric_position - 1]
                insertions[anchor_index].append(
                    _build_preview_alpha_clause_row(
                        base_row=anchor_row,
                        parent_section_code=parent_code,
                        parent_section_key=parent_key,
                        parent_section_title=parent_title,
                        label=label,
                        text=text,
                        sequence=alpha_sequence,
                    )
                )

    if not insertions:
        return updated

    rebuilt: list[dict[str, Any]] = []
    for idx, row in enumerate(updated):
        rebuilt.append(row)
        for extra in insertions.get(idx, []):
            rebuilt.append(extra)
    return rebuilt


def _should_surface_scoped_local_clause_as_section(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return True
    if not bool(row.get("is_scoped_local_clause", False)):
        return True
    if normalize_scope_id(str(row.get("appendix_scope") or "")) != MAIN_SCOPE_ID:
        return True
    code = str(row.get("section_code") or "").strip()
    if not code:
        return True
    # Keep multi-level local clause anchors visible, but avoid turning plain
    # ordered items like 1）/2）/3） into standalone synthetic sections.
    return not re.fullmatch(r"\d+", code)


def _should_promote_local_clause_to_section(
    *,
    parent_section_code: str,
    clause_code: str,
    allow_same_code: bool = False,
) -> bool:
    parent_code = str(parent_section_code or "").strip()
    local_code = str(clause_code or "").strip()
    if not local_code:
        return False
    if not parent_code:
        return True
    if local_code == parent_code:
        return allow_same_code
    # Local clause anchors frequently reset inside a parent block
    # (for example 3.2.2 -> 6 / 6.1 / 11.12 or 6 -> 1 / 2 / 3),
    # so explicit clause codes should become their own canonical section anchors.
    return True


def _should_scope_local_clause_to_parent(
    *,
    parent_section_code: str,
    clause_code: str,
    allow_same_code: bool = False,
) -> bool:
    parent_code = str(parent_section_code or "").strip()
    local_code = str(clause_code or "").strip()
    if not parent_code or not local_code:
        return False
    if local_code == parent_code:
        return allow_same_code
    return not local_code.startswith(f"{parent_code}.")


def _join_scoped_local_clause_key(parent_section_key: str, clause_code: str) -> str:
    parent_key = str(parent_section_key or "").strip()
    code = str(clause_code or "").strip()
    if not parent_key or not code:
        return ""
    return f"{parent_key}::{code}"


def _scoped_local_clause_parent_code(*, parent_section_code: str, clause_code: str) -> str:
    code = str(clause_code or "").strip()
    if "." in code:
        return code.rsplit(".", 1)[0]
    return str(parent_section_code or "").strip()


def _scoped_local_clause_parent_key(*, parent_section_key: str, clause_code: str) -> str:
    base_key = str(parent_section_key or "").strip()
    code = str(clause_code or "").strip()
    if not base_key:
        return ""
    if "." in code:
        return _join_scoped_local_clause_key(base_key, code.rsplit(".", 1)[0])
    return base_key


def counts_toward_fidelity_section(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict) or bool(row.get("is_appendix_marker", False)):
        return False
    if bool(row.get("complex_table_mode", False)):
        return False
    if not str(row.get("section_code") or "").strip():
        return False
    structural_kind = str(row.get("structural_kind") or "").strip().lower()
    if structural_kind != "synthetic":
        return True
    # Keep appendix/test-outline synthetic scopes in fidelity counts, but do not
    # let preview-only local clause splits in the main document inflate section totals.
    return normalize_scope_id(str(row.get("appendix_scope") or "")) != MAIN_SCOPE_ID


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
    latest_by_id: dict[str, dict[str, Any]] = {}
    ordered_ids: list[str] = []
    for asset in assets:
        table_id = str(asset.get("table_id") or "").strip()
        if not table_id:
            continue
        if table_id not in latest_by_id:
            ordered_ids.append(table_id)
        latest_by_id[table_id] = asset
    return [latest_by_id[table_id] for table_id in ordered_ids]


def _format_clause_section_label(section_code: str, *, clause_code: str = "") -> str:
    code = str(clause_code or section_code or "").strip()
    if not code:
        return ""
    return f"{code}）" if str(clause_code or "").strip() else code


def _extract_explicit_section_code(anchor: str) -> str:
    match = SECTION_ANCHOR_RE.search(anchor or "")
    return str(match.group(1) or "").strip() if match else ""


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
    return str(match.group(1) or "").strip() if match else ""


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
    if "::" in value:
        parent_key, local_code = value.rsplit("::", 1)
        if ":" in parent_key:
            appendix_scope = parent_key.split(":", 1)[0].strip().upper()
        else:
            appendix_scope = ""
        return (appendix_scope, local_code.strip())
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
    scope_catalog: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not segments or not markers:
        return []

    scope_has_requirements = {
        normalize_scope_id(str(row.get("appendix_scope") or ""))
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
        scope_info = _resolve_scope_info(
            scope_catalog,
            str(marker.get("appendix_scope") or ""),
            title=str(marker.get("section_title") or ""),
        )
        scope = scope_info["scope_id"]
        scope_identity = normalize_scope_id(scope)
        if not scope or not scope_identity:
            continue
        if scope_identity in scope_has_requirements:
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

        section_code = ""
        section_title = str(scope_info["scope_title"] or marker.get("section_title") or f"附录 {scope}").strip()[:160]
        section_key = _join_title_section_key(scope, section_title)
        req_order += 1
        out.append(
            {
                "req_code": f"REQ-{scope}{req_order:04d}",
                "entry_id": f"REQ-{scope}{req_order:04d}",
                "entry_kind": "REQUIREMENT",
                "appendix_scope": scope,
                "appendix_title": section_title,
                "section_code": section_code,
                "section_key": section_key,
                "section_level": 0,
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
        scope_has_requirements.add(scope_identity)
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
    *,
    include_scope_roots: bool = False,
    exclude_complex_table_mode: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not sections:
        return [], {
            "zero_direct_requirement_parent_count": 0,
            "synthetic_section_count": 0,
            "orphan_section_count": 0,
        }

    copied = [
        {
            **dict(row),
            "appendix_scope": normalize_scope_id(str(row.get("appendix_scope") or "")),
            "appendix_title": str(
                row.get("appendix_title")
                or (MAIN_SCOPE_TITLE if normalize_scope_id(str(row.get("appendix_scope") or "")) == MAIN_SCOPE_ID else "")
            ).strip(),
            "numbering_level": _to_int(row.get("numbering_level") or row.get("section_level"))
            or _infer_section_level(str(row.get("section_code") or "")),
        }
        for row in sections
        if not (exclude_complex_table_mode and bool(row.get("complex_table_mode", False)))
    ]
    if include_scope_roots:
        scope_meta: dict[str, dict[str, Any]] = {}
        for node in copied:
            scope = normalize_scope_id(str(node.get("appendix_scope") or ""))
            title = str(node.get("appendix_title") or "").strip() or (MAIN_SCOPE_TITLE if scope == MAIN_SCOPE_ID else "")
            state = scope_meta.setdefault(
                scope,
                {
                    "appendix_title": title,
                    "source_page_start": _to_int(node.get("source_page_start")),
                    "source_page_end": _to_int(node.get("source_page_end")),
                },
            )
            if title and not state.get("appendix_title"):
                state["appendix_title"] = title
            start_page = _to_int(node.get("source_page_start"))
            end_page = _to_int(node.get("source_page_end")) or start_page
            if start_page > 0 and (
                _to_int(state.get("source_page_start")) <= 0 or start_page < _to_int(state.get("source_page_start"))
            ):
                state["source_page_start"] = start_page
            if end_page > 0 and end_page > _to_int(state.get("source_page_end")):
                state["source_page_end"] = end_page

        for scope, meta in sorted(scope_meta.items(), key=lambda item: scope_sort_key(item[0])):
            title = str(meta.get("appendix_title") or (MAIN_SCOPE_TITLE if scope == MAIN_SCOPE_ID else f"附录 {scope}")).strip()
            root_key = f"{scope}:ROOT"
            copied.append(
                {
                    "appendix_scope": scope,
                    "appendix_title": title,
                    "section_code": "",
                    "section_key": root_key,
                    "section_instance_key": root_key,
                    "section_title": title,
                    "section_label": title,
                    "section_level": 0,
                    "numbering_level": 0,
                    "parent_section_code": "",
                    "parent_section_key": "",
                    "has_children": True,
                    "is_leaf": False,
                    "source_page_start": _to_int(meta.get("source_page_start")) or None,
                    "source_page_end": _to_int(meta.get("source_page_end")) or _to_int(meta.get("source_page_start")) or None,
                    "source_line_index_start": 0,
                    "source_line_index_end": 0,
                    "line_count": 0,
                    "section_char_count": len(title),
                    "requirement_like": False,
                    "is_appendix_marker": False,
                    "requirement_count": 0,
                    "total_chars": 0,
                    "text_preview": title[:280],
                    "first_req_order": None,
                    "outline_order": None,
                    "is_scoped_local_clause": False,
                    "is_appendix_scope_root": True,
                }
            )

    node_key_by_section_key: dict[str, str] = {}
    any_node_key_by_section_key: dict[str, str] = {}
    children_by_parent_key: dict[str, list[dict[str, Any]]] = {}
    root_nodes: list[dict[str, Any]] = []
    scope_root_key_by_scope: dict[str, str] = {}

    for index, node in enumerate(copied):
        node_key = str(node.get("section_instance_key") or node.get("section_key") or "").strip()
        if not node_key:
            node_key = f"section-node-{index}"
        node["_structural_node_key"] = node_key
        if bool(node.get("is_appendix_scope_root", False)):
            structural_kind = "scope_root"
            scope_root_key_by_scope[normalize_scope_id(str(node.get("appendix_scope") or ""))] = node_key
        else:
            structural_kind = "outline" if node.get("outline_order") is not None else "synthetic"
        node["structural_kind"] = structural_kind
        section_key = str(node.get("section_key") or "").strip()
        if section_key and section_key not in any_node_key_by_section_key:
            any_node_key_by_section_key[section_key] = node_key
        if structural_kind == "outline" and section_key and section_key not in node_key_by_section_key:
            node_key_by_section_key[section_key] = node_key

    node_by_structural_key: dict[str, dict[str, Any]] = {
        str(node.get("_structural_node_key") or "").strip(): node for node in copied
    }

    for node in copied:
        parent_key = _resolve_structural_parent_node_key(
            node,
            outline_node_key_by_section_key=node_key_by_section_key,
            any_node_key_by_section_key=any_node_key_by_section_key,
            scope_root_node_key_by_scope=scope_root_key_by_scope,
        )
        if parent_key == node.get("_structural_node_key"):
            parent_key = ""
        node["structural_parent_key"] = parent_key
        if parent_key:
            children_by_parent_key.setdefault(parent_key, []).append(node)
        else:
            root_nodes.append(node)

    ordered: list[dict[str, Any]] = []

    def visit(node: dict[str, Any], depth: int) -> None:
        node["tree_order"] = len(ordered)
        node["structural_depth"] = depth
        node["structural_level"] = depth
        ordered.append(node)
        node_key = str(node.get("_structural_node_key") or "").strip()
        children = children_by_parent_key.get(node_key, [])
        for child in sorted(children, key=_section_tree_child_sort_key):
            visit(child, depth + 1)

    for node in sorted(root_nodes, key=_section_tree_child_sort_key):
        visit(node, 0)

    for node in ordered:
        node_key = str(node.get("_structural_node_key") or "").strip()
        parent_node = node_by_structural_key.get(str(node.get("structural_parent_key") or "").strip())
        owner_section_code = str(node.get("section_code") or "").strip()
        owner_section_title = str(node.get("section_title") or "").strip()
        owner_section_label = _format_owner_section_label(
            owner_section_code,
            owner_section_title,
            str(node.get("section_label") or "").strip(),
        )
        parent_section_code = str(node.get("parent_section_code") or "").strip()
        parent_section_key = str(node.get("parent_section_key") or "").strip()
        structural_parent_section_code = (
            str(parent_node.get("section_code") or "").strip() if parent_node else parent_section_code
        )
        structural_parent_section_key = (
            str(parent_node.get("section_instance_key") or parent_node.get("section_key") or "").strip()
            if parent_node
            else parent_section_key
        )
        structural_parent_section_title = str(parent_node.get("section_title") or "").strip() if parent_node else ""
        structural_parent_section_label = (
            _format_owner_section_label(
                structural_parent_section_code,
                structural_parent_section_title,
                str(parent_node.get("section_label") or "").strip(),
            )
            if parent_node
            else _format_owner_section_label(structural_parent_section_code, structural_parent_section_title)
        )
        node["owner_section_code"] = owner_section_code
        node["owner_section_title"] = owner_section_title
        node["owner_section_label"] = owner_section_label
        node["structural_parent_section_code"] = structural_parent_section_code
        node["structural_parent_section_key"] = structural_parent_section_key
        node["structural_parent_section_title"] = structural_parent_section_title
        node["structural_parent_section_label"] = structural_parent_section_label
        node["numbering_level"] = _to_int(node.get("numbering_level") or node.get("section_level"))
        node["section_level"] = node["numbering_level"]
        has_children = bool(children_by_parent_key.get(node_key))
        node["has_children"] = has_children
        node["is_leaf"] = not has_children
        node.pop("_structural_node_key", None)

    stats = {
        "zero_direct_requirement_parent_count": sum(
            1
            for row in ordered
            if bool(row.get("has_children"))
            and not bool(row.get("is_appendix_scope_root", False))
            and _to_int(row.get("requirement_count")) <= 0
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
    scope_root_node_key_by_scope: dict[str, str],
) -> str:
    if bool(node.get("is_appendix_scope_root", False)):
        return ""
    explicit_parent_key = str(node.get("parent_section_key") or "").strip()
    if explicit_parent_key:
        parent_key = outline_node_key_by_section_key.get(explicit_parent_key) or any_node_key_by_section_key.get(
            explicit_parent_key
        )
        if not parent_key and normalize_scope_id(str(node.get("appendix_scope") or "")) == MAIN_SCOPE_ID:
            plain_parent_key = explicit_parent_key.split(":", 1)[-1].strip()
            parent_key = outline_node_key_by_section_key.get(plain_parent_key) or any_node_key_by_section_key.get(
                plain_parent_key
            )
        if parent_key:
            return parent_key
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
        if not parent_key and appendix_scope == MAIN_SCOPE_ID:
            parent_key = outline_node_key_by_section_key.get(parent_code) or any_node_key_by_section_key.get(parent_code)
        if parent_key:
            return parent_key
        parent_code = _parent_section_code(parent_code)
    scope = normalize_scope_id(str(node.get("appendix_scope") or ""))
    root_key = scope_root_node_key_by_scope.get(scope)
    if root_key and root_key != str(node.get("_structural_node_key") or ""):
        return root_key
    return ""


def _section_tree_child_sort_key(row: dict[str, Any]) -> tuple[int, int, int, int, int, int, str]:
    structural_kind = str(row.get("structural_kind") or "").strip().lower()
    if structural_kind == "scope_root":
        scope_rank, scope_order, _ = scope_sort_key(str(row.get("appendix_scope") or ""))
        return (-1, scope_rank, scope_order, 0, 0, 0, str(row.get("section_instance_key") or row.get("section_key") or ""))
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
    rank, order, _ = scope_sort_key(scope)
    return (rank, order)


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
                "appendix_scope_evidence": APPENDIX_EVIDENCE_EXPLICIT,
                "appendix_title_source": "appendix_marker",
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
            for candidate in _iter_test_outline_candidates(line):
                scope = normalize_test_outline_scope(str(candidate.get("scope") or ""))
                scope_identity = test_outline_scope_identity(scope)
                if not scope or not scope_identity or scope_identity in markers_by_scope:
                    continue
                markers_by_scope[scope_identity] = {
                    "appendix_scope": scope,
                    "section_title": str(candidate.get("title") or scope),
                    "source_page_start": source_page if source_page > 0 else None,
                    "source_page_end": source_page if source_page > 0 else None,
                    "appendix_scope_evidence": str(candidate.get("evidence") or APPENDIX_EVIDENCE_HEADING),
                    "appendix_title_source": str(candidate.get("title_source") or "test_outline_marker"),
                }
    return sorted(markers_by_scope.values(), key=lambda row: _scope_sort_key(str(row.get("appendix_scope", ""))))


def _iter_test_outline_candidates(line: str) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    raw = str(line or "").strip()
    if not raw:
        return candidates

    generic_title = infer_generic_test_outline_title(raw)
    if generic_title:
        dedupe_key = (normalize_scope_id(generic_title), normalize_scope_text(generic_title))
        if dedupe_key not in seen:
            candidates.append(
                {
                    "scope": generic_title,
                    "title": generic_title,
                    "evidence": APPENDIX_EVIDENCE_HEADING,
                    "title_source": "test_outline_heading",
                }
            )
            seen.add(dedupe_key)

    if ELECTRIC_TEST_OUTLINE_HEADER_RE.search(raw):
        dedupe_key = (normalize_scope_id(TEST_OUTLINE_INFERRED_GENERIC_TITLE), TEST_OUTLINE_INFERRED_GENERIC_TITLE)
        if dedupe_key not in seen:
            candidates.append(
                {
                    "scope": TEST_OUTLINE_INFERRED_GENERIC_TITLE,
                    "title": TEST_OUTLINE_INFERRED_GENERIC_TITLE,
                    "evidence": APPENDIX_EVIDENCE_TABLE_HEADER,
                    "title_source": "test_outline_table_header",
                }
            )
            seen.add(dedupe_key)

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
                dedupe_key = (normalize_scope_id(scope), normalize_scope_text(canonical))
                if dedupe_key not in seen:
                    candidates.append(
                        {
                            "scope": scope,
                            "title": canonical,
                            "evidence": APPENDIX_EVIDENCE_HEADING,
                            "title_source": "test_outline_named_title",
                        }
                    )
                    seen.add(dedupe_key)
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
        test_outline_scope_identity(str(row.get("appendix_scope") or "")): row
        for row in appendix_markers
        if str(row.get("appendix_scope") or "").strip()
    }
    injected: set[str] = set()
    out: list[dict[str, Any]] = []

    for row in section_outline:
        scope = normalize_test_outline_scope(str(row.get("appendix_scope") or ""))
        scope_identity = test_outline_scope_identity(scope)
        marker = marker_by_scope.get(scope_identity)
        if scope_identity and marker and scope_identity not in injected:
            out.append(_build_appendix_outline_row(marker))
            injected.add(scope_identity)
        merged = dict(row)
        merged["appendix_scope"] = scope
        merged["is_appendix_marker"] = False
        out.append(merged)

    for scope_identity, marker in marker_by_scope.items():
        if scope_identity in injected:
            continue
        out.append(_build_appendix_outline_row(marker))
    return out


def _build_appendix_outline_row(marker: dict[str, Any]) -> dict[str, Any]:
    title = str(marker.get("section_title") or "")
    return {
        "appendix_scope": normalize_test_outline_scope(str(marker.get("appendix_scope") or "")),
        "appendix_title": str(marker.get("appendix_title") or title),
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

    hidden_groups_by_parent_instance: dict[str, list[list[dict[str, Any]]]] = {}
    for section_instance_key, grouped in req_groups.items():
        if not grouped:
            continue
        seed = grouped[0]
        if not bool(seed.get("is_scoped_local_clause", False)):
            continue
        if _should_surface_scoped_local_clause_as_section(seed):
            continue
        parent_instance_key = str(
            seed.get("structural_parent_section_key")
            or seed.get("parent_section_key")
            or ""
        ).strip()
        if not parent_instance_key or parent_instance_key == section_instance_key:
            continue
        hidden_groups_by_parent_instance.setdefault(parent_instance_key, []).append(grouped)

    for groups in hidden_groups_by_parent_instance.values():
        groups.sort(
            key=lambda grouped: (
                min((_to_int(row.get("req_order")) for row in grouped), default=10**9),
                min((_to_int(row.get("source_page")) for row in grouped), default=10**9),
                min((_to_int(row.get("source_page_end")) for row in grouped), default=10**9),
                str(grouped[0].get("req_code") or ""),
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
        hidden_groups = hidden_groups_by_parent_instance.get(section_instance_key, [])
        if (
            not grouped
            and not hidden_groups
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
        for hidden_group in hidden_groups:
            hidden_section_instance_key = str(
                hidden_group[0].get("section_instance_key") or hidden_group[0].get("section_key") or ""
            ).strip()
            if hidden_section_instance_key:
                emitted_section_instance_keys.add(hidden_section_instance_key)
        section_code = str(node.get("section_code") or "")
        heading_title = str(heading.get("section_title") or "")
        rows_to_emit = list(grouped)
        for hidden_group in hidden_groups:
            rows_to_emit.extend(hidden_group)
        rows_to_emit.sort(
            key=lambda row: (
                _to_int(row.get("req_order")),
                _to_int(row.get("source_page")),
                _to_int(row.get("source_page_end")),
                str(row.get("req_code") or ""),
            )
        )
        for req in rows_to_emit:
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
            "is_scoped_local_clause": bool(seed.get("is_scoped_local_clause", False)),
        }
        synthetic_node["section_instance_key"] = section_instance_key
        section_code = str(synthetic_node.get("section_code") or "")
        heading_title = str(synthetic_node.get("section_title") or "")
        if _should_surface_scoped_local_clause_as_section(synthetic_node):
            heading_seq += 1
            heading = _build_heading_entry(node=synthetic_node, heading_seq=heading_seq, synthetic=True)
            entries.append(heading)
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
    scope = normalize_scope_id(str(node.get("appendix_scope") or ""))
    appendix_title = str(node.get("appendix_title") or (MAIN_SCOPE_TITLE if scope == MAIN_SCOPE_ID else "")).strip()
    section_code = str(node.get("section_code") or "").strip()
    section_title = str(node.get("section_title") or "").strip()
    section_key = str(node.get("section_key") or "").strip() or _join_section_key(scope, section_code)
    if not section_key and section_title:
        section_key = _join_title_section_key(scope, section_title)
    section_instance_key = str(node.get("section_instance_key") or section_key or "").strip()
    if not section_title:
        if section_code:
            section_title = section_code
        elif appendix_title:
            section_title = appendix_title
        elif scope:
            section_title = f"附录 {scope}"
        else:
            section_title = MAIN_SCOPE_TITLE

    source_page = _to_int(node.get("source_page_start"))
    source_page_end = _to_int(node.get("source_page_end")) or source_page
    source_line_index = _to_int(node.get("source_line_index_start"))
    source_line_index_end = _to_int(node.get("source_line_index_end")) or source_line_index
    numbering_level = _to_int(node.get("numbering_level") or node.get("section_level")) or _infer_section_level(section_code)
    structural_level = _to_int(node.get("structural_level"))
    parent_section_code = str(node.get("parent_section_code") or "").strip()
    parent_section_key = str(node.get("parent_section_key") or "").strip()
    section_label = str(node.get("section_label") or "").strip()
    owner_section_label = _format_owner_section_label(section_code, section_title, section_label)

    scope_token = scope or "MAIN"
    code_token = section_code or "ROOT"
    instance_token = _entry_token(section_instance_key) if section_instance_key else f"{scope_token}-{code_token}"
    entry_id = f"H-{instance_token}-{heading_seq}"

    return {
        "entry_id": entry_id,
        "entry_kind": "HEADING",
        "req_code": "",
        "appendix_scope": scope,
        "appendix_title": appendix_title,
        "section_code": section_code,
        "section_key": section_key,
        "section_instance_key": section_instance_key,
        "section_level": numbering_level,
        "numbering_level": numbering_level,
        "structural_level": structural_level,
        "parent_section_code": parent_section_code,
        "parent_section_key": parent_section_key,
        "source_page": source_page,
        "source_page_end": source_page_end,
        "source_line_index": source_line_index,
        "source_line_index_end": source_line_index_end,
        "source_anchor": "",
        "section_title": section_title,
        "owner_section_code": section_code,
        "owner_section_title": section_title,
        "owner_section_label": owner_section_label,
        "structural_parent_section_code": str(node.get("structural_parent_section_code") or parent_section_code).strip(),
        "structural_parent_section_key": str(node.get("structural_parent_section_key") or parent_section_key).strip(),
        "structural_parent_section_title": str(node.get("structural_parent_section_title") or "").strip(),
        "structural_parent_section_label": str(node.get("structural_parent_section_label") or "").strip(),
        "requirement_type": "",
        "priority": "",
        "text_length": len(section_title),
        "requirement_text": section_title,
        "section_label": section_label,
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
        appendix_scope = normalize_scope_id(str(req.get("appendix_scope") or ""))
        section_row = by_instance_key.get(section_instance_key) or by_section_key.get(section_key)
        section_code = str(req.get("section_code") or "").strip()
        section_title = str(req.get("section_title") or "").strip()
        section_label = str(req.get("section_label") or "").strip()
        appendix_title = str(req.get("appendix_title") or "").strip()
        if section_row:
            if not appendix_title:
                appendix_title = str(section_row.get("appendix_title") or "").strip()
            if not section_code:
                section_code = str(section_row.get("section_code") or "").strip()
            if not section_title:
                section_title = str(section_row.get("section_title") or "").strip()
            if not section_label:
                section_label = str(section_row.get("section_label") or "").strip()

        req["appendix_scope"] = appendix_scope
        req["appendix_title"] = appendix_title or (MAIN_SCOPE_TITLE if appendix_scope == MAIN_SCOPE_ID else "")
        if section_row:
            req["numbering_level"] = _to_int(section_row.get("numbering_level") or section_row.get("section_level"))
            req["structural_level"] = _to_int(section_row.get("structural_level"))

        structural_parent_code = str(req.get("structural_parent_section_code") or "").strip()
        structural_parent_key = str(req.get("structural_parent_section_key") or "").strip()
        if section_row:
            if not structural_parent_code:
                structural_parent_code = str(section_row.get("structural_parent_section_code") or "").strip()
            if not structural_parent_key:
                structural_parent_key = str(section_row.get("structural_parent_section_key") or "").strip()
        if not structural_parent_code:
            structural_parent_code = _parent_section_code(str(req.get("section_code") or "").strip())
        parent_row = None
        if structural_parent_key:
            parent_row = by_instance_key.get(structural_parent_key) or by_section_key.get(structural_parent_key)
        if parent_row is None and structural_parent_code:
            parent_key = _join_section_key(appendix_scope, structural_parent_code)
            parent_row = by_section_key.get(parent_key) or by_instance_key.get(parent_key)
            if not structural_parent_key and parent_row is not None:
                structural_parent_key = str(parent_row.get("section_instance_key") or parent_row.get("section_key") or "").strip()
        structural_parent_title = str(req.get("structural_parent_section_title") or "").strip()
        structural_parent_label = str(req.get("structural_parent_section_label") or "").strip()
        if parent_row:
            if not structural_parent_code:
                structural_parent_code = str(parent_row.get("section_code") or "").strip()
            if not structural_parent_title:
                structural_parent_title = str(parent_row.get("section_title") or "").strip()
            if not structural_parent_label:
                structural_parent_label = _format_owner_section_label(
                    structural_parent_code,
                    structural_parent_title,
                    str(parent_row.get("section_label") or "").strip(),
                )
        else:
            structural_parent_label = structural_parent_label or _format_owner_section_label(
                structural_parent_code,
                structural_parent_title,
            )

        req["structural_parent_section_code"] = structural_parent_code
        req["structural_parent_section_key"] = structural_parent_key
        req["structural_parent_section_title"] = structural_parent_title
        req["structural_parent_section_label"] = structural_parent_label

        owner_section_code = section_code
        owner_section_title = section_title
        owner_section_label = _format_owner_section_label(owner_section_code, owner_section_title, section_label)
        if bool(req.get("is_scoped_local_clause", False)) and not _should_surface_scoped_local_clause_as_section(req):
            owner_section_code = structural_parent_code or owner_section_code
            owner_section_title = structural_parent_title or owner_section_title
            owner_section_label = structural_parent_label or _format_owner_section_label(
                owner_section_code,
                owner_section_title,
            )
        req["owner_section_code"] = owner_section_code
        req["owner_section_title"] = owner_section_title
        req["owner_section_label"] = owner_section_label

        if owner_section_code or owner_section_title:
            req["parent_section_code"] = owner_section_code
            req["parent_section_title"] = owner_section_title
            req["parent_section_label"] = _format_section_label(owner_section_code, owner_section_title)
        else:
            req["parent_section_code"] = ""
            req["parent_section_title"] = ""
            req["parent_section_label"] = ""


def _format_owner_section_label(section_code: str, section_title: str, section_label: str = "") -> str:
    explicit = str(section_label or "").strip()
    if explicit:
        return explicit
    code = str(section_code or "").strip()
    if code:
        return code
    return str(section_title or "").strip()


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
