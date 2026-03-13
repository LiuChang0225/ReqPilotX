from __future__ import annotations

import re
import io
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from docx import Document
from pypdf import PdfReader

try:
    import pdfplumber  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pdfplumber = None

try:
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        import pymupdf4llm  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pymupdf4llm = None

try:
    import pymupdf  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pymupdf = None


REPEATED_LINE_THRESHOLD = 0.35
GLOBAL_REPEAT_THRESHOLD = 0.12
GLOBAL_REPEAT_MIN_PAGES = 5
BORDER_SCAN_LINES = 3
EMAIL_RE = re.compile(r"[\w.\-]+@[\w.\-]+")
DOT_LEADER_RE = re.compile(r"\.{6,}|…{3,}")
ONLY_SYMBOLS_RE = re.compile(r"^[\W_]+$")
PAGE_NO_RE = re.compile(r"^(page\s*)?\d+\s*(of|/)\s*\d+$", re.I)
SECTION_HINT_RE = re.compile(r"^\d+(?:\.\d+){1,6}\s+\S+")
SECTION_CODE_LINE_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,6})\b")
SECTION_CANDIDATE_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,6})\s*[\)\]:：.\-]?\s*(.*)$")
ENUM_SUBHEADING_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,2})\s*[）)、)]\s*(.+)$")
ENUM_IMPORTANT_TITLE_RE = re.compile(r"(?:要求|说明|支持|风险|布置|试验|验证|校验|评估|参数|满足|标识|连接器|优化)", re.I)
ENUM_SIGNAL_RE = re.compile(r"^\s*(\d{1,2}(?:\.\d{1,2}){0,3})\s*[）)、)]")
ENUM_DETAIL_HINT_RE = re.compile(
    r"(?:m/s|阻尼力|线性变化|暂定力值|蓄能器|工作环境|载荷输入|dvp|emc|注油压力|功耗|法规|寿命)",
    re.I,
)
PLAIN_TABLE_HEADER_RE = re.compile(r"^\s*序号\s*项目\s*要求\s*$")
TABLE_FIG_HINT_RE = re.compile(r"(?:\btable\s*\d+|\bfig(?:ure)?\.?\s*\d+|表\s*\d+|图\s*\d+)", re.I)
MARKDOWN_TABLE_RULE_RE = re.compile(r"^\|?(?:\s*:?-{2,}:?\s*\|)+\s*$")
DOC_CODE_YEAR_RE = re.compile(r"^[A-Z]{1,6}\s*[/\-]?\s*[A-Z]?\s*T?\s*\d{2,6}(?:\.\d+)?(?:\s*[—\-~/]?\s*\d{2,6})?.*20\d{2}$")
DOC_STD_YEAR_RE = re.compile(r"^[A-Za-z]{1,6}\s*/\s*[A-Za-z]{0,3}\s*\d{2,6}(?:[\.\-_\s]\d{1,4})?.*?(?:19|20)\d{2}\b")
METADATA_HINT_WORDS = {
    "version",
    "revision",
    "rev",
    "date",
    "department",
    "sign",
    "approved",
    "approval",
    "document",
    "doc",
    "confidential",
    "release",
    "issued",
}
WATERMARK_COMPACT_KEYWORDS = (
    "confidential",
    "uncontrolledcopy",
    "internaluseonly",
    "copyright",
)
WATERMARK_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"uncontrolled copy",
        r"internal use only",
        r"copyright",
        r"version history",
        r"revision history",
        r"document key",
        r"designed by",
        r"released by",
        r"s-list id",
        r"\besign\b",
        r"decision date",
        r"\bfilename\b",
        r"\bphone\b",
        r"\.com\b",
        r"^to[:：]",
        r"^by[:：]",
        r"^of[:：]",
        r"^at[:：]",
        r"^\s*编制[:：_]",
        r"^\s*校对[:：_]",
        r"^\s*审核[:：_]",
        r"^\s*标审[:：_]",
        r"^\s*会签[:：_]",
        r"^\s*审定[:：_]",
        r"^\s*批准[:：_]",
    ]
]
WATERMARK_CONTACT_RE = re.compile(r"(?:to[:：]\s*others|vitesco\.com|@[\w.\-]+)", re.I)
WATERMARK_FRAGMENT_RE = re.compile(r"(?:jun-|\bliang\.l\b|vitesco|tesco\.)", re.I)
TABLE_INLINE_WATERMARK_RE = re.compile(
    r"(?:\b(?:ju|jun|un)\s*-\s*(?:l|li|lia|lian|liang(?:\.?li(?:u)?)?)\b|\bn\s*-\s*liang\.?li\b|"
    r"\bliang\.?li(?:u)?\b|\b(?:vitesco|tesco)\.?(?:co|com)?\b|"
    r"电子（\s*om|子（长春|长春）|春）有限(?:公)?|\bom\b(?=\s*\|)|\b-li\b|\bers\s+jun(?:-|)\w*\b|"
    r"(?:@geely|geely\.co|@vitesc|vitesc(?:o)?\.co|zhu@\s*\d{1,2}:\d{2}|liu@\s*限公司)|"
    r"(?:\b(?:to|of|by|f|y|o|at)\s*[:：]\s*[^\|\n]{0,16}(?:纬|湃汽|geely|zhu|dec|an))"
    r")",
    re.I,
)


def parse_document(file_path: str) -> tuple[str, list[dict[str, Any]], str]:
    parsed = parse_document_with_diagnostics(file_path)
    return parsed["text"], parsed["segments"], parsed["file_type"]


def parse_document_with_diagnostics(file_path: str) -> dict[str, Any]:
    path = Path(file_path)
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            text, segments, file_type, diagnostics = _parse_pdf(path)
            return {
                "text": text,
                "segments": segments,
                "file_type": file_type,
                "diagnostics": diagnostics,
            }
        if ext == ".docx":
            text, segments, file_type = _parse_docx(path)
            return {
                "text": text,
                "segments": segments,
                "file_type": file_type,
                "diagnostics": {"mode": "docx_paragraph"},
            }
        text, segments, file_type = _parse_text(path)
        return {
            "text": text,
            "segments": segments,
            "file_type": file_type,
            "diagnostics": {"mode": "plain_text"},
        }
    except Exception as exc:
        # Keep workflow moving when a parser dependency fails.
        text, segments, file_type = _parse_text(path)
        return {
            "text": text,
            "segments": segments,
            "file_type": file_type,
            "diagnostics": {"mode": "fallback_text", "error": str(exc)},
        }


def _parse_pdf(path: Path) -> tuple[str, list[dict[str, Any]], str, dict[str, Any]]:
    reader = PdfReader(str(path))
    total_pages = len(reader.pages)

    engine_pages: dict[str, list[dict[str, Any]]] = {}
    watermark_model = {"phrases": [], "token_groups": []}
    if pymupdf is not None:
        watermark_model = _detect_watermark_model_with_pymupdf(path, total_pages)
    engine_pages["pypdf"] = _parse_pdf_pages_with_pypdf(reader)
    if pymupdf is not None:
        engine_pages["pymupdf_raw"] = _parse_pdf_pages_with_pymupdf_raw(path, total_pages)
    if pdfplumber is not None:
        engine_pages["pdfplumber"] = _parse_pdf_pages_with_pdfplumber(path)
    if pymupdf4llm is not None:
        engine_pages["pymupdf4llm"] = _parse_pdf_pages_with_pymupdf4llm(path, total_pages)

    merged_pages, diagnostics = _merge_pdf_engine_pages(engine_pages, total_pages, watermark_model)
    segments = _build_segments_from_pages(merged_pages)
    full_text = "\n".join(seg["text"] for seg in segments if seg.get("text"))

    diagnostics.update(
        {
            "mode": "pdf_multi_engine_merge",
            "file_name": path.name,
            "total_pages": total_pages,
            "engines_available": list(engine_pages.keys()),
            "detected_watermark_phrases": len(watermark_model.get("phrases", [])),
        }
    )
    return full_text, segments, "PDF", diagnostics


def _parse_pdf_pages_with_pypdf(reader: PdfReader) -> list[dict[str, Any]]:
    pages_lines: list[list[str]] = []
    for page in reader.pages:
        raw_text = page.extract_text() or ""
        pages_lines.append(_extract_clean_lines(raw_text))
    return _finalize_pages_lines(pages_lines)


def _parse_pdf_pages_with_pdfplumber(path: Path) -> list[dict[str, Any]]:
    pages_lines: list[list[str]] = []
    with pdfplumber.open(str(path)) as pdf:  # type: ignore[union-attr]
        for page in pdf.pages:
            raw_text = page.extract_text(layout=True) or ""
            pages_lines.append(_extract_clean_lines(raw_text))
    return _finalize_pages_lines(pages_lines)


def _parse_pdf_pages_with_pymupdf_raw(path: Path, total_pages: int) -> list[dict[str, Any]]:
    doc = pymupdf.open(str(path))  # type: ignore[union-attr]
    page_payloads: list[dict[str, Any]] = []
    signatures_by_page: list[list[str]] = []

    try:
        for page_index in range(total_pages):
            page = doc[page_index]
            raw = page.get_text("rawdict")
            page_w = max(1.0, float(page.rect.width))
            page_h = max(1.0, float(page.rect.height))

            line_records: list[dict[str, Any]] = []
            signatures: list[str] = []
            for block in raw.get("blocks", []):
                if int(block.get("type", 0)) != 0:
                    continue
                for line in block.get("lines", []):
                    span_parts: list[str] = []
                    max_size = 0.0
                    for span in line.get("spans", []):
                        span_text = _span_text(span)
                        normalized = _normalize_line(span_text)
                        if not normalized:
                            continue
                        span_parts.append(normalized)
                        max_size = max(max_size, float(span.get("size", 0.0) or 0.0))

                    if not span_parts:
                        continue
                    line_text = _normalize_line(" ".join(span_parts))
                    if not line_text:
                        continue

                    line_dir = line.get("dir") or (1.0, 0.0)
                    dir_x = float(line_dir[0]) if isinstance(line_dir, (list, tuple)) and len(line_dir) >= 1 else 1.0
                    dir_y = float(line_dir[1]) if isinstance(line_dir, (list, tuple)) and len(line_dir) >= 2 else 0.0
                    bbox = line.get("bbox")
                    cx_ratio, cy_ratio = _bbox_center_ratio(bbox, page_w, page_h)

                    record = {
                        "text": line_text,
                        "dir_x": dir_x,
                        "dir_y": dir_y,
                        "max_size": max_size,
                        "cx_ratio": cx_ratio,
                        "cy_ratio": cy_ratio,
                    }
                    line_records.append(record)

                    sig = _watermark_line_signature(record)
                    if sig:
                        signatures.append(sig)

            page_payloads.append(
                {
                    "line_records": line_records,
                    "table_count": 0,
                    "image_count": len(page.get_images(full=False)),
                    "graphics_count": 0,
                }
            )
            signatures_by_page.append(signatures)
    finally:
        doc.close()

    signature_counter: Counter[str] = Counter()
    for signatures in signatures_by_page:
        for sig in set(signatures):
            signature_counter[sig] += 1
    repeat_threshold = max(4, int(total_pages * 0.06))

    cleaned_pages_lines: list[list[str]] = []
    pre_removed_watermark: list[int] = []
    for payload in page_payloads:
        lines: list[str] = []
        removed_wm = 0
        for record in payload.get("line_records", []):
            sig = _watermark_line_signature(record)
            repeated_count = signature_counter.get(sig, 0) if sig else 0
            if _is_pymupdf_watermark_line(record, repeated_count, repeat_threshold):
                removed_wm += 1
                continue
            lines.append(str(record.get("text", "")))
        cleaned_pages_lines.append(lines)
        pre_removed_watermark.append(removed_wm)

    repeat_model = _build_repeat_noise_model(cleaned_pages_lines)
    finalized: list[dict[str, Any]] = []
    for idx, payload in enumerate(page_payloads):
        lines, noise_stats = _filter_noise_lines(cleaned_pages_lines[idx], repeat_model)
        watermark_removed = int(pre_removed_watermark[idx]) + int(noise_stats.get("watermark", 0))
        total_removed = int(pre_removed_watermark[idx]) + int(noise_stats.get("total", 0))
        finalized.append(
            {
                "lines": lines,
                "table_count": _table_figure_hint_count(lines),
                "image_count": int(payload.get("image_count", 0)),
                "graphics_count": int(payload.get("graphics_count", 0)),
                "removed_noise_count": total_removed,
                "removed_repeat_count": int(noise_stats.get("repeat", 0)),
                "removed_watermark_count": watermark_removed,
            }
        )
    return finalized


def _span_text(span: dict[str, Any]) -> str:
    if "text" in span:
        return str(span.get("text", ""))
    chars = span.get("chars") or []
    return "".join(str(ch.get("c", "")) for ch in chars if isinstance(ch, dict))


def _bbox_center_ratio(bbox: Any, page_w: float, page_h: float) -> tuple[float, float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) < 4:
        return 0.0, 0.0
    try:
        x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
    except Exception:
        return 0.0, 0.0
    cx = ((x0 + x1) / 2.0) / page_w
    cy = ((y0 + y1) / 2.0) / page_h
    return round(cx, 3), round(cy, 3)


def _watermark_line_signature(record: dict[str, Any]) -> str:
    text = str(record.get("text", "")).strip().lower()
    if not text:
        return ""
    normalized = re.sub(r"\d+", "", text)
    normalized = re.sub(r"[^a-z\u4e00-\u9fff@\.]+", "", normalized)
    if len(normalized) < 4:
        return ""
    cx = float(record.get("cx_ratio", 0.0))
    cy = float(record.get("cy_ratio", 0.0))
    dy = float(record.get("dir_y", 0.0))
    return f"{normalized}|{round(cx,2)}|{round(cy,2)}|{round(dy,1)}"


def _is_pymupdf_watermark_line(record: dict[str, Any], repeat_count: int, repeat_threshold: int) -> bool:
    text = str(record.get("text", "")).strip()
    if not text:
        return True

    if WATERMARK_CONTACT_RE.search(text):
        return True
    for pattern in WATERMARK_PATTERNS:
        if pattern.search(text):
            return True

    dir_y = abs(float(record.get("dir_y", 0.0)))
    max_size = float(record.get("max_size", 0.0))
    rotated = dir_y >= 0.18
    repeated = repeat_count >= repeat_threshold

    if rotated and repeated and (max_size >= 12.0 or len(text) <= 90):
        return True
    if rotated and max_size >= 16.0 and len(text) <= 140:
        return True
    if repeated and len(text) <= 42 and _looks_like_spaced_watermark(text):
        return True
    return False


def _parse_pdf_pages_with_pymupdf4llm(path: Path, total_pages: int) -> list[dict[str, Any]]:
    page_payloads: list[dict[str, Any]] = [
        {"lines": [], "table_count": 0, "image_count": 0, "graphics_count": 0}
        for _ in range(total_pages)
    ]

    # pymupdf4llm prints guidance text on each call; silence it for API cleanliness.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        chunks = pymupdf4llm.to_markdown(str(path), page_chunks=True)  # type: ignore[union-attr]
    for chunk in chunks:
        metadata = chunk.get("metadata", {}) if isinstance(chunk, dict) else {}
        page_no = _to_int(metadata.get("page"))
        if page_no <= 0 or page_no > total_pages:
            continue
        text = str(chunk.get("text", "")) if isinstance(chunk, dict) else ""
        page_payloads[page_no - 1] = {
            "lines": _extract_clean_lines(text),
            "table_count": len(chunk.get("tables") or []) if isinstance(chunk, dict) else 0,
            "image_count": len(chunk.get("images") or []) if isinstance(chunk, dict) else 0,
            "graphics_count": len(chunk.get("graphics") or []) if isinstance(chunk, dict) else 0,
        }

    repeat_model = _build_repeat_noise_model([p["lines"] for p in page_payloads])
    finalized: list[dict[str, Any]] = []
    for payload in page_payloads:
        lines, noise_stats = _filter_noise_lines(payload["lines"], repeat_model)
        finalized.append(
            {
                "lines": lines,
                "table_count": int(payload.get("table_count", 0)),
                "image_count": int(payload.get("image_count", 0)),
                "graphics_count": int(payload.get("graphics_count", 0)),
                "removed_noise_count": int(noise_stats.get("total", 0)),
                "removed_repeat_count": int(noise_stats.get("repeat", 0)),
                "removed_watermark_count": int(noise_stats.get("watermark", 0)),
            }
        )
    return finalized


def _finalize_pages_lines(pages_lines: list[list[str]]) -> list[dict[str, Any]]:
    repeat_model = _build_repeat_noise_model(pages_lines)
    finalized: list[dict[str, Any]] = []
    for lines in pages_lines:
        clean_lines, noise_stats = _filter_noise_lines(lines, repeat_model)
        finalized.append(
            {
                "lines": clean_lines,
                "table_count": _table_figure_hint_count(clean_lines),
                "image_count": 0,
                "graphics_count": 0,
                "removed_noise_count": int(noise_stats.get("total", 0)),
                "removed_repeat_count": int(noise_stats.get("repeat", 0)),
                "removed_watermark_count": int(noise_stats.get("watermark", 0)),
            }
        )
    return finalized


def _merge_pdf_engine_pages(
    engine_pages: dict[str, list[dict[str, Any]]],
    total_pages: int,
    watermark_model: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    merged_pages: list[dict[str, Any]] = []
    selected_engine_counter: Counter[str] = Counter()
    page_details: list[dict[str, Any]] = []
    low_confidence_pages: list[int] = []
    line_order_violation_pages: list[int] = []

    for page_index in range(total_pages):
        candidates: list[dict[str, Any]] = []
        for engine_name, pages in engine_pages.items():
            payload = pages[page_index] if page_index < len(pages) else {"lines": []}
            lines = payload.get("lines") or []
            score = _page_quality_score(lines, payload)
            fragment_ratio = _fragment_line_ratio(lines)
            mega_table_rows = _mega_table_row_count(lines)
            enum_signal = _enumeration_signal_count(lines)
            max_line_length = max((len(_normalize_line(str(line or ""))) for line in lines), default=0)
            candidates.append(
                {
                    "engine": engine_name,
                    "lines": lines,
                    "score": score,
                    "fragment_ratio": fragment_ratio,
                    "mega_table_rows": mega_table_rows,
                    "enum_signal": enum_signal,
                    "max_line_length": max_line_length,
                    "table_count": int(payload.get("table_count", 0)),
                    "image_count": int(payload.get("image_count", 0)),
                    "graphics_count": int(payload.get("graphics_count", 0)),
                    "removed_noise_count": int(payload.get("removed_noise_count", 0)),
                    "removed_repeat_count": int(payload.get("removed_repeat_count", 0)),
                    "removed_watermark_count": int(payload.get("removed_watermark_count", 0)),
                }
            )

        candidates.sort(key=lambda c: c["score"], reverse=True)
        chosen = candidates[0] if candidates else {"engine": "none", "lines": [], "score": -999.0}

        # Strict single-engine fallback:
        # avoid over-trusting compact markdown-table pages that may drop trailing clauses.
        if (
            int(chosen.get("table_count", 0)) > 0
            and int(chosen.get("enum_signal", 0)) == 0
            and len(chosen.get("lines") or []) <= 24
        ):
            for alt in candidates[1:]:
                if not alt.get("lines"):
                    continue
                # Preserve table-structured runs (typically appendix test matrices):
                # avoid switching table page -> non-table page when both neighbor
                # pages of the same engine also carry table structure.
                if (
                    int(chosen.get("table_count", 0)) > 0
                    and int(alt.get("table_count", 0)) <= 0
                    and _table_run_neighbor_count(engine_pages, str(chosen.get("engine") or ""), page_index) >= 2
                    and not _has_mixed_table_clause_signal(chosen.get("lines") or [])
                ):
                    continue
                if len(alt["lines"]) < max(16, int(len(chosen["lines"]) * 1.8)):
                    continue
                if int(alt.get("enum_signal", 0)) < 3:
                    continue
                if float(alt.get("score", -999.0)) + 0.6 < float(chosen.get("score", -999.0)):
                    continue
                if (
                    int(chosen.get("table_count", 0)) > 0
                    and int(chosen.get("max_line_length", 0)) >= 1800
                    and len(alt["lines"]) >= max(24, int(len(chosen["lines"]) * 1.4))
                    and float(alt.get("score", -999.0)) + 0.8 >= float(chosen.get("score", -999.0))
                    and float(alt.get("fragment_ratio", 0.0)) <= 0.52
                ):
                    chosen = alt
                    break
                if float(alt.get("fragment_ratio", 0.0)) > 0.38:
                    continue
                chosen = alt
                break

        # If another candidate has much longer text with similar quality, prefer completeness.
        for alt in candidates[1:]:
            if not alt["lines"] or not chosen["lines"]:
                continue
            if _should_prefer_attachment_band_candidate(chosen, alt):
                chosen = alt
                break
            if (
                int(chosen.get("table_count", 0)) > 0
                and int(alt.get("table_count", 0)) <= 0
                and _should_preserve_structured_table_page(chosen.get("lines") or [], alt.get("lines") or [])
            ):
                continue
            if (
                int(chosen.get("mega_table_rows", 0)) > int(alt.get("mega_table_rows", 0))
                and int(alt.get("mega_table_rows", 0)) == 0
                and float(alt.get("score", -999.0)) + 0.16 >= float(chosen.get("score", -999.0))
                and len(alt["lines"]) >= max(12, int(len(chosen["lines"]) * 0.7))
            ):
                chosen = alt
                break
            if (
                int(alt.get("enum_signal", 0)) >= int(chosen.get("enum_signal", 0)) + 4
                and float(alt.get("score", -999.0)) + 0.14 >= float(chosen.get("score", -999.0))
                and int(alt.get("max_line_length", 0)) <= max(1200, int(chosen.get("max_line_length", 0)) + 240)
            ):
                chosen = alt
                break
            if alt["score"] + 0.25 < chosen["score"]:
                continue
            # Avoid switching to engines that are much more fragmented/noisy.
            if (
                float(alt.get("fragment_ratio", 0.0))
                > max(0.12, float(chosen.get("fragment_ratio", 0.0)) * 1.8)
                and alt["score"] <= chosen["score"] + 0.12
            ):
                continue
            if len(alt["lines"]) >= len(chosen["lines"]) * 1.45:
                chosen = alt
                break

        # Attachment table pages often have multiple decent text candidates.
        # After broad completeness switching, do one more pass for cleaner band labels.
        for alt in candidates:
            if alt["engine"] == chosen["engine"]:
                continue
            if not alt["lines"] or not chosen["lines"]:
                continue
            if _should_prefer_attachment_band_candidate(chosen, alt):
                chosen = alt
                break

        # Strict single-engine merge mode:
        # keep only one chosen engine per page, no cross-engine line injection.
        chosen_raw_lines = list(chosen.get("lines") or [])
        enriched_lines = list(chosen_raw_lines)
        supplemental_heading_count = 0
        injected_table_blocks = 0
        table_block_quality = 0.0
        enriched_lines, dropped_mega_rows = _drop_mega_duplicate_table_rows(enriched_lines)
        if watermark_model:
            enriched_lines = _apply_detected_watermark_cleanup(enriched_lines, watermark_model)
        if _has_line_order_inversion(chosen_raw_lines, enriched_lines):
            line_order_violation_pages.append(page_index + 1)
        if _is_low_confidence_page(chosen, candidates):
            low_confidence_pages.append(page_index + 1)
        chosen = {
            **chosen,
            "lines": enriched_lines,
            "supplemental_heading_count": supplemental_heading_count,
            "injected_table_block_count": injected_table_blocks,
            "table_block_quality": table_block_quality,
            "dropped_mega_table_rows": dropped_mega_rows,
        }

        merged_pages.append(
            {
                "page_no": page_index + 1,
                "engine": chosen["engine"],
                "lines": chosen["lines"],
                "table_count": int(chosen.get("table_count", 0)),
                "image_count": int(chosen.get("image_count", 0)),
                "graphics_count": int(chosen.get("graphics_count", 0)),
                "quality_score": float(chosen.get("score", -999.0)),
                "supplemental_heading_count": int(chosen.get("supplemental_heading_count", 0)),
                "injected_table_block_count": int(chosen.get("injected_table_block_count", 0)),
                "table_block_quality": float(chosen.get("table_block_quality", 0.0)),
                "dropped_mega_table_rows": int(chosen.get("dropped_mega_table_rows", 0)),
                "removed_noise_count": int(chosen.get("removed_noise_count", 0)),
                "removed_repeat_count": int(chosen.get("removed_repeat_count", 0)),
                "removed_watermark_count": int(chosen.get("removed_watermark_count", 0)),
                "low_confidence": bool((page_index + 1) in low_confidence_pages),
            }
        )
        selected_engine_counter.update([str(chosen["engine"])])
        page_details.append(
            {
                "page_no": page_index + 1,
                "selected_engine": chosen["engine"],
                "selected_lines": len(chosen["lines"]),
                "quality_score": round(float(chosen.get("score", -999.0)), 4),
                "fragment_ratio": round(float(chosen.get("fragment_ratio", 0.0)), 4),
                "mega_table_rows": int(chosen.get("mega_table_rows", 0)),
                "enum_signal": int(chosen.get("enum_signal", 0)),
                "max_line_length": int(chosen.get("max_line_length", 0)),
                "engine_scores": {c["engine"]: round(float(c["score"]), 4) for c in candidates},
                "table_count": int(chosen.get("table_count", 0)),
                "image_count": int(chosen.get("image_count", 0)),
                "graphics_count": int(chosen.get("graphics_count", 0)),
                "supplemental_heading_count": int(chosen.get("supplemental_heading_count", 0)),
                "injected_table_block_count": int(chosen.get("injected_table_block_count", 0)),
                "table_block_quality": round(float(chosen.get("table_block_quality", 0.0)), 4),
                "dropped_mega_table_rows": int(chosen.get("dropped_mega_table_rows", 0)),
                "removed_noise_count": int(chosen.get("removed_noise_count", 0)),
                "removed_repeat_count": int(chosen.get("removed_repeat_count", 0)),
                "removed_watermark_count": int(chosen.get("removed_watermark_count", 0)),
            }
        )

    table_pages = sum(1 for page in merged_pages if int(page.get("table_count", 0)) > 0)
    image_pages = sum(
        1 for page in merged_pages if int(page.get("image_count", 0)) + int(page.get("graphics_count", 0)) > 0
    )
    non_empty_pages = sum(1 for page in merged_pages if page.get("lines"))
    supplemental_heading_pages = sum(1 for page in merged_pages if int(page.get("supplemental_heading_count", 0)) > 0)
    supplemental_heading_total = sum(int(page.get("supplemental_heading_count", 0)) for page in merged_pages)
    table_block_injected_total = sum(int(page.get("injected_table_block_count", 0)) for page in merged_pages)
    dropped_mega_table_rows_total = sum(int(page.get("dropped_mega_table_rows", 0)) for page in merged_pages)
    table_quality_values = [
        float(page.get("table_block_quality", 0.0))
        for page in merged_pages
        if float(page.get("table_block_quality", 0.0)) > 0
    ]
    avg_table_block_quality = (
        round(sum(table_quality_values) / len(table_quality_values), 4) if table_quality_values else 0.0
    )
    removed_noise_total = sum(int(page.get("removed_noise_count", 0)) for page in merged_pages)
    removed_repeat_total = sum(int(page.get("removed_repeat_count", 0)) for page in merged_pages)
    removed_watermark_total = sum(int(page.get("removed_watermark_count", 0)) for page in merged_pages)

    diagnostics = {
        "merge_mode": "strict_single_engine",
        "injection_disabled": True,
        "selected_engine_pages": dict(selected_engine_counter),
        "table_pages": table_pages,
        "image_or_graphic_pages": image_pages,
        "non_empty_pages": non_empty_pages,
        "supplemental_heading_pages": supplemental_heading_pages,
        "supplemental_heading_total": supplemental_heading_total,
        "table_block_injected_total": table_block_injected_total,
        "dropped_mega_table_rows_total": dropped_mega_table_rows_total,
        "avg_table_block_quality": avg_table_block_quality,
        "removed_noise_total": removed_noise_total,
        "removed_repeat_total": removed_repeat_total,
        "removed_watermark_total": removed_watermark_total,
        "detected_watermark_phrases": len((watermark_model or {}).get("phrases", [])),
        "line_injection_count": 0,
        "line_reorder_count": len(line_order_violation_pages),
        "order_integrity_ok": len(line_order_violation_pages) == 0,
        "line_order_violation_pages": line_order_violation_pages,
        "low_confidence_pages": low_confidence_pages,
        "page_details": page_details,
    }
    return merged_pages, diagnostics


def _has_line_order_inversion(source_lines: list[str], final_lines: list[str]) -> bool:
    # Strict mode only applies monotonic, in-order transforms (drop/clean).
    # We detect true inversions via unique anchor lines shared by source/final.
    source_keys = [
        _line_merge_key(_normalize_line(str(line or "")))
        for line in source_lines
    ]
    final_keys = [
        _line_merge_key(_normalize_line(str(line or "")))
        for line in final_lines
    ]
    source_keys = [key for key in source_keys if key]
    final_keys = [key for key in final_keys if key]
    if not source_keys or not final_keys:
        return False

    key_counts = Counter(source_keys)
    unique_positions = {
        key: idx for idx, key in enumerate(source_keys) if key_counts.get(key, 0) == 1
    }
    projected_positions = [unique_positions[key] for key in final_keys if key in unique_positions]
    if len(projected_positions) < 2:
        return False

    prev = projected_positions[0]
    for pos in projected_positions[1:]:
        if pos < prev:
            return True
        prev = pos
    return False


def _table_run_neighbor_count(
    engine_pages: dict[str, list[dict[str, Any]]],
    engine_name: str,
    page_index: int,
) -> int:
    pages = engine_pages.get(str(engine_name or ""), [])
    if not pages:
        return 0
    count = 0
    for offset in (-1, 1):
        idx = page_index + offset
        if idx < 0 or idx >= len(pages):
            continue
        payload = pages[idx] or {}
        if int(payload.get("table_count", 0) or 0) > 0:
            count += 1
            continue
        lines = payload.get("lines") or []
        markdown_rows = sum(1 for line in lines if str(line or "").strip().count("|") >= 2)
        if markdown_rows >= 4:
            count += 1
    return count


def _is_low_confidence_page(chosen: dict[str, Any], candidates: list[dict[str, Any]]) -> bool:
    score = float(chosen.get("score", -999.0))
    selected_lines = len(chosen.get("lines") or [])
    fragment_ratio = float(chosen.get("fragment_ratio", 0.0))
    second_best = float(candidates[1].get("score", -999.0)) if len(candidates) > 1 else -999.0
    score_gap = score - second_best if second_best > -900 else score

    if selected_lines <= 0:
        return True
    if score < 0.25:
        return True
    if fragment_ratio > 0.35 and score < 0.55:
        return True
    if score_gap < 0.03 and selected_lines < 10:
        return True
    return False


def _has_mixed_table_clause_signal(lines: list[str]) -> bool:
    """Detect pages that are table-heavy but also carry clause headings.

    These mixed pages are risky in strict table-run protection because markdown-table
    engines may flatten away nearby paragraph clauses (e.g. 3)/4) headings).
    """
    if not lines:
        return False

    table_clause_hits = 0
    plain_clause_hits = 0
    for raw in lines:
        text = _normalize_line(str(raw or ""))
        if not text:
            continue
        if _is_markdown_table_row_like(text):
            cells = _parse_markdown_cells(text)
            if _table_row_contains_clause_heading(cells):
                table_clause_hits += 1
            continue

        match = ENUM_SUBHEADING_RE.match(text)
        if not match:
            continue
        title = _normalize_line(match.group(2))
        if _looks_like_clause_heading_text(title):
            plain_clause_hits += 1

    return table_clause_hits >= 1 or plain_clause_hits >= 2


def _should_preserve_structured_table_page(chosen_lines: list[str], alt_lines: list[str]) -> bool:
    if _count_markdown_table_rows(chosen_lines) < 2:
        return False
    has_stage_band = _has_table_stage_band_signal(chosen_lines)
    if not (_has_mixed_table_clause_signal(chosen_lines) or has_stage_band):
        return False
    if has_stage_band:
        return len(alt_lines) <= max(16, int(len(chosen_lines) * 5.0))
    return len(alt_lines) <= max(12, int(len(chosen_lines) * 2.4))


def _has_table_stage_band_signal(lines: list[str]) -> bool:
    for raw in lines:
        text = _normalize_line(str(raw or ""))
        if not _is_markdown_table_row_like(text):
            continue
        cells = [_normalize_line(cell) for cell in _parse_markdown_cells(text) if _normalize_line(cell)]
        if len(cells) < 2:
            continue
        meaningful = [cell for cell in cells if not re.fullmatch(r"col\d+", cell, re.I)]
        if len(meaningful) < 2:
            continue
        stage_text = meaningful[0]
        if not re.search(
            r"(生命周期|lifecycle|阶段|phase|production|development|maintenance|support|总体要求|general requirements)",
            stage_text,
            re.I,
        ):
            continue
        unique = {re.sub(r"\s+", "", cell).lower() for cell in meaningful[:12] if cell}
        if len(unique) <= max(2, len(meaningful[:12]) // 3):
            return True
    return False


def _attachment_band_label_count(lines: list[str]) -> int:
    return _attachment_band_label_metrics(lines)[0]


def _attachment_band_label_quality_score(lines: list[str]) -> int:
    return _attachment_band_label_metrics(lines)[1]


def _attachment_band_label_metrics(lines: list[str]) -> tuple[int, int]:
    if not lines:
        return 0, 0
    count = 0
    quality_score = 0
    seen: set[str] = set()
    for raw in lines:
        text = _normalize_line(str(raw or ""))
        if not text or "|" in text:
            continue
        if len(text) > 48:
            continue
        if not re.match(
            r"^(?:附件|附录|appendix|annex)\s*\d+\s*(?:[-—–]{1,2}\s*)?[A-Za-z\u4e00-\u9fff0-9].{0,80}$",
            text,
            re.I,
        ):
            continue
        key = re.sub(r"\s+", "", text).lower()
        if key in seen:
            continue
        seen.add(key)
        count += 1
        quality_score += max(0, 32 - len(text))
    return count, quality_score


def _should_prefer_attachment_band_candidate(chosen: dict[str, Any], alt: dict[str, Any]) -> bool:
    chosen_labels = _attachment_band_label_count(chosen.get("lines") or [])
    alt_labels = _attachment_band_label_count(alt.get("lines") or [])
    chosen_quality = _attachment_band_label_quality_score(chosen.get("lines") or [])
    alt_quality = _attachment_band_label_quality_score(alt.get("lines") or [])
    if alt_labels == 0:
        return False
    if alt_labels < chosen_labels:
        return False
    if alt_labels == chosen_labels and alt_quality <= chosen_quality:
        return False
    if float(alt.get("score", -999.0)) + 0.12 < float(chosen.get("score", -999.0)):
        return False
    if len(alt.get("lines") or []) + 2 < int(len(chosen.get("lines") or []) * 0.85):
        return False
    if (
        float(alt.get("fragment_ratio", 0.0))
        > max(0.24, float(chosen.get("fragment_ratio", 0.0)) * 2.2)
        and float(alt.get("score", -999.0)) <= float(chosen.get("score", -999.0)) + 0.08
    ):
        return False
    return True


def _table_row_contains_clause_heading(cells: list[str]) -> bool:
    if not cells:
        return False
    for cell in cells[:3]:
        value = _normalize_line(str(cell or ""))
        if not value:
            continue
        match = ENUM_SUBHEADING_RE.match(value)
        if match and _looks_like_clause_heading_text(_normalize_line(match.group(2))):
            return True
        if re.match(r"^\d{1,2}\s*[）)、)]", value) and _looks_like_clause_heading_text(value):
            return True
    return False


def _looks_like_clause_heading_text(text: str) -> bool:
    value = _normalize_line(str(text or ""))
    if not value:
        return False
    if len(value) <= 2:
        return False
    return bool(ENUM_IMPORTANT_TITLE_RE.search(value))


def _is_markdown_table_row_like(line: str) -> bool:
    text = _normalize_line(str(line or ""))
    if "|" not in text:
        return False
    cells = _parse_markdown_cells(text)
    return len(cells) >= 2


def _detect_watermark_model_with_pymupdf(path: Path, total_pages: int) -> dict[str, Any]:
    phrases: list[str] = []
    token_groups: list[list[str]] = []
    try:
        doc = pymupdf.open(str(path))  # type: ignore[union-attr]
    except Exception:
        return {"phrases": phrases, "token_groups": token_groups}

    span_stat: dict[str, dict[str, Any]] = {}
    try:
        for page_index in range(total_pages):
            page = doc[page_index]
            raw = page.get_text("rawdict")
            page_h = max(1.0, float(page.rect.height))
            for block in raw.get("blocks", []):
                if int(block.get("type", 0)) != 0:
                    continue
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        text = _span_text(span)
                        normalized = _normalize_watermark_phrase(text)
                        if not normalized:
                            continue
                        bbox = span.get("bbox")
                        bbox_h_ratio = 0.0
                        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
                            try:
                                bbox_h_ratio = max(0.0, (float(bbox[3]) - float(bbox[1])) / page_h)
                            except Exception:
                                bbox_h_ratio = 0.0
                        size = float(span.get("size", 0.0) or 0.0)
                        state = span_stat.setdefault(
                            normalized,
                            {"pages": set(), "size_sum": 0.0, "count": 0, "bbox_h_ratio_sum": 0.0},
                        )
                        state["pages"].add(page_index + 1)
                        state["size_sum"] += size
                        state["bbox_h_ratio_sum"] += bbox_h_ratio
                        state["count"] += 1
    finally:
        doc.close()

    page_threshold = max(4, int(total_pages * 0.2))
    for phrase, state in span_stat.items():
        page_count = len(state["pages"])
        if page_count < page_threshold:
            continue
        count = max(1, int(state["count"]))
        avg_size = float(state["size_sum"]) / count
        avg_h_ratio = float(state["bbox_h_ratio_sum"]) / count
        tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", phrase)
        if len(tokens) < 2:
            continue
        if avg_size < 10 and avg_h_ratio < 0.12:
            continue
        # Keep only low-semantic repeated phrases as watermark candidates.
        if len(phrase) > 120:
            continue
        phrases.append(phrase)
        token_group = [tok for tok in tokens if len(tok) >= 2]
        if token_group:
            token_groups.append(token_group[:8])

    # Keep deterministic order and dedupe.
    phrases = sorted(set(phrases), key=lambda value: (len(value), value))
    dedup_groups: list[list[str]] = []
    seen_group: set[str] = set()
    for group in token_groups:
        key = "|".join(sorted(set(group)))
        if not key or key in seen_group:
            continue
        seen_group.add(key)
        dedup_groups.append(list(dict.fromkeys(group)))
    return {"phrases": phrases[:24], "token_groups": dedup_groups[:24]}


def _normalize_watermark_phrase(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"\s+", " ", value)
    if len(value) < 8:
        return ""
    return value


def _apply_detected_watermark_cleanup(lines: list[str], watermark_model: dict[str, Any]) -> list[str]:
    phrases = [str(item) for item in watermark_model.get("phrases", []) if str(item).strip()]
    token_groups = [group for group in watermark_model.get("token_groups", []) if isinstance(group, list)]
    if not phrases and not token_groups:
        return lines

    cleaned_lines: list[str] = []
    for line in lines:
        value = str(line or "")
        if not value:
            continue
        for phrase in phrases:
            if phrase in value:
                value = value.replace(phrase, " ")

        for group in token_groups:
            if len(group) < 2:
                continue
            hit_count = 0
            for token in group:
                if not token:
                    continue
                if re.search(r"[A-Za-z0-9]", token):
                    if re.search(rf"\b{re.escape(token)}\b", value, re.I):
                        hit_count += 1
                else:
                    if token in value:
                        hit_count += 1
            if hit_count < 2:
                continue
            for token in group:
                if re.search(r"[A-Za-z0-9]", token):
                    value = re.sub(rf"\b{re.escape(token)}\b", " ", value, flags=re.I)
                else:
                    value = value.replace(token, " ")

        value = re.sub(r"\s+", " ", value).strip()
        if value:
            cleaned_lines.append(value)
    return cleaned_lines


def _build_segments_from_pages(merged_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for page in merged_pages:
        lines = page.get("lines") or []
        if not lines:
            continue
        page_no = int(page.get("page_no", 0))
        segments.append(
            {
                "source_page": page_no,
                "source_anchor": f"Page {page_no}",
                "text": "\n".join(lines),
                "parser_engine": page.get("engine"),
                "table_count": int(page.get("table_count", 0)),
                "image_count": int(page.get("image_count", 0)),
                "graphics_count": int(page.get("graphics_count", 0)),
                "quality_score": float(page.get("quality_score", 0.0)),
                "supplemental_heading_count": int(page.get("supplemental_heading_count", 0)),
                "removed_noise_count": int(page.get("removed_noise_count", 0)),
                "removed_repeat_count": int(page.get("removed_repeat_count", 0)),
                "removed_watermark_count": int(page.get("removed_watermark_count", 0)),
            }
        )
    return segments


def _page_quality_score(lines: list[str], payload: dict[str, Any]) -> float:
    if not lines:
        return -999.0
    base = _segment_quality([{"text": "\n".join(lines)}])
    section_bonus = min(0.8, _section_hint_count(lines) * 0.05)
    table_hint_bonus = min(0.7, _table_figure_hint_count(lines) * 0.04)
    table_bonus = min(0.6, int(payload.get("table_count", 0)) * 0.04)
    image_bonus = min(0.25, int(payload.get("image_count", 0)) * 0.01)
    enum_signal_bonus = min(0.35, _enumeration_signal_count(lines) * 0.012)
    watermark_penalty = min(0.9, _watermark_line_count(lines) * 0.05)
    fragment_penalty = min(1.2, _fragment_line_count(lines) * 0.08)
    watermark_fragment_penalty = min(1.6, _watermark_fragment_line_count(lines) * 0.35)
    mega_table_penalty = min(1.6, _mega_table_row_count(lines) * 0.8)
    return (
        base
        + section_bonus
        + table_hint_bonus
        + table_bonus
        + image_bonus
        + enum_signal_bonus
        - watermark_penalty
        - fragment_penalty
        - watermark_fragment_penalty
        - mega_table_penalty
    )


def _section_hint_count(lines: list[str]) -> int:
    return sum(1 for line in lines if SECTION_HINT_RE.search(line))


def _table_figure_hint_count(lines: list[str]) -> int:
    return sum(1 for line in lines if TABLE_FIG_HINT_RE.search(line))


def _watermark_line_count(lines: list[str]) -> int:
    count = 0
    for line in lines:
        for pattern in WATERMARK_PATTERNS:
            if pattern.search(line):
                count += 1
                break
    return count


def _watermark_fragment_line_count(lines: list[str]) -> int:
    return sum(1 for line in lines if WATERMARK_FRAGMENT_RE.search(line))


def _fragment_line_count(lines: list[str]) -> int:
    return sum(1 for line in lines if _is_fragment_like_line(line))


def _fragment_line_ratio(lines: list[str]) -> float:
    if not lines:
        return 0.0
    return _fragment_line_count(lines) / len(lines)


def _enumeration_signal_count(lines: list[str]) -> int:
    seen: set[str] = set()
    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if not value:
            continue
        match = ENUM_SIGNAL_RE.match(value)
        if not match:
            continue
        code = str(match.group(1) or "").strip()
        if not code:
            continue
        seen.add(code)
    return len(seen)


def _mega_table_row_count(lines: list[str]) -> int:
    return sum(1 for line in lines if _is_mega_duplicate_table_row(line))


def _is_fragment_like_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    if SECTION_CODE_LINE_RE.match(value):
        return False

    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)
    if not tokens:
        return False

    if len(value) <= 8:
        if len(tokens) <= 4 and max((len(tok) for tok in tokens), default=0) <= 2:
            return True
        if re.search(r"(?:\b[A-Za-z]\b\s*){2,}", value):
            return True

    single_letter = sum(1 for tok in tokens if re.fullmatch(r"[A-Za-z]", tok))
    single_cjk = sum(1 for tok in tokens if re.fullmatch(r"[\u4e00-\u9fff]", tok))
    if len(tokens) >= 4 and single_letter + single_cjk >= int(len(tokens) * 0.65):
        return True

    if re.search(r"[A-Za-z]", value) and re.search(r"[\u4e00-\u9fff]", value):
        if len(tokens) <= 5 and max((len(tok) for tok in tokens), default=0) <= 2:
            return True
    return False


def _parse_docx(path: Path) -> tuple[str, list[dict[str, Any]], str]:
    doc = Document(str(path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text and p.text.strip()]
    cleaned = [_clean_text(text) for text in paragraphs]
    segments: list[dict[str, Any]] = []
    for idx, text in enumerate(cleaned, start=1):
        segments.append(
            {
                "source_page": None,
                "source_anchor": f"Paragraph {idx}",
                "text": text,
            }
        )
    return "\n".join(cleaned), segments, "DOCX"


def _parse_text(path: Path) -> tuple[str, list[dict[str, Any]], str]:
    content = path.read_text(encoding="utf-8", errors="ignore")
    cleaned = _clean_text(content)
    segments = [
        {
            "source_page": None,
            "source_anchor": "PlainText",
            "text": cleaned,
        }
    ]
    return cleaned, segments, "TEXT"


def _clean_text(text: str) -> str:
    lines = _extract_clean_lines(text)
    return "\n".join(lines)


def _extract_clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = _normalize_line(raw)
        if line:
            lines.extend(_split_inline_attachment_band_line(line))
    return lines


def _normalize_line(line: str) -> str:
    value = line.strip()
    if not value:
        return ""
    value = re.sub(r"^#+\s*", "", value)
    value = value.replace("**", "").replace("__", "").replace("`", "")
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = value.replace("&nbsp;", " ")
    value = re.sub(r"\bs\s+h\s+a\s+l\s+l\b", "shall", value, flags=re.I)
    value = re.sub(r"\bm\s+u\s+s\s+t\b", "must", value, flags=re.I)
    value = re.sub(r"\bs\s+h\s+o\s+u\s+l\s+d\b", "should", value, flags=re.I)
    # Collapse gaps between Chinese characters introduced by PDF extraction.
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"\s+", " ", value)
    value = _clean_table_row_inline_noise(value)
    value = _strip_inline_watermark_fragments(value)
    value = value.replace("•", "-").replace("·", "-")
    return value.strip()


def _split_inline_attachment_band_line(line: str) -> list[str]:
    value = str(line or "").strip()
    if not value:
        return []
    match = re.match(
        r"^\s*((?:附件|附录|appendix|annex)\s*\d+\s*[-—–]{1,2}\s*[A-Za-z\u4e00-\u9fff0-9][^。！？!?；;|]{0,32}?)\s+(.+)$",
        value,
        re.I,
    )
    if not match:
        return [value]
    label = _normalize_line(str(match.group(1) or ""))
    tail = _normalize_line(str(match.group(2) or ""))
    if not _looks_like_attachment_band_label(label) or not _looks_like_inline_attachment_tail(tail):
        return [value]
    return [label, tail]


def _looks_like_attachment_band_label(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if len(value) > 40:
        return False
    return bool(
        re.match(
            r"^(?:附件|附录|appendix|annex)\s*\d+\s*[-—–]{1,2}\s*[A-Za-z\u4e00-\u9fff0-9].+$",
            value,
            re.I,
        )
    )


def _looks_like_inline_attachment_tail(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        re.match(
            r"^(?:[（(]\d+\s*[）)]|\d+(?:\.\d+){0,4}\s*[、.．:：)]|\d+(?:\.\d+){1,6}\s+)",
            value,
        )
    )


def _strip_inline_watermark_fragments(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)
    if len(tokens) < 6:
        return value

    single_count = sum(1 for token in tokens if len(token) == 1 and not token.isdigit())
    if single_count < 2:
        return value

    frag_ratio = single_count / max(1, len(tokens))
    if frag_ratio < 0.22:
        return value

    parts = value.split()
    if len(parts) < 4:
        return value

    cleaned_parts: list[str] = []
    for idx, part in enumerate(parts):
        plain = re.sub(r"[^A-Za-z\u4e00-\u9fff0-9]", "", part)
        if len(plain) == 1 and not plain.isdigit():
            prev_plain = ""
            next_plain = ""
            if idx > 0:
                prev_plain = re.sub(r"[^A-Za-z\u4e00-\u9fff0-9]", "", parts[idx - 1])
            if idx + 1 < len(parts):
                next_plain = re.sub(r"[^A-Za-z\u4e00-\u9fff0-9]", "", parts[idx + 1])

            # Keep common valid one-letter words unless the line is strongly fragmented.
            if plain.lower() in {"a", "i"} and frag_ratio < 0.34:
                cleaned_parts.append(part)
                continue

            if len(prev_plain) >= 2 and len(next_plain) >= 2:
                continue
            if frag_ratio >= 0.34:
                continue
        cleaned_parts.append(part)

    normalized = " ".join(cleaned_parts)
    normalized = re.sub(r"\b([A-Za-z]{2,})\s+[A-Za-z]\s+([A-Za-z]{2,})\b", r"\1 \2", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _clean_table_row_inline_noise(text: str) -> str:
    value = str(text or "").strip()
    if "|" not in value:
        return value

    cleaned = TABLE_INLINE_WATERMARK_RE.sub(" ", value)
    cells = cleaned.split("|")
    normalized_cells: list[str] = []
    for cell in cells:
        raw = _strip_table_watermark_chunks(cell.strip())
        if not raw:
            normalized_cells.append("")
            continue
        tokens = raw.split()
        if len(tokens) >= 3:
            kept: list[str] = []
            for token in tokens:
                plain = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]", "", token)
                if _is_suspicious_table_token(plain):
                    continue
                if len(plain) == 1 and plain.islower():
                    continue
                if len(plain) == 1 and re.fullmatch(r"[\u4e00-\u9fff]", plain):
                    continue
                kept.append(token)
            raw = " ".join(kept).strip()
        raw = _normalize_table_header_noise(raw)
        raw = _trim_table_cell_tail_noise(raw)
        raw = re.sub(r"\s+", " ", raw).strip()
        normalized_cells.append(raw)

    collapsed = "|".join(normalized_cells)
    collapsed = re.sub(r"\|\s+\|", "||", collapsed)
    collapsed = re.sub(r"\s+", " ", collapsed).strip()
    return collapsed


def _is_suspicious_table_token(token: str) -> bool:
    value = str(token or "").strip().lower()
    if not value:
        return False
    if "@" in value:
        return True
    if re.fullmatch(r"\d{1,2}:\d{2}", value):
        return True
    if re.fullmatch(r"\d{1,2}-\d{1,2}", value):
        return True
    if re.fullmatch(r"\d{1,2}\)", value):
        return True
    if value in {
        "jun",
        "ju",
        "un",
        "om",
        "m",
        "ers",
        "hers",
        "tesco",
        "vitesco",
        "liang",
        "liangl",
        "liangli",
        "geely",
        "vitesc",
        "zhu",
        "liu",
        "decan",
        "by",
        "others",
        "thers",
        "company",
        "公司",
        "有限公",
        "车电",
        "湃汽",
        "纬",
        "an",
    }:
        return True
    if value.startswith("jun") and len(value) <= 10:
        return True
    if "liang" in value and len(value) <= 12:
        return True
    if any(key in value for key in {"geely", "eely", "vitesc", "zhu", "liu", "liang", "tesco", "decan"}):
        return True
    if len(value) <= 2 and re.search(r"[a-z]", value):
        return True
    if value.endswith("co") and value.startswith(("geely", "vitesc")):
        return True
    if re.fullmatch(r"(?:to|of|by|at|f|y|o)\W*", value):
        return True
    return False


def _strip_table_watermark_chunks(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    cleaned = TABLE_INLINE_WATERMARK_RE.sub(" ", value)
    cleaned = re.sub(r"(?:\b(?:to|of|by|at|f|y|o)\s*[:：]\s*[^\|\n]{0,20})", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\b(?:zhu|liu)\s*@\s*\d{1,2}:\d{2}\b)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:@(?:geely|vitesc\w*)\b)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\b(?:geely|eely|vitesc\w*|zhu|liu)\S*)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\b\d{1,2}:\d{2}\b)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\b\d{1,2}-\d{1,2}\b)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:\b(?:company|others|thers|decan)\b)", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"(?:有限公|限公司|公司|车电|湃汽|纬\s*an)", " ", cleaned)
    cleaned = re.sub(r"(?:电子（长)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _normalize_table_header_noise(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"^\s*[\u4e00-\u9fff](?=测试项目)", "", value)
    value = re.sub(r"^\s*@\s*应用范围\s*\d+(?:\s*\d+)*\s*[:：]?", "应用范围 ", value)
    value = re.sub(r"^\s*@\s*", "", value)
    value = re.sub(r"\b(?:to|of|by)\s*[:：]\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _trim_table_cell_tail_noise(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"(?:电子（长|-\s*）|-\s*）有限公)\s*$", "", value).strip()
    value = re.sub(r"(?:\s+[.\-])$", "", value).strip()
    if len(value) >= 24:
        value = re.sub(r"\s+[0-9]{1,2}$", "", value).strip()
    if re.search(r"[A-Za-z\u4e00-\u9fff]", value) and re.search(r"\s[0-9]$", value):
        if not re.search(r"(?:\d+(?:\.\d+)?\s*(?:v|a|ma|mm|cm|ms|s|%|℃|°c))$", value, re.I):
            value = re.sub(r"\s[0-9]$", "", value).strip()
    return value


def _build_repeat_noise_model(pages_lines: list[list[str]]) -> dict[str, set[str]]:
    if not pages_lines:
        return {
            "border_exact_keys": set(),
            "border_loose_keys": set(),
            "global_repeat_meta_keys": set(),
        }

    border_exact_counter: Counter[str] = Counter()
    border_loose_counter: Counter[str] = Counter()
    global_exact_counter: Counter[str] = Counter()
    global_representative: dict[str, str] = {}

    for lines in pages_lines:
        if not lines:
            continue

        border = lines[:BORDER_SCAN_LINES] + lines[-BORDER_SCAN_LINES:]
        for line in border:
            exact_key = _normalize_for_repeat(line)
            loose_key = _normalize_for_repeat_loose(line)
            if exact_key:
                border_exact_counter[exact_key] += 1
            if loose_key:
                border_loose_counter[loose_key] += 1

        seen_in_page: set[str] = set()
        for line in lines:
            exact_key = _normalize_for_repeat(line)
            if not exact_key:
                continue
            if exact_key not in global_representative:
                global_representative[exact_key] = line
            if exact_key in seen_in_page:
                continue
            global_exact_counter[exact_key] += 1
            seen_in_page.add(exact_key)

    border_threshold = max(3, int(len(pages_lines) * REPEATED_LINE_THRESHOLD))
    global_threshold = max(GLOBAL_REPEAT_MIN_PAGES, int(len(pages_lines) * GLOBAL_REPEAT_THRESHOLD))

    border_exact_keys = {key for key, count in border_exact_counter.items() if count >= border_threshold}
    border_loose_keys = {key for key, count in border_loose_counter.items() if count >= border_threshold}

    global_repeat_meta_keys: set[str] = set()
    for key, count in global_exact_counter.items():
        if count < global_threshold:
            continue
        sample_line = global_representative.get(key, key)
        if _is_global_header_footer_candidate(sample_line):
            global_repeat_meta_keys.add(key)

    return {
        "border_exact_keys": border_exact_keys,
        "border_loose_keys": border_loose_keys,
        "global_repeat_meta_keys": global_repeat_meta_keys,
    }


def _normalize_for_repeat(line: str) -> str:
    value = line.lower()
    value = re.sub(r"\d+", "#", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _normalize_for_repeat_loose(line: str) -> str:
    value = line.lower()
    value = re.sub(r"\bpages?\s*\d+\s*(?:of|/)\s*\d+\b", " ", value, flags=re.I)
    value = re.sub(r"\b\d+(?:\.\d+){1,6}\b", " ", value)
    value = re.sub(r"\d+", " ", value)
    value = re.sub(r"[^a-z\u4e00-\u9fff]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value if len(value) >= 6 else ""


def _is_global_header_footer_candidate(line: str) -> bool:
    value = line.strip()
    if not value:
        return False
    lower = value.lower()
    if PAGE_NO_RE.match(lower):
        return True
    if DOC_CODE_YEAR_RE.match(value):
        return True
    if DOC_STD_YEAR_RE.match(value):
        return True

    metadata_hits = sum(1 for token in METADATA_HINT_WORDS if re.search(rf"\b{re.escape(token)}\b", lower))
    if metadata_hits >= 2:
        return True

    cjk_meta_tokens = ["版本", "日期", "部门", "签字", "批准", "机密", "保密", "文档"]
    cjk_hits = sum(1 for token in cjk_meta_tokens if token in value)
    if cjk_hits >= 2:
        return True

    if len(value) <= 40 and re.search(r"[A-Za-z]{1,4}\s*/\s*[A-Za-z]?\s*T?\s*\d{2,6}", value):
        return True
    return False


def _is_repeated_noise_line(line: str, repeat_model: dict[str, set[str]]) -> bool:
    exact_key = _normalize_for_repeat(line)
    if exact_key and exact_key in repeat_model.get("border_exact_keys", set()):
        return True

    loose_key = _normalize_for_repeat_loose(line)
    if loose_key and loose_key in repeat_model.get("border_loose_keys", set()):
        return True

    if exact_key and exact_key in repeat_model.get("global_repeat_meta_keys", set()):
        return True
    return False


def _filter_noise_lines(lines: list[str], repeat_model: dict[str, set[str]]) -> tuple[list[str], dict[str, int]]:
    filtered: list[str] = []
    removed_total = 0
    removed_repeat = 0
    removed_watermark = 0

    for line in lines:
        if _is_repeated_noise_line(line, repeat_model):
            removed_total += 1
            removed_repeat += 1
            continue
        # Keep numeric heading candidates even in noisy OCR pages.
        if SECTION_CODE_LINE_RE.match(line.strip()):
            filtered.append(line)
            continue
        noise_reason = _noise_reason(line)
        if noise_reason:
            removed_total += 1
            if noise_reason.startswith("watermark"):
                removed_watermark += 1
            continue
        filtered.append(line)
    return filtered, {
        "total": removed_total,
        "repeat": removed_repeat,
        "watermark": removed_watermark,
    }


def _is_noise_line(line: str) -> bool:
    return bool(_noise_reason(line))


def _noise_reason(line: str) -> str:
    value = line.strip()
    lower = value.lower()
    if len(value) <= 1:
        return "too_short"
    if PAGE_NO_RE.match(lower):
        return "page_number"
    if DOT_LEADER_RE.search(value):
        return "dot_leader"
    if MARKDOWN_TABLE_RULE_RE.match(value):
        return "table_rule"
    if EMAIL_RE.search(value):
        return "email"
    if re.fullmatch(r"[\d\-/:\. ]+", value):
        return "numeric_only"
    if ONLY_SYMBOLS_RE.match(value):
        return "symbols_only"
    if "____" in value:
        return "underscores"
    if re.search(r"\bpages?\s+\d+\s+of\s+\d+\b", lower):
        return "page_of_total"
    # OCR/stamp artifacts: many isolated single-letter tokens on one line.
    if re.search(r"(?:\b[A-Za-z]\b\s*){4,}", value):
        return "isolated_letters"
    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)
    if len(tokens) >= 6:
        single_letter_tokens = sum(1 for token in tokens if re.fullmatch(r"[A-Za-z]", token))
        short_cjk_tokens = sum(1 for token in tokens if re.fullmatch(r"[\u4e00-\u9fff]", token))
        if single_letter_tokens + short_cjk_tokens >= int(len(tokens) * 0.7):
            return "token_fragmentation"
    digit_count = sum(ch.isdigit() for ch in value)
    has_alpha_or_cjk = bool(re.search(r"[A-Za-z\u4e00-\u9fff]", value))
    if digit_count >= max(6, len(value) // 2) and not has_alpha_or_cjk:
        return "digit_dense"
    if _looks_like_spaced_watermark(value):
        return "watermark_spaced"
    for pattern in WATERMARK_PATTERNS:
        if pattern.search(value):
            return "watermark_pattern"
    return ""


def _looks_like_spaced_watermark(value: str) -> bool:
    compact_letters = re.sub(r"[^A-Za-z]", "", value).lower()
    if len(compact_letters) < 8:
        return False
    if not any(keyword in compact_letters for keyword in WATERMARK_COMPACT_KEYWORDS):
        return False
    if re.search(r"(?:\b[A-Za-z]\b[\s\-_]*){6,}", value):
        return True
    if value.upper() == value and len(value) <= 48:
        return True
    return False


def _inject_missing_section_headings(
    chosen_lines: list[str],
    candidates: list[dict[str, Any]],
    chosen_engine: str,
) -> tuple[list[str], int]:
    if not chosen_lines:
        return chosen_lines, 0
    chosen_codes = {_section_code_from_line(line) for line in chosen_lines}
    chosen_codes.discard("")

    supplemental: list[tuple[str, str]] = []
    seen_codes = set(chosen_codes)
    for candidate in candidates:
        if str(candidate.get("engine")) == str(chosen_engine):
            continue
        lines = candidate.get("lines") or []
        for code, normalized_heading in _extract_section_heading_candidates(lines):
            if not code or code in seen_codes:
                continue
            # Only inject minor-section headings from alternate engines.
            if "." not in code:
                continue
            seen_codes.add(code)
            supplemental.append((code, normalized_heading))

    merged_lines = list(chosen_lines)
    injected_count = 0

    if supplemental:
        supplemental_sorted = sorted(supplemental, key=lambda item: _section_code_sort_key(item[0]))
        insert_at = _first_section_header_index(chosen_lines)
        for _, heading_line in supplemental_sorted:
            merged_lines.insert(insert_at, heading_line)
            insert_at += 1
        injected_count += len(supplemental_sorted)

    merged_lines, enum_injected = _inject_missing_enum_subheading_blocks(
        merged_lines=merged_lines,
        candidates=candidates,
        chosen_engine=chosen_engine,
    )
    injected_count += enum_injected
    merged_lines, detail_injected = _inject_missing_section_detail_blocks(
        merged_lines=merged_lines,
        candidates=candidates,
        chosen_engine=chosen_engine,
    )
    injected_count += detail_injected
    return merged_lines, injected_count


def _inject_missing_table_blocks(
    chosen_lines: list[str],
    candidates: list[dict[str, Any]],
    chosen_engine: str,
) -> tuple[list[str], int, float]:
    if not chosen_lines:
        return chosen_lines, 0, 0.0
    # Preserve existing table-rich pages; inject only when chosen page has no
    # reliable markdown table rows.
    if _count_markdown_table_rows(chosen_lines) >= 2:
        cleaned, dropped = _drop_mega_duplicate_table_rows(chosen_lines)
        return cleaned, 0, 0.0 if dropped <= 0 else 0.0

    block_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        if str(candidate.get("engine")) == str(chosen_engine):
            continue
        cand_lines = [_normalize_line(str(line or "")) for line in candidate.get("lines") or []]
        cand_lines = [line for line in cand_lines if line]
        if not cand_lines:
            continue
        for block in _extract_candidate_table_blocks(cand_lines):
            score = _score_table_block(block["lines"])
            if score < 0.52:
                continue
            block_candidates.append(
                {
                    "engine": str(candidate.get("engine") or ""),
                    "lines": block["lines"],
                    "anchor_before": block.get("anchor_before", ""),
                    "score": score,
                }
            )

    if not block_candidates:
        cleaned, dropped = _drop_mega_duplicate_table_rows(chosen_lines)
        return cleaned, 0, 0.0 if dropped <= 0 else 0.0

    block_candidates.sort(
        key=lambda row: (
            float(row.get("score", 0.0)),
            len(row.get("lines", [])),
        ),
        reverse=True,
    )

    merged = list(chosen_lines)
    existing_table_keys = {
        _line_merge_key(line)
        for line in merged
        if _looks_like_markdown_row(line) and _line_merge_key(line)
    }
    injected_blocks = 0
    best_quality = 0.0
    for block in block_candidates:
        if injected_blocks >= 2:
            break
        block_lines = [
            line
            for line in list(block.get("lines", []))
            if line and not _is_mega_duplicate_table_row(line) and not _is_suspicious_injected_table_line(line)
        ]
        if len(block_lines) < 2:
            continue
        if not _is_injectable_table_block(block_lines):
            continue
        unique_lines: list[str] = []
        for line in block_lines:
            key = _line_merge_key(line)
            if not key:
                continue
            if key in existing_table_keys:
                continue
            unique_lines.append(line)
            existing_table_keys.add(key)
        if len(unique_lines) < 2:
            continue

        anchor_before = str(block.get("anchor_before") or "")
        insert_at = len(merged)
        anchor_found = False
        if anchor_before:
            anchor_key = _line_merge_key(anchor_before)
            found = _find_anchor_index_in_lines(merged, anchor_key)
            if found >= 0:
                insert_at = found + 1
                anchor_found = True
        if _table_block_requires_anchor(block_lines) and not anchor_found:
            continue
        merged[insert_at:insert_at] = unique_lines
        injected_blocks += 1
        best_quality = max(best_quality, float(block.get("score", 0.0)))

    cleaned, dropped = _drop_mega_duplicate_table_rows(merged)
    if dropped > 0:
        best_quality = max(best_quality, 0.5)
    return cleaned, injected_blocks, round(best_quality, 4)


def _extract_candidate_table_blocks(lines: list[str]) -> list[dict[str, Any]]:
    if not lines:
        return []
    blocks: list[dict[str, Any]] = []
    idx = 0
    while idx < len(lines):
        if not _looks_like_markdown_row(lines[idx]):
            idx += 1
            continue
        start = idx
        rows: list[str] = []
        while idx < len(lines) and _looks_like_markdown_row(lines[idx]):
            rows.append(lines[idx])
            idx += 1
        if len(rows) < 2:
            continue
        for chunk in _split_candidate_table_chunks(rows):
            if len(chunk) < 2:
                continue
            anchor_before = ""
            for back in range(start - 1, max(-1, start - 8), -1):
                candidate = _normalize_line(lines[back])
                if not candidate:
                    continue
                if _looks_like_markdown_row(candidate):
                    continue
                if _noise_reason(candidate):
                    continue
                anchor_before = candidate
                break
            blocks.append({"lines": chunk, "anchor_before": anchor_before})
    return blocks


def _split_candidate_table_chunks(rows: list[str]) -> list[list[str]]:
    if not rows:
        return []
    out: list[list[str]] = []
    current: list[str] = []
    has_body = False
    for line in rows:
        cells = _parse_markdown_cells(line)
        if not cells:
            continue
        header_like = _is_header_like_cells(cells)
        if current and header_like and has_body:
            out.append(current)
            current = []
            has_body = False
        current.append(line)
        if not header_like:
            has_body = True
    if current:
        out.append(current)
    return out


def _score_table_block(lines: list[str]) -> float:
    rows = [_parse_markdown_cells(line) for line in lines]
    rows = [cells for cells in rows if len(cells) >= 2]
    if len(rows) < 2:
        return 0.0
    col_counts = [len(cells) for cells in rows]
    median_cols = sorted(col_counts)[len(col_counts) // 2]
    if median_cols < 2:
        return 0.0
    rows_count = len(rows)
    header_bonus = 0.2 if _is_header_like_cells(rows[0]) else 0.0
    consistency = sum(1 for count in col_counts if abs(count - median_cols) <= 1) / rows_count
    long_cell_count = sum(1 for cells in rows for cell in cells if len(cell) >= 260)
    total_cells = sum(len(cells) for cells in rows)
    long_cell_ratio = long_cell_count / max(1, total_cells)
    duplicate_row_ratio = 1.0 - (len({_line_merge_key("|" + "|".join(cells) + "|") for cells in rows}) / rows_count)
    score = (
        0.35 * min(1.0, rows_count / 8.0)
        + 0.35 * consistency
        + header_bonus
        - 0.25 * long_cell_ratio
        - 0.2 * max(0.0, duplicate_row_ratio)
    )
    return max(0.0, min(1.0, round(score, 4)))


def _count_markdown_table_rows(lines: list[str]) -> int:
    return sum(1 for line in lines if _looks_like_markdown_row(line))


def _looks_like_markdown_row(line: str) -> bool:
    return len(_parse_markdown_cells(line)) >= 2


def _parse_markdown_cells(line: str) -> list[str]:
    value = str(line or "").strip()
    if not value or "|" not in value:
        return []
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    cells = [part.strip() for part in value.split("|")]
    return [cell for cell in cells if cell]


def _is_header_like_cells(cells: list[str]) -> bool:
    if not cells:
        return False
    joined = "|".join(cells).lower()
    if "序号" in joined and "项目" in joined and "要求" in joined:
        return True
    if "序号" in joined and "层级" in joined and "代号" in joined and "版本" in joined:
        return True
    if "项目代号" in joined and "车型代号" in joined:
        return True
    if "test item" in joined and "requirements" in joined:
        return True
    return False


def _is_injectable_table_block(lines: list[str]) -> bool:
    if len(lines) < 2:
        return False
    header_cells = _parse_markdown_cells(lines[0])
    if _is_header_like_cells(header_cells):
        return True
    # Also allow very compact two-row matrix blocks on title pages.
    if len(lines) == 2:
        cells0 = _parse_markdown_cells(lines[0])
        cells1 = _parse_markdown_cells(lines[1])
        if len(cells0) >= 3 and len(cells1) >= 3 and len(cells0) == len(cells1):
            return True
    return False


def _drop_mega_duplicate_table_rows(lines: list[str]) -> tuple[list[str], int]:
    if not lines:
        return lines, 0
    out: list[str] = []
    dropped = 0
    for line in lines:
        if _is_mega_duplicate_table_row(line):
            dropped += 1
            continue
        out.append(line)
    return out, dropped


def _is_mega_duplicate_table_row(line: str) -> bool:
    text = _normalize_line(str(line or ""))
    if not text or "|" not in text:
        return False
    cells = _parse_markdown_cells(text)
    if len(cells) < 3:
        return False
    cell_keys = [_line_merge_key(cell) for cell in cells if _line_merge_key(cell)]
    unique_keys = set(cell_keys)
    line_len = len(text)
    has_long_cell = any(len(cell) >= 260 for cell in cells)
    if line_len >= 1500 and has_long_cell:
        return True
    if has_long_cell and len(unique_keys) <= 2 and len(cells) >= 3:
        return True
    if "产品详细描述" in text and text.count("3.2.2") >= 2:
        return True
    return False


def _is_suspicious_injected_table_line(line: str) -> bool:
    text = _normalize_line(str(line or ""))
    if not text:
        return True
    if "|" not in text:
        return False
    if len(text) < 320:
        return False
    section_code_hits = len(re.findall(r"\b\d+(?:\.\d+){1,3}\b", text))
    if section_code_hits >= 3:
        return True
    if text.count("|") >= 8 and re.search(r"(?:车型信息|产品技术要求及说明|产品详细描述)", text):
        return True
    return False


def _table_block_requires_anchor(lines: list[str]) -> bool:
    if not lines:
        return False
    header = _normalize_line(str(lines[0] or ""))
    if not header:
        return False
    compact = re.sub(r"\s+", "", header)
    if "项目代号" in compact and "车型代号" in compact:
        return True
    if len(lines) <= 2 and len(header) >= 64:
        return True
    return False


def _inject_missing_enum_subheading_blocks(
    merged_lines: list[str],
    candidates: list[dict[str, Any]],
    chosen_engine: str,
) -> tuple[list[str], int]:
    if not merged_lines:
        return merged_lines, 0
    if not any("|" in str(line) for line in merged_lines):
        return merged_lines, 0

    merged = list(merged_lines)
    existing_keys = {_line_merge_key(line) for line in merged if _line_merge_key(line)}
    existing_enum_keys = {_enum_heading_key(line) for line in merged if _enum_heading_key(line)}
    pending_insertions: list[tuple[int, list[str]]] = []

    for candidate in candidates:
        if str(candidate.get("engine")) == str(chosen_engine):
            continue
        cand_lines = [_normalize_line(str(line or "")) for line in candidate.get("lines") or []]
        cand_lines = [line for line in cand_lines if line]
        if not cand_lines:
            continue
        idx = 0
        while idx < len(cand_lines):
            line = cand_lines[idx]
            enum_key = _enum_heading_key(line)
            if not enum_key:
                idx += 1
                continue
            if enum_key in existing_enum_keys:
                idx += 1
                continue
            if not _enum_heading_is_important(line):
                idx += 1
                continue

            block = [line]
            scan = idx + 1
            while scan < len(cand_lines) and len(block) < 6:
                next_line = cand_lines[scan]
                if not next_line:
                    break
                if _enum_heading_key(next_line):
                    break
                if _is_structural_section_heading_line(next_line):
                    break
                if next_line.startswith("|"):
                    break
                if PLAIN_TABLE_HEADER_RE.match(next_line):
                    break
                if _looks_like_non_content_fragment(next_line):
                    scan += 1
                    continue
                if _looks_like_enum_detail_line(next_line):
                    block.append(next_line)
                    scan += 1
                    continue
                if len(block) == 1 and len(next_line) <= 180:
                    block.append(next_line)
                    scan += 1
                    continue
                break

            unique_block: list[str] = []
            for item in block:
                key = _line_merge_key(item)
                if not key or key in existing_keys:
                    continue
                unique_block.append(item)
                existing_keys.add(key)
            if unique_block:
                insert_at = _locate_enum_block_insert_index(merged, cand_lines, idx)
                pending_insertions.append((insert_at, unique_block))
                existing_enum_keys.add(enum_key)
            idx = max(scan, idx + 1)

    if not pending_insertions:
        return merged_lines, 0

    injected_lines = 0
    offset = 0
    for insert_at, block in sorted(pending_insertions, key=lambda item: item[0]):
        position = max(0, min(len(merged), insert_at + offset))
        merged[position:position] = block
        offset += len(block)
        injected_lines += len(block)
    return merged, injected_lines


def _inject_missing_section_detail_blocks(
    merged_lines: list[str],
    candidates: list[dict[str, Any]],
    chosen_engine: str,
) -> tuple[list[str], int]:
    if not merged_lines:
        return merged_lines, 0
    if not any("|" in str(line) for line in merged_lines):
        return merged_lines, 0

    merged = list(merged_lines)
    existing_keys = {_line_merge_key(line) for line in merged if _line_merge_key(line)}
    heading_indices = [idx for idx, line in enumerate(merged) if _section_code_from_line(line)]
    if not heading_indices:
        return merged_lines, 0

    pending_insertions: list[tuple[int, list[str]]] = []
    for heading_index in heading_indices:
        line = merged[heading_index]
        code = _section_code_from_line(line)
        if not code or "." not in code:
            continue
        if _has_local_section_detail(merged, heading_index):
            continue

        best_block: list[str] = []
        for candidate in candidates:
            if str(candidate.get("engine")) == str(chosen_engine):
                continue
            cand_lines = [_normalize_line(str(item or "")) for item in candidate.get("lines") or []]
            cand_lines = [item for item in cand_lines if item]
            if not cand_lines:
                continue

            block = _extract_section_detail_block_from_candidate(cand_lines, code)
            if not block:
                continue

            unique_block: list[str] = []
            for item in block:
                key = _line_merge_key(item)
                if not key or key in existing_keys:
                    continue
                unique_block.append(item)
            if len(unique_block) > len(best_block):
                best_block = unique_block

        if best_block:
            for item in best_block:
                key = _line_merge_key(item)
                if key:
                    existing_keys.add(key)
            pending_insertions.append((heading_index + 1, best_block))

    if not pending_insertions:
        return merged_lines, 0

    injected_lines = 0
    offset = 0
    for insert_at, block in sorted(pending_insertions, key=lambda item: item[0]):
        position = max(0, min(len(merged), insert_at + offset))
        merged[position:position] = block
        offset += len(block)
        injected_lines += len(block)
    return merged, injected_lines


def _has_local_section_detail(lines: list[str], heading_index: int) -> bool:
    next_heading = len(lines)
    for idx in range(heading_index + 1, len(lines)):
        if _section_code_from_line(lines[idx]):
            next_heading = idx
            break
    for idx in range(heading_index + 1, next_heading):
        value = _normalize_line(str(lines[idx] or ""))
        if not value:
            continue
        if _looks_like_non_content_fragment(value):
            continue
        return True
    return False


def _extract_section_detail_block_from_candidate(candidate_lines: list[str], section_code: str) -> list[str]:
    match_index = -1
    for idx, line in enumerate(candidate_lines):
        if _section_code_from_line(line) == section_code:
            match_index = idx
            break
    if match_index < 0:
        return []

    block: list[str] = []
    total_chars = 0
    for idx in range(match_index + 1, len(candidate_lines)):
        line = candidate_lines[idx]
        if _section_code_from_line(line):
            break
        if line.startswith("|"):
            # Avoid injecting table shells as detail lines.
            continue
        if _looks_like_non_content_fragment(line):
            continue
        if _noise_reason(line):
            continue
        block.append(line)
        total_chars += len(line)
        if len(block) >= 16 or total_chars >= 2200:
            break
    return block


def _enum_heading_key(line: str) -> str:
    value = _normalize_line(str(line or ""))
    if "|" in value:
        cells = [part.strip() for part in value.split("|") if part.strip()]
        if cells:
            keys = {_line_merge_key(cell) for cell in cells if _line_merge_key(cell)}
            if len(keys) == 1:
                value = cells[0]
    match = ENUM_SUBHEADING_RE.match(value)
    if not match:
        return ""
    index_no = (match.group(1) or "").strip()
    title = _line_merge_key(match.group(2) or "")
    if not index_no or not title:
        return ""
    return f"{index_no}:{title[:48]}"


def _enum_heading_is_important(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    match = ENUM_SUBHEADING_RE.match(value)
    if not match:
        return False
    code = str(match.group(1) or "").strip()
    code_parts: list[int] = []
    for token in code.split("."):
        if not token.isdigit():
            return False
        code_parts.append(int(token))
    if not code_parts:
        return False
    enum_no = code_parts[0]
    # Keep only early structural bullet headings to avoid injecting long tail list noise.
    if enum_no < 3 or enum_no > 15:
        return False
    title = (match.group(2) or "").strip()
    if not title or len(title) > 160:
        return False
    if not re.search(r"[\u4e00-\u9fffA-Za-z]", title):
        return False
    if ENUM_IMPORTANT_TITLE_RE.search(title):
        return True

    # Keep short nested clauses (e.g. 10.1/11.2) to preserve completeness.
    if len(code_parts) >= 2 and len(title) <= 72 and not re.search(r"[。；;]", title):
        return True
    return False


def _looks_like_enum_detail_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if ENUM_DETAIL_HINT_RE.search(value):
        return True
    if re.match(r"^\s*(?:[（(]?\d+[）)]|[-•])\s*", value):
        return True
    if re.match(r"^\s*\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s|h)\b", value, re.I):
        return True
    return False


def _line_merge_key(line: str) -> str:
    value = _normalize_line(str(line or ""))
    value = value.lower()
    value = value.replace(" ", "")
    value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
    return value[:280]


def _locate_enum_block_insert_index(merged_lines: list[str], candidate_lines: list[str], enum_index: int) -> int:
    for back in range(enum_index - 1, max(-1, enum_index - 10), -1):
        if not _is_enum_anchor_candidate(candidate_lines[back]):
            continue
        anchor = _line_merge_key(candidate_lines[back])
        if not anchor:
            continue
        found = _find_anchor_index_in_lines(merged_lines, anchor)
        if found >= 0:
            return found + 1

    for fwd in range(enum_index + 1, min(len(candidate_lines), enum_index + 12)):
        if not _is_enum_anchor_candidate(candidate_lines[fwd]):
            continue
        anchor = _line_merge_key(candidate_lines[fwd])
        if not anchor:
            continue
        found = _find_anchor_index_in_lines(merged_lines, anchor)
        if found >= 0:
            return found

    return len(merged_lines)


def _find_anchor_index_in_lines(lines: list[str], anchor_key: str) -> int:
    if not anchor_key:
        return -1
    for idx, line in enumerate(lines):
        candidate = _line_merge_key(line)
        if not candidate:
            continue
        short_len = min(len(candidate), len(anchor_key))
        if short_len < 12:
            continue
        if candidate == anchor_key:
            return idx
        if candidate in anchor_key or anchor_key in candidate:
            return idx
    return -1


def _looks_like_non_content_fragment(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return True
    if len(value) <= 2:
        return True
    if MARKDOWN_TABLE_RULE_RE.match(value):
        return True
    if re.fullmatch(r"[\|\-_/ ]+", value):
        return True
    return False


def _is_enum_anchor_candidate(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if len(value) < 10:
        return False
    if _looks_like_non_content_fragment(value):
        return False
    if re.match(r"^\s*\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s)\b", value, re.I):
        return False
    if re.match(r"^\s*(?:[（(]?\d+[）)]|[-•])\s*", value):
        return False
    return True


def _is_structural_section_heading_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if re.match(r"^\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s)\b", value, re.I):
        return False
    match = SECTION_CANDIDATE_RE.match(value)
    if not match:
        return False
    code = (match.group(1) or "").strip()
    title = _clean_heading_title(match.group(2) or "")
    try:
        code_parts = [int(part) for part in code.split(".") if part]
    except ValueError:
        return False
    if not code_parts:
        return False
    if code_parts[0] == 0:
        return False
    if len(code_parts) == 1 and code_parts[0] > 40:
        return False
    if len(code_parts) >= 2 and code_parts[0] > 90:
        return False
    if not title:
        return False
    if len(title) > 160:
        return False
    if re.match(r"^(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s)\b", title, re.I):
        return False
    if "@" in title and re.search(r"(?:m/s|mm|kN|A|V|℃|°C)", title, re.I):
        return False
    return True


def _extract_section_heading_candidates(lines: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, line in enumerate(lines):
        match = SECTION_CANDIDATE_RE.match(line.strip())
        if not match:
            continue
        code = (match.group(1) or "").strip()
        if not code or code in seen:
            continue
        raw_title = (match.group(2) or "").strip()
        title = _clean_heading_title(raw_title)
        if _looks_like_noisy_heading_title(title):
            next_title = _next_heading_title_hint(lines, idx + 1)
            if next_title:
                title = next_title
        if not title:
            continue
        if not re.search(r"[A-Za-z\u4e00-\u9fff]", title):
            continue
        if len(title) > 140:
            title = title[:140].strip()
        seen.add(code)
        out.append((code, f"{code} {title}"))
    return out


def _next_heading_title_hint(lines: list[str], start_index: int) -> str:
    for idx in range(start_index, min(len(lines), start_index + 4)):
        candidate = lines[idx].strip()
        if not candidate:
            continue
        if SECTION_CANDIDATE_RE.match(candidate):
            return ""
        value = _clean_heading_title(candidate)
        if _looks_like_noisy_heading_title(value):
            continue
        return value
    return ""


def _clean_heading_title(title: str) -> str:
    value = title.strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"^[\|\-:：,，;；\. ]+", "", value)
    return value


def _looks_like_noisy_heading_title(title: str) -> bool:
    if not title:
        return True
    if re.search(r"[\u4e00-\u9fff]{2,}", title):
        return False
    if re.search(r"[A-Za-z]{3,}", title):
        return False
    if re.search(r"[A-Za-z]{2,}\.\d+", title):
        return False
    return True


def _section_code_from_line(line: str) -> str:
    match = SECTION_CANDIDATE_RE.match((line or "").strip())
    return (match.group(1) or "").strip() if match else ""


def _section_code_sort_key(code: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(code).split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(9999)
    return tuple(parts)


def _first_section_header_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if _section_code_from_line(line):
            return idx
    return 0


def _segment_quality(segments: list[dict[str, Any]]) -> float:
    lines: list[str] = []
    for seg in segments:
        for line in seg.get("text", "").splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    if not lines:
        return -999.0
    total = len(lines)
    long_ratio = sum(1 for line in lines if len(line) >= 22) / total
    short_ratio = sum(1 for line in lines if len(line) <= 3) / total
    alpha_ratio = sum(1 for line in lines if any(ch.isalpha() for ch in line)) / total
    return long_ratio * 1.4 + alpha_ratio * 0.6 - short_ratio * 1.8


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0
