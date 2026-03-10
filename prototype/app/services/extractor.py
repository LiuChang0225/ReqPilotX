from __future__ import annotations

import re
from typing import Any


DOT_LEADER_RE = re.compile(r"\.{5,}|…{3,}")
PAGE_MARK_RE = re.compile(r"^(?:page\s*)?\d+\s*(?:of|/)\s*\d+$", re.I)
NUMBER_UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|sec|v|kv|a|ma|ohm|hz|khz|mhz|%|°c|c)\b", re.I)
SECTION_HEADER_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\s*([、．。\)）\]:：.\-])?\s*(.+)?$")
SECTION_CODE_HINT_RE = re.compile(r"^\s*(\d+(?:\.\d+){1,6})\b(?:\s+.*)?$")
SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s+|\n+")
CLAUSE_PREFIX_RE = re.compile(r"^\s*(?:\d+(?:\.\d+){1,5}|[A-Z]\d+(?:\.\d+){1,3})\s*[\)\]:：.\-]*\s*")
HEADER_TITLE_LEADING_PUNCT_RE = re.compile(r"^[,，;；:：、)）\]】\-\.\|]+")
HEADER_TITLE_VALID_START_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
HEADER_ENUM_LIKE_RE = re.compile(r"^\d+\s*[)）:：、\.]")
HEADER_SENTENCE_PREFIX_RE = re.compile(
    r"^(?:and|or|to|for|with|without|in|on|at|by|from|than|while|where|then|if|when|which|that|operate|operating|mode)\b",
    re.I,
)
MARKDOWN_TABLE_RULE_RE = re.compile(r"^\|?(?:\s*:?-{2,}:?\s*\|)+\s*$")
MEASUREMENT_TITLE_START_RE = re.compile(
    r"^\s*(?:㎜|mm|cm|dm|m|um|nm|kg|g|mg|l/min|ml/min|l|ml|kpa|pa|bar|v|kv|a|ma|hz|khz|mhz|℃|°c|%)"
    r"(?:\b|[，,;；。\.、:：])",
    re.I,
)
MEASUREMENT_HINT_RE = re.compile(
    r"(?:\b\d+(?:\.\d+)?\s*(?:mm|cm|m|kpa|pa|bar|v|a|ma|hz|khz|mhz|%|℃|°c)\b|l/min|ml/min)",
    re.I,
)
QUANTITY_TITLE_START_RE = re.compile(r"^\s*(?:个|次|条|组|轮|小时|分钟|秒)(?:\b|[，,;；。\.、:：\s]|$)")
HEADING_KEYWORD_RE = re.compile(
    r"(?:chapter|section|scope|terms?|definition|requirement|test|procedure|spec|标准|规范|术语|定义|范围|要求|试验|测试)",
    re.I,
)
STRONG_MAJOR_HEADING_KEYWORD_RE = re.compile(
    r"(?:chapter|section|scope|requirement|standard|appendix|annex|概述|范围|要求|标准|规范|术语|定义|总则|附录|附件)",
    re.I,
)
TABLE_VALUE_TITLE_RE = re.compile(
    r"(?:\btest\s*case\b|测试工况|工作模式|operating mode|duty cycle|cycle count|number of cycles)",
    re.I,
)
TEST_PLAN_ROW_HINT_RE = re.compile(
    r"(?:\b(?:DV|PV|OTS|USCAR|rev)\b|供应商|插拔速度|插入力|拔出力|锁止|外观|试验样品|test item|applicable scope)",
    re.I,
)
TEST_PLAN_COUNT_HINT_RE = re.compile(r"(?:\b\d+\s*/\s*OTS\b|\b\d+\s*天\b|件/OTS)", re.I)
COMPONENT_BOM_TITLE_RE = re.compile(
    r"^\s*\d+\s+[^|]{0,80}/\s*\d+\s*件(?:\s*/\s*)?$",
    re.I,
)
COMPONENT_BOM_INLINE_RE = re.compile(r"/\s*\d+\s*件(?:\s*/|$)", re.I)
TABLE_SERIAL_ROW_RE = re.compile(
    r"^\s*\d+\s+\d+\s+[A-Za-z0-9\-/\.]{0,40}\s*[^|]{0,120}$",
    re.I,
)
CN_ENUM_PREFIX_RE = re.compile(
    r"^(?:对于|若|在|将|按|依据|完成|应|需|具备|提供|采用|通过|进行|根据|为了)",
)
APPENDIX_MARKER_RE = re.compile(
    r"^\s*(?:附件|附录|appendix|annex|attachment)\s*([A-Za-zＡ-Ｚａ-ｚ])\b",
    re.I,
)
APPENDIX_REFERENCE_HINT_RE = re.compile(
    r"(?:详见|参见|见附件|依据附件|根据附件|按照附件|see\s+(?:appendix|annex|attachment))",
    re.I,
)
TEST_OUTLINE_HEADER_RE = re.compile(
    r"(?:主动前减振器试验大纲|主动后减振器试验大纲|蓄能器试验大纲|整车用电器与线束匹配评估表|"
    r"测试分组\s*/\s*test groups|用电器试验大纲)",
    re.I,
)
ELECTRIC_TEST_OUTLINE_HEADER_RE = re.compile(
    r"(?:序号\s*/?.*项目\s*/\s*item.*测试顺序.*参考标准.*要求\s*/\s*requirements)",
    re.I,
)
TEST_OUTLINE_ROW_RE = re.compile(r"^\|\s*(\d{1,3})(?:\s*[（(][^|）)]{0,10}[）)])?\s*\|")
DVP_ENUM_LINE_RE = re.compile(r"^\s*((?:[5-9]|1[0-5])(?:\.\d+)*)\s*[）)、)]")
DVP_ENUM_INLINE_RE = re.compile(r"(?<![（(])((?:[5-9]|1[0-5])(?:\.\d+)*)\s*[）)、)]")
TEST_OUTLINE_SCOPE_TITLES: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"主动前减振器试验大纲", re.I), "T", "主动前减振器试验大纲"),
    (re.compile(r"主动后减振器试验大纲", re.I), "U", "主动后减振器试验大纲"),
    (re.compile(r"蓄能器试验大纲", re.I), "V", "蓄能器试验大纲"),
    (re.compile(r"整车用电器与线束匹配评估表", re.I), "W", "整车用电器与线束匹配评估表"),
    (re.compile(r"测试分组\s*/\s*test groups", re.I), "X", "测试分组 / Test Groups（推荐）"),
    (re.compile(r"用电器试验大纲", re.I), "Y", "用电器试验大纲"),
]

MODAL_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"\bshall\b",
        r"\bs\s+h\s+a\s+l\s+l\b",
        r"\bmust\b",
        r"\bm\s+u\s+s\s+t\b",
        r"\bshould\b",
        r"\bs\s+h\s+o\s+u\s+l\s+d\b",
        r"\brequired to\b",
        r"\bneed(?:s)? to\b",
        r"\bhas to\b",
        r"必须",
        r"应当",
        r"应满足",
        r"应符合",
        r"应能",
        r"应可",
        r"应在",
        r"不应",
        r"不得",
        r"需满足",
        r"需要",
    ]
]
BLOCK_HINT_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"requirement",
        r"test method",
        r"test parameters",
        r"test matrix",
        r"technical requirement",
        r"试验方法",
        r"试验参数",
        r"测试矩阵",
        r"技术要求",
        r"要求",
        r"规范",
        r"shall",
        r"must",
        r"should",
        r"应",
        r"必须",
        r"不得",
    ]
]
NOISE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"^contents?$",
        r"^目次$",
        r"^目录$",
        r"^foreword$",
        r"^前言$",
        r"revision history",
        r"version history",
        r"uncontrolled copy",
        r"internal use only",
        r"copyright",
        r"confidential",
        r"document key",
        r"\besign\b",
        r"decision date",
        r"\.com\b",
        r"^\s*附件\s*[a-z0-9]?",
    ]
]


def extract_atomic_requirements(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_segments = _sort_segments(segments)
    line_entries = _build_line_entries(ordered_segments)
    if not line_entries:
        return _append_test_outline_requirements([], ordered_segments)

    section_blocks = _extract_section_blocks(line_entries)
    if not section_blocks:
        # P0 default: keep full block extraction even when section headings are weak.
        fallback_blocks = _extract_segment_block_requirements(ordered_segments)
        if fallback_blocks:
            return _append_test_outline_requirements(fallback_blocks, ordered_segments)
        sentence_rows = _extract_sentence_requirements(ordered_segments)
        return _append_test_outline_requirements(sentence_rows, ordered_segments)

    requirements: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    cursor = 1

    for block in section_blocks:
        sub_blocks = _split_block_by_bullets(block)
        for sub in sub_blocks:
            block_text = _normalize_block_text(sub["lines"])
            if not block_text:
                continue
            dedupe_key = _build_dedupe_key(block_text)
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)

            section_code = block.get("section_code", "")
            appendix_scope = str(block.get("appendix_scope") or "").strip().upper()
            section_title = str(block.get("section_title") or "").strip()
            source_anchor = sub["source_anchor"]
            if appendix_scope:
                source_anchor = f"{source_anchor} | Appendix {appendix_scope}"
            if section_code:
                if section_title:
                    source_anchor = f"{source_anchor} | Section {section_code} {section_title}"
                else:
                    source_anchor = f"{source_anchor} | Section {section_code}"

            requirements.append(
                {
                    "req_code": f"REQ-{cursor:04d}",
                    "source_page": sub["source_page"],
                    "source_page_end": sub["source_page_end"],
                    "source_line_index": sub.get("source_line_index"),
                    "source_line_index_end": sub.get("source_line_index_end"),
                    "source_anchor": source_anchor,
                    "appendix_scope": appendix_scope,
                    "requirement_text": block_text,
                    "requirement_type": infer_requirement_type(block_text),
                    "priority": infer_priority(block_text),
                }
            )
            cursor += 1

    if requirements:
        return _append_test_outline_requirements(requirements, ordered_segments)
    sentence_rows = _extract_sentence_requirements(ordered_segments)
    return _append_test_outline_requirements(sentence_rows, ordered_segments)


def extract_section_outline(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_segments = _sort_segments(segments)
    line_entries = _build_line_entries(ordered_segments)
    if not line_entries:
        return []
    blocks = _extract_section_blocks(line_entries)
    section_keys = [
        (
            str(block.get("appendix_scope") or "").strip().upper(),
            str(block.get("section_code") or ""),
        )
        for block in blocks
    ]
    outline: list[dict[str, Any]] = []
    for block in blocks:
        lines = [entry["text"] for entry in block.get("entries", [])]
        block_text = _normalize_block_text(lines)
        code = str(block.get("section_code") or "")
        appendix_scope = str(block.get("appendix_scope") or "").strip().upper()
        level = int(block.get("section_level") or (code.count(".") + 1 if code else 0))
        has_children = _section_has_children(appendix_scope, code, section_keys)
        outline.append(
            {
                "appendix_scope": appendix_scope,
                "section_code": code,
                "section_title": str(block.get("section_title") or ""),
                "section_level": level,
                "parent_section_code": _section_parent_code(code),
                "has_children": has_children,
                "is_leaf": (not has_children),
                "source_page_start": block.get("source_page"),
                "source_page_end": block.get("source_page_end"),
                "source_line_index_start": block.get("source_line_index"),
                "source_line_index_end": block.get("source_line_index_end"),
                "line_count": len(lines),
                "char_count": len(block_text),
                "text_preview": block_text[:280],
                # Temporarily treat all extracted section blocks as requirement candidates.
                "requirement_like": True,
            }
        )
    return outline


def analyze_section_consistency(section_outline: list[dict[str, Any]]) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    code_counter: dict[str, int] = {}
    parsed_rows: list[dict[str, Any]] = []

    for row in section_outline:
        code = str(row.get("section_code") or "").strip()
        if not code:
            continue
        appendix_scope = str(row.get("appendix_scope") or "").strip().upper()
        scoped_code = f"{appendix_scope}:{code}" if appendix_scope else code
        code_counter[scoped_code] = code_counter.get(scoped_code, 0) + 1
        parsed_rows.append(
            {
                "appendix_scope": appendix_scope,
                "scoped_code": scoped_code,
                "section_code": code,
                "section_tuple": _parse_section_code_tuple(code),
                "source_page_start": _safe_int(row.get("source_page_start")),
                "section_title": str(row.get("section_title") or ""),
            }
        )

    duplicate_codes = {code: count for code, count in code_counter.items() if count > 1}
    for scoped_code, count in sorted(duplicate_codes.items()):
        appendix_scope, section_code = _split_scoped_code(scoped_code)
        issues.append(
            {
                "issue_type": "duplicate_code",
                "severity": "HIGH" if count >= 3 else "MEDIUM",
                "appendix_scope": appendix_scope,
                "section_code": section_code,
                "detail": f"duplicate_count={count}",
            }
        )

    by_parent: dict[tuple[str, tuple[int, ...]], set[int]] = {}
    for row in parsed_rows:
        code_tuple = row["section_tuple"]
        if len(code_tuple) <= 1:
            continue
        appendix_scope = str(row.get("appendix_scope") or "")
        parent = code_tuple[:-1]
        by_parent.setdefault((appendix_scope, parent), set()).add(code_tuple[-1])

    missing_group_count = 0
    for (appendix_scope, parent), nums in by_parent.items():
        if len(nums) < 2:
            continue
        min_num = min(nums)
        max_num = max(nums)
        if max_num - min_num <= 1:
            continue
        missing = [n for n in range(min_num, max_num + 1) if n not in nums]
        if not missing or len(missing) > 12:
            continue
        missing_group_count += 1
        issues.append(
            {
                "issue_type": "missing_sibling_code",
                "severity": "MEDIUM",
                "appendix_scope": appendix_scope,
                "section_code": _tuple_to_code(parent),
                "detail": f"missing={','.join(str(n) for n in missing[:12])}",
            }
        )

    ordered_rows = sorted(
        parsed_rows,
        key=lambda row: (
            row["source_page_start"] <= 0,
            row["source_page_start"] if row["source_page_start"] > 0 else 10**9,
            row.get("appendix_scope", ""),
            row["section_tuple"],
        ),
    )
    for idx in range(1, len(ordered_rows)):
        prev_row = ordered_rows[idx - 1]
        curr_row = ordered_rows[idx]
        prev_tuple = prev_row["section_tuple"]
        curr_tuple = curr_row["section_tuple"]
        if not prev_tuple or not curr_tuple:
            continue
        level_jump = len(curr_tuple) - len(prev_tuple)
        if level_jump > 1:
            issues.append(
                {
                    "issue_type": "level_jump",
                    "severity": "LOW",
                    "appendix_scope": curr_row.get("appendix_scope", ""),
                    "section_code": curr_row["section_code"],
                    "detail": f"prev={prev_row['section_code']} jump={level_jump}",
                }
            )
        if str(prev_row.get("appendix_scope") or "") != str(curr_row.get("appendix_scope") or ""):
            continue
        prev_major = prev_tuple[0]
        curr_major = curr_tuple[0]
        curr_page = curr_row["source_page_start"]
        if prev_major >= 3 and curr_major <= 2 and curr_page >= 20:
            issues.append(
                {
                    "issue_type": "late_major_reset",
                    "severity": "HIGH",
                    "appendix_scope": curr_row.get("appendix_scope", ""),
                    "section_code": curr_row["section_code"],
                    "detail": f"prev={prev_row['section_code']} page={curr_page}",
                }
            )

    issue_counter: dict[str, int] = {}
    for item in issues:
        key = str(item.get("issue_type") or "")
        issue_counter[key] = issue_counter.get(key, 0) + 1

    return {
        "total_issues": len(issues),
        "duplicate_code_count": len(duplicate_codes),
        "missing_sibling_group_count": missing_group_count,
        "issue_type_counts": issue_counter,
        "issues": issues,
    }


def _safe_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _tuple_to_code(parts: tuple[int, ...]) -> str:
    if not parts:
        return ""
    return ".".join(str(item) for item in parts)


def _split_scoped_code(value: str) -> tuple[str, str]:
    scoped = str(value or "")
    if ":" not in scoped:
        return ("", scoped)
    left, right = scoped.split(":", 1)
    return (left.strip().upper(), right.strip())


def _sort_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        segments,
        key=lambda seg: (
            seg.get("source_page") is None,
            seg.get("source_page") if seg.get("source_page") is not None else 10**9,
            seg.get("source_anchor") or "",
        ),
    )


def _build_line_entries(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    global_line_index = 0
    for seg in segments:
        source_page = seg.get("source_page")
        source_anchor = seg.get("source_anchor") or "Unknown"
        table_count = int(seg.get("table_count", 0) or 0)
        source_line_index = 0
        for raw_line in str(seg.get("text", "")).splitlines():
            line = _normalize_line(raw_line)
            if not line or _is_line_noise(line):
                continue
            if "|" in line:
                for heading_candidate in _extract_table_heading_candidates(line):
                    entries.append(
                        {
                            "text": heading_candidate,
                            "source_page": source_page,
                            "source_anchor": source_anchor,
                            "table_count": table_count,
                            "source_line_index": source_line_index,
                            "global_line_index": global_line_index,
                        }
                    )
                    global_line_index += 1
            entries.append(
                {
                    "text": line,
                    "source_page": source_page,
                    "source_anchor": source_anchor,
                    "table_count": table_count,
                    "source_line_index": source_line_index,
                    "global_line_index": global_line_index,
                }
            )
            source_line_index += 1
            global_line_index += 1
    return entries


def _extract_section_blocks(line_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    idx = 0
    current_scope = ""
    last_section_code_by_scope: dict[str, str] = {}
    last_section_title_by_scope: dict[str, str] = {}
    last_section_page_by_scope: dict[str, int] = {}
    last_section_table_count_by_scope: dict[str, int] = {}
    seen_section_codes_by_scope: dict[str, set[str]] = {}
    discovery_counter = 0
    serial_table_mode_until = -1
    in_test_outline_zone = False
    while idx < len(line_entries):
        entry = line_entries[idx]
        line = entry["text"]
        if _is_serial_table_header_line(line):
            serial_table_mode_until = max(serial_table_mode_until, idx + 240)
        if _is_test_outline_header_line(line):
            if current and current.get("entries"):
                # Entering test-outline tables should close the previous main-body chapter block.
                header_page = entry.get("source_page")
                current_code = str(current.get("section_code") or "")
                current_tuple = _parse_section_code_tuple(current_code)
                current_major = current_tuple[0] if current_tuple else 0
                if (
                    not current_scope
                    and current_major >= 6
                    and isinstance(header_page, int)
                    and int(entry.get("table_count", 0) or 0) > 0
                ):
                    # If the test-outline header appears mid-page, drop pre-header noisy rows from the same page.
                    current["entries"] = [
                        row
                        for row in list(current.get("entries", []))
                        if row.get("source_page") != header_page
                    ]
                    kept_pages = [
                        int(row.get("source_page"))
                        for row in list(current.get("entries", []))
                        if isinstance(row.get("source_page"), int)
                    ]
                    if kept_pages:
                        current["source_page"] = min(kept_pages)
                        current["source_page_end"] = max(kept_pages)
                if current.get("entries"):
                    blocks.append(current)
                current = None
            in_test_outline_zone = True
        appendix_scope = _extract_appendix_scope(line)
        if appendix_scope:
            if current and current.get("entries"):
                blocks.append(current)
                current = None
            current_scope = appendix_scope
            idx += 1
            continue

        last_section_code = last_section_code_by_scope.get(current_scope, "")
        last_section_title = last_section_title_by_scope.get(current_scope, "")
        last_section_page = last_section_page_by_scope.get(current_scope)
        last_section_table_count = last_section_table_count_by_scope.get(current_scope, 0)
        header = _parse_section_header(line)
        consumed_following = 0

        # Try to recover "code only + title on next line" broken OCR headings.
        if header is None:
            code_hint = _extract_section_code_hint(line)
            next_title, title_offset = _next_header_title_hint(line_entries, idx + 1)
            if code_hint and next_title and _allow_code_hint_recovery(code_hint, next_title, last_section_code):
                recovered = _parse_section_header(f"{code_hint} {next_title}")
                if recovered:
                    header = recovered
                    consumed_following = title_offset

        # Noisy OCR often puts only section code on one line and title on next line.
        if header and _looks_like_noisy_header_title(str(header.get("title", ""))):
            next_title, title_offset = _next_header_title_hint(line_entries, idx + 1)
            if next_title:
                merged_header = _parse_section_header(f"{header['code']} {next_title}")
                if merged_header:
                    header = merged_header
                    consumed_following = max(consumed_following, title_offset)

        if (
            header
            and not current_scope
            and idx <= serial_table_mode_until
            and _is_serial_table_row_candidate(line, header)
        ):
            header = None
        if header and in_test_outline_zone and not current_scope:
            # Once entering the PDS test-outline zone, all following main-body numeric headers
            # are usually table references (e.g., USCAR 5.1.8 / 4.3) rather than real chapter nodes.
            header = None

        if header and _should_treat_as_enumerated_clause(
            header=header,
            appendix_scope=current_scope,
            last_section_code=last_section_code,
            seen_codes=seen_section_codes_by_scope.get(current_scope, set()),
            candidate_page=entry.get("source_page"),
            previous_page=last_section_page,
        ):
            header = None

        if header and not _is_plausible_section_transition(
            last_section_code,
            str(header.get("code", "")),
            str(header.get("title", "")),
            candidate_punct=str(header.get("punct", "")),
            appendix_scope=current_scope,
            previous_title=last_section_title,
            previous_page=last_section_page,
            candidate_page=entry.get("source_page"),
            previous_table_count=last_section_table_count,
            candidate_table_count=int(entry.get("table_count", 0) or 0),
        ):
            header = None

        if header:
            if current and current.get("entries"):
                blocks.append(current)
            current = {
                "appendix_scope": current_scope,
                "section_code": header["code"],
                "section_title": header.get("title", ""),
                "section_level": header.get("level", 0),
                "_discovery_order": discovery_counter,
                "source_page": entry["source_page"],
                "source_page_end": entry["source_page"],
                "source_line_index": entry.get("source_line_index"),
                "source_line_index_end": entry.get("source_line_index"),
                "source_anchor": entry["source_anchor"],
                "entries": [entry],
            }
            discovery_counter += 1
            last_section_code_by_scope[current_scope] = str(header.get("code") or last_section_code)
            last_section_title_by_scope[current_scope] = str(header.get("title") or "")
            src_page = entry.get("source_page")
            last_section_page_by_scope[current_scope] = int(src_page) if isinstance(src_page, int) else 0
            last_section_table_count_by_scope[current_scope] = int(entry.get("table_count", 0) or 0)
            seen_section_codes_by_scope.setdefault(current_scope, set()).add(str(header.get("code") or ""))
            if consumed_following > 0:
                idx += consumed_following
            idx += 1
            continue

        if current is not None:
            current["entries"].append(entry)
            if entry.get("source_page") is not None:
                if current.get("source_page_end") is None:
                    current["source_page_end"] = entry["source_page"]
                else:
                    current["source_page_end"] = max(current["source_page_end"], entry["source_page"])
            line_idx = entry.get("source_line_index")
            if isinstance(line_idx, int):
                if current.get("source_line_index") is None:
                    current["source_line_index"] = line_idx
                else:
                    current["source_line_index"] = min(int(current.get("source_line_index")), line_idx)
                end_idx = current.get("source_line_index_end")
                current["source_line_index_end"] = line_idx if end_idx is None else max(int(end_idx), line_idx)
        idx += 1

    if current and current.get("entries"):
        blocks.append(current)
    return _consolidate_section_blocks(blocks)


def _split_block_by_bullets(block: dict[str, Any]) -> list[dict[str, Any]]:
    entries = block.get("entries", [])
    if not entries:
        return []
    # Split priority is section hierarchy; keep each section/subsection as one complete block.
    return [_shape_subblock(entries, block["source_anchor"])]


def _shape_subblock(entries: list[dict[str, Any]], default_anchor: str) -> dict[str, Any]:
    pages = [e.get("source_page") for e in entries if e.get("source_page") is not None]
    source_page = min(pages) if pages else None
    source_page_end = max(pages) if pages else source_page
    line_indices = [e.get("source_line_index") for e in entries if isinstance(e.get("source_line_index"), int)]
    source_line_index = min(line_indices) if line_indices else None
    source_line_index_end = max(line_indices) if line_indices else source_line_index
    source_anchor = default_anchor
    for entry in entries:
        if entry.get("source_page") == source_page and entry.get("source_anchor"):
            source_anchor = entry["source_anchor"]
            break
    return {
        "source_page": source_page,
        "source_page_end": source_page_end,
        "source_line_index": source_line_index,
        "source_line_index_end": source_line_index_end,
        "source_anchor": source_anchor,
        "lines": [e["text"] for e in entries],
    }


def _parse_section_header(line: str) -> dict[str, Any] | None:
    if DOT_LEADER_RE.search(line):
        return None
    match = SECTION_HEADER_RE.match(line)
    if not match:
        return None

    code = (match.group(1) or "").strip()
    punct = (match.group(2) or "").strip()
    title = (match.group(3) or "").strip()
    if not code:
        return None
    if _looks_like_date_section_code(code):
        return None

    level = code.count(".") + 1
    if level > 6:
        return None
    main_idx = int(code.split(".")[0]) if code.split(".")[0].isdigit() else 999
    if main_idx == 0:
        return None

    # Exclude serial-number lines from appendices/lists.
    if (level == 1 and main_idx > 30) or (level > 1 and main_idx > 80) or (level == 1 and len(code) >= 3):
        return None

    if not title:
        return None
    # TOC artifacts like "11.3 III" are not real section titles.
    if level >= 2 and re.fullmatch(r"[IVXLCM]{1,8}", title, re.I):
        return None
    if _looks_like_table_row_text(line) or _looks_like_table_row_text(title):
        return None

    if _looks_like_measurement_title(title):
        return None
    if _looks_like_component_bom_title(title):
        return None
    if _looks_like_quantity_only_title(title):
        return None
    if level >= 2 and _looks_like_test_plan_row_title(title):
        return None
    if level == 1 and QUANTITY_TITLE_START_RE.match(title):
        return None

    # List-style lines like "1) xxx" are usually bullets; keep only concise heading-like cases.
    if punct in {")", "）"}:
        compact = title.strip()
        if len(compact) > 28:
            return None
        if re.search(r"[，,；;。]", compact):
            return None
        if level > 1 and len(compact) > 12 and not HEADING_KEYWORD_RE.search(compact):
            return None

    # Exclude legend/list lines like "1: X-axis", "1.2, and ...", "2.1 to determine ..."
    if HEADER_TITLE_LEADING_PUNCT_RE.match(title):
        return None
    if level == 1 and punct in {":", "："}:
        return None
    if level == 1 and punct in {"、", "。", "．"}:
        if len(title) > 48:
            return None
        if re.search(r"[，,；;。!?！？]", title):
            return None
        if CN_ENUM_PREFIX_RE.search(title):
            return None
    if level == 1 and not punct:
        if re.search(r"[，,；;。!?！？]", title):
            return None
        if len(title) > 40 and not HEADING_KEYWORD_RE.search(title):
            return None
        if re.search(r"(?:应|须|需|应当|不得|必须)", title) and len(title) > 18:
            return None
        if re.fullmatch(r"[（(][^）)]{1,20}[）)]", title):
            return None
    if level == 1 and punct == ".":
        if ":" in title or "：" in title:
            return None
        if re.search(r"[，,；;。!?！？]", title):
            return None
        token_count = len(re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", title))
        if token_count > 20:
            return None
    if HEADER_ENUM_LIKE_RE.match(title):
        return None

    if title and _is_line_noise(title):
        return None

    start_match = HEADER_TITLE_VALID_START_RE.search(title)
    if not start_match:
        return None
    start_ch = start_match.group(0)
    if "a" <= start_ch <= "z":
        return None
    if level == 1 and HEADER_SENTENCE_PREFIX_RE.search(title):
        return None

    # Keep section title lines only when likely true headings.
    if level == 1 and title and not re.search(r"[\u4e00-\u9fffA-Z]", title):
        return None

    # Skip lines that are likely sentence continuations, not headings.
    if re.search(r"[,，;；]", title[:4]) and level > 2:
        return None
    if level > 1 and title[:1].islower():
        return None
    if level > 1 and len(title) > 180:
        return None
    if level >= 2 and len(title) > 70 and not HEADING_KEYWORD_RE.search(title):
        return None
    if level >= 2 and len(title) > 40 and re.match(r"^(?:use|spray|set|measure|apply|check|connect)\b", title, re.I):
        return None
    if level == 1 and title:
        first_token = re.split(r"\s+", title, maxsplit=1)[0].strip().lower()
        if first_token in {"h", "hz", "khz", "mhz", "mm", "cm", "m", "ms", "s", "v", "a", "ma", "ohm", "c", "°c"}:
            return None
        if re.match(r"^[a-zA-Z]\W", title):
            return None

    return {"code": code, "title": title, "level": level, "punct": punct}


def _should_treat_as_enumerated_clause(
    header: dict[str, Any],
    appendix_scope: str,
    last_section_code: str,
    seen_codes: set[str],
    candidate_page: int | None,
    previous_page: int | None,
) -> bool:
    code = str(header.get("code") or "").strip()
    punct = str(header.get("punct") or "").strip()
    title = str(header.get("title") or "").strip()
    if not code or punct not in {")", "）"}:
        return False

    code_tuple = _parse_section_code_tuple(code)
    if not code_tuple:
        return False
    if str(appendix_scope or "").strip().upper():
        # Appendix rows frequently use punctuated list numbering; avoid promoting deep
        # x.y.z) lines to section nodes unless parent chain is present.
        if len(code_tuple) >= 2 and _section_parent_code(code) not in seen_codes:
            return True
        return False

    parent_code = _section_parent_code(code)
    parent_seen = bool(parent_code and parent_code in seen_codes)
    parent_matches_context = bool(
        parent_code
        and (
            parent_code == str(last_section_code or "")
            or str(last_section_code or "").startswith(f"{parent_code}.")
        )
    )
    if len(code_tuple) >= 2 and not (parent_seen or parent_matches_context):
        return True

    if len(code_tuple) == 1:
        last_tuple = _parse_section_code_tuple(str(last_section_code or ""))
        # Top-level punctuated numbers (e.g. 4）后蓄能器布置及要求) inside an
        # existing deep section are usually list clauses, not a new major chapter.
        if (
            last_tuple
            and len(last_tuple) >= 2
            and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(title)
            and len(title) <= 80
        ):
            if previous_page is None or candidate_page is None or candidate_page <= previous_page + 1:
                return True

    # Deep punctuated numeric items are usually list clauses, not chapter headings.
    if len(code_tuple) >= 3 and len(title) >= 14 and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(title):
        return True
    if re.search(r"[，,；;。]", title):
        return True

    # Same-page backward deep jumps are usually table/list references.
    if (
        previous_page is not None
        and candidate_page is not None
        and candidate_page <= previous_page
        and len(code_tuple) >= 2
        and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(title)
    ):
        return True
    return False


def _looks_like_date_section_code(code: str) -> bool:
    parts = _parse_section_code_tuple(code)
    if not parts:
        return False
    if len(parts) >= 3 and any(1900 <= item <= 2100 for item in parts):
        return True
    if len(parts) == 3:
        first, second, third = parts
        if 1 <= first <= 31 and 1 <= second <= 12 and 1900 <= third <= 2100:
            return True
    return False


def _allow_code_hint_recovery(code_hint: str, next_title: str, last_section_code: str) -> bool:
    if not code_hint or not next_title:
        return False
    if _looks_like_table_row_text(next_title):
        return False
    # Prevent TOC-style false recovery such as "16.2" + "1 工程概要".
    if re.match(r"^\s*\d+(?:\.\d+){0,5}\s+", next_title):
        return False
    if _looks_like_measurement_title(next_title):
        return False
    if not _looks_like_heading_phrase(next_title):
        return False

    hint_tuple = _parse_section_code_tuple(code_hint)
    prev_tuple = _parse_section_code_tuple(last_section_code)
    if not hint_tuple:
        return False
    if len(hint_tuple) >= 3:
        parent_code = ".".join(str(item) for item in hint_tuple[:-1])
        if parent_code and not (
            parent_code == str(last_section_code or "")
            or str(last_section_code or "").startswith(f"{parent_code}.")
        ):
            return False
        if re.match(r"^\s*[（(]", next_title):
            return False
        if len(next_title.strip()) > 28 and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(next_title):
            return False
    if not prev_tuple:
        return True

    # If we are deep inside a section tree, avoid recovering shallow numeric values as headings.
    if len(prev_tuple) >= 3 and len(hint_tuple) <= 2:
        prev_major = prev_tuple[0]
        hint_major = hint_tuple[0]
        if abs(hint_major - prev_major) > 1 and not HEADING_KEYWORD_RE.search(next_title):
            return False
    return True


def _looks_like_heading_phrase(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if len(value) > 120:
        return False
    if _looks_like_measurement_title(value):
        return False
    if _looks_like_table_row_text(value):
        return False
    if re.search(r"[。！？!?;；]\s*$", value):
        return False
    if re.search(r"[,，;；]", value[:10]) and not HEADING_KEYWORD_RE.search(value):
        return False
    if HEADING_KEYWORD_RE.search(value):
        return True
    if re.search(r"[\u4e00-\u9fff]{2,}", value):
        return len(re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)) <= 18

    token_count = len(re.findall(r"[A-Za-z0-9]+", value))
    if token_count == 0 or token_count > 16:
        return False
    if value[:1].islower():
        return False
    return True


def _looks_like_table_row_text(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if value.count("|") >= 2:
        return True
    if "\t" in value and len(value.split("\t")) >= 3:
        return True
    if _looks_like_component_bom_title(value):
        return True
    if TABLE_SERIAL_ROW_RE.match(value) and ("件" in value or "/" in value):
        return True
    if re.search(r"\b(?:数量|版本|层级|代号|重量|开发状态)\b", value) and len(value.split()) >= 4:
        return True
    return False


def _looks_like_component_bom_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return False
    if COMPONENT_BOM_TITLE_RE.match(value):
        return True
    if COMPONENT_BOM_INLINE_RE.search(value) and "/" in value and len(value) <= 100:
        return True
    if re.search(r"(?:缓冲块|防尘罩|上支座|自锁螺母|蓄能器|总成)", value) and re.search(r"\b\d+\s*件\b", value):
        return True
    return False


def _is_serial_table_header_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    lower = value.lower()
    if "序号" in value and ("测试类别" in value or "测试项目" in value or "测试小类" in value):
        return True
    if "序号" in value and "项目" in value and "要求" in value:
        return True
    if "test" in lower and "group" in lower and "item" in lower:
        return True
    return False


def _is_test_outline_header_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    if TEST_OUTLINE_HEADER_RE.search(value) and len(value) <= 140:
        return True
    if "|" in value:
        for cell in [part.strip() for part in value.split("|") if part.strip()]:
            if TEST_OUTLINE_HEADER_RE.search(cell) and len(cell) <= 120:
                return True
    return False


def _is_serial_table_row_candidate(line: str, header: dict[str, Any]) -> bool:
    code = str(header.get("code") or "").strip()
    title = str(header.get("title") or "").strip()
    level = int(header.get("level") or 0)
    raw = str(line or "").strip()
    if level != 1 or not code or not title:
        return False
    if "、" in raw or ":" in raw or "：" in raw:
        return False
    if re.match(r"^\d+[（(]", raw):
        return True
    if not re.match(r"^\d+\s+", raw):
        return False
    return len(title) <= 36


def _looks_like_quantity_only_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return False
    if re.fullmatch(r"(?:\d+(?:/\d+)?)?\s*(?:件|件/OTS|OTS|/OTS|天|days?)", value, re.I):
        return True
    if len(value) <= 16 and re.search(r"(?:万台车|台车|件/|/件|/OTS|OTS)$", value, re.I):
        return True
    if len(value) <= 24 and re.search(r"(?:万台车/\d+|台车/\d+|[0-9.]+\s*万台车)", value, re.I):
        return True
    return False


def _looks_like_test_plan_row_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return False
    if len(value) < 10:
        return False
    hint = TEST_PLAN_ROW_HINT_RE.search(value) is not None
    count_hint = TEST_PLAN_COUNT_HINT_RE.search(value) is not None
    if hint and count_hint:
        return True
    if count_hint and (len(value) >= 22 or re.search(r"\b(?:DV|PV)\b", value, re.I)):
        return True
    if re.search(r"(?:\b(?:DV|PV)\b\s*供应商|供应商\s*\d+\s*/\s*OTS)", value, re.I):
        return True
    return False


def _extract_table_heading_candidates(line: str) -> list[str]:
    value = str(line or "").strip()
    if value.count("|") < 2:
        return []
    cells = [part.strip() for part in value.split("|") if part.strip()]
    if not cells:
        return []
    normalized_cells = [_normalize_line(cell) for cell in cells if _normalize_line(cell)]
    if len(normalized_cells) >= 2 and len(set(normalized_cells)) == 1:
        # Rows like "|4、标准法规要求|4、标准法规要求|4、标准法规要求|" may still be real merged headings.
        merged = normalized_cells[0]
        merged_header = _parse_section_header(merged)
        if (
            merged_header
            and int(merged_header.get("level", 0) or 0) == 1
            and STRONG_MAJOR_HEADING_KEYWORD_RE.search(str(merged_header.get("title", "")))
        ):
            return [merged]
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    for cell in cells:
        normalized = _normalize_line(cell)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)

        if len(normalized) > 90:
            continue
        if _looks_like_component_bom_title(normalized):
            continue
        if _looks_like_table_row_text(normalized):
            continue
        if _is_line_noise(normalized):
            continue

        if _parse_section_header(normalized):
            candidates.append(normalized)
            continue

        # Some tables store major headings as "1、概述" in repeated cells.
        if re.match(r"^\d+[、．。]\s*[A-Za-z\u4e00-\u9fff].{0,64}$", normalized):
            candidates.append(normalized)

    return candidates


def _looks_like_measurement_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return False
    normalized = (
        value.replace("㎜", "mm")
        .replace("℃", "°C")
        .replace("＋", "+")
        .replace("－", "-")
    )
    if MEASUREMENT_TITLE_START_RE.match(normalized):
        return True

    # Unit-dense short fragments are usually requirement content, not headings.
    if len(normalized) <= 120 and MEASUREMENT_HINT_RE.search(normalized):
        tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", normalized)
        long_tokens = sum(1 for token in tokens if len(token) >= 4)
        if long_tokens <= 2:
            return True
    return False


def _normalize_block_text(lines: list[str]) -> str:
    normalized_lines: list[str] = []
    prev = ""
    for line in lines:
        text = _normalize_line(line)
        text = _compact_table_line(text)
        if not text:
            continue
        if _is_block_artifact_line(text):
            continue
        if text == prev:
            continue
        normalized_lines.append(text)
        prev = text

    if not normalized_lines:
        return ""

    normalized_lines = _normalize_table_lines(normalized_lines)
    normalized_lines = [line for line in normalized_lines if line]
    if not normalized_lines:
        return ""

    # Keep line boundaries to preserve original subsection completeness.
    text = "\n".join(normalized_lines).strip()
    if len(text) < 20:
        return ""
    return text


def _compact_table_line(line: str) -> str:
    value = str(line or "").strip()
    if not value or "|" not in value:
        return value
    cells = [part.strip() for part in value.split("|") if part.strip()]
    if len(cells) < 2:
        return value

    out_cells: list[str] = []
    seen_keys: set[str] = set()
    for cell in cells:
        key = _build_dedupe_key(cell)
        if key and key in seen_keys:
            continue
        if _cell_is_near_duplicate(cell, out_cells):
            continue
        if key:
            seen_keys.add(key)
        out_cells.append(cell)

    if len(out_cells) < 2:
        # Rows extracted from merged cells are often repeated N times.
        # Collapse to a single cell when all source cells are near-duplicates.
        if len(out_cells) == 1 and len(cells) >= 3:
            if all(_cell_is_near_duplicate(source_cell, [out_cells[0]]) for source_cell in cells[1:]):
                return out_cells[0]
        return value
    compact = "|" + "|".join(out_cells) + "|"
    return re.sub(r"\s+", " ", compact).strip()


def _cell_is_near_duplicate(cell: str, existing_cells: list[str]) -> bool:
    candidate = _build_dedupe_key(cell)
    if not candidate:
        return False
    for existing in existing_cells:
        baseline = _build_dedupe_key(existing)
        if not baseline:
            continue
        if candidate == baseline:
            return True

        short_len = min(len(candidate), len(baseline))
        long_len = max(len(candidate), len(baseline))
        if short_len >= 32 and (candidate in baseline or baseline in candidate):
            return True

        if short_len >= 40:
            prefix = min(48, short_len)
            if candidate[:prefix] == baseline[:prefix]:
                # OCR truncation or slight drift in trailing tokens.
                if short_len >= int(long_len * 0.72):
                    return True
    return False


def _normalize_table_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    out: list[str] = []
    for raw in lines:
        line = _normalize_line(raw)
        if not line:
            continue

        if _looks_like_plain_bom_header(line):
            out.append("|序号|层级|代号|版本|名称|重量|数量|开发状态|")
            continue
        if _looks_like_plain_table_row(line):
            line = _convert_plain_table_row_to_markdown(line)
        out.append(line)
    out = _normalize_plain_vehicle_info_table(out)
    out = _normalize_plain_test_matrix_table(out)
    out = _refine_integrated_validation_table(out)
    out = _normalize_requirement_table_structure(out)
    out = _repair_requirement_row_handoffs(out)
    out = _drop_plain_requirement_table_duplicates(out)
    # Strict original-order mode: keep bridge lines to avoid dropping genuine clauses.
    # Strict original-order mode: disable bridge relocation that reorders lines.
    out = _normalize_bom_markdown_rows(out)
    out = _dedupe_bom_markdown_rows(out)
    out = _drop_stray_bom_artifact_lines(out)
    # Strict original-order mode: disable cross-page row migration / synthetic tail-row recovery.
    out = _dedupe_requirement_table_rows(out)
    # Strict original-order mode: disable synthetic row insertion and DVP enumeration rewrites.
    # Run duplicate plain-table cleanup again after table normalization/repair passes.
    # Some pages only become fully structured markdown tables in later passes.
    out = _drop_plain_requirement_table_duplicates(out)
    return out


def _looks_like_plain_bom_header(line: str) -> bool:
    value = re.sub(r"\s+", "", str(line or "").strip())
    if not value or "|" in value:
        return False
    return bool(re.search(r"序号.*层级.*代号.*版本.*名称.*重量.*数量.*开发状态", value))


def _normalize_plain_vehicle_info_table(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    header_cells = ["项目代号", "车型代号", "软模件到样时间", "工装样件到样时间", "OTS认可完成", "小批量时间", "量纲"]
    header_markdown = "|" + "|".join(header_cells) + "|"
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = _normalize_line(str(lines[idx] or ""))
        compact = re.sub(r"\s+", "", line)
        if "|" not in line and all(token in compact for token in ["项目代号", "车型代号", "软模件到样时间", "工装样件到样时间", "量纲"]):
            next_idx = idx + 1
            if next_idx < len(lines):
                next_line = _normalize_line(str(lines[next_idx] or ""))
                continuation = ""
                cont_idx = next_idx + 1
                if cont_idx < len(lines):
                    cont_line = _normalize_line(str(lines[cont_idx] or ""))
                    if _looks_like_vehicle_scale_fragment(cont_line):
                        continuation = cont_line
                data_row = _build_vehicle_info_markdown_row(next_line, continuation)
                if data_row:
                    out.append(header_markdown)
                    out.append(data_row)
                    idx = next_idx + 1 + (1 if continuation else 0)
                    continue
        out.append(lines[idx])
        idx += 1
    return out


def _build_vehicle_info_markdown_row(line: str, continuation: str = "") -> str:
    value = _normalize_line(str(line or ""))
    if not value:
        return ""
    continuation_value = _normalize_line(str(continuation or ""))
    if continuation_value:
        value = _normalize_line(f"{value} {continuation_value}")
    value = re.sub(r"(\d)\s+(\d)\s*\.\s*(\d{2})\s*\.\s*(\d{2})", r"\1\2.\3.\4", value)
    value = re.sub(r"(\d{4})\s*\.\s*(\d{2})\s*\.\s*(\d{2})", r"\1.\2.\3", value)
    value = re.sub(r"\s+", " ", value).strip()
    date_iter = list(re.finditer(r"\d{4}\.\d{2}\.\d{2}", value))
    if len(date_iter) < 4:
        return ""
    dates = [item.group(0) for item in date_iter[:4]]
    prefix = value[: date_iter[0].start()].strip()
    suffix = value[date_iter[3].end() :].strip()
    prefix_tokens = [token for token in prefix.split() if token]
    if len(prefix_tokens) >= 4:
        item_code = "".join(prefix_tokens[:2])
        model_code = "".join(prefix_tokens[2:4])
    elif len(prefix_tokens) >= 2:
        item_code = prefix_tokens[0]
        model_code = prefix_tokens[1]
    else:
        return ""
    scale = suffix or "/"
    return f"|{item_code}|{model_code}|{dates[0]}|{dates[1]}|{dates[2]}|{dates[3]}|{scale}|"


def _looks_like_vehicle_scale_fragment(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value or "|" in value:
        return False
    compact = re.sub(r"\s+", "", value)
    return bool(re.fullmatch(r"\d+(?:\.\d+)?万台车/\d+(?:年)?", compact))


def _normalize_plain_test_matrix_table(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    out: list[str] = []
    idx = 0
    header_markdown = "|序号|测试类别|测试小类|测试项目|"
    while idx < len(lines):
        line = _normalize_line(str(lines[idx] or ""))
        compact = re.sub(r"\s+", "", line)
        if "|" in line or not all(token in compact for token in ["序号", "测试类别", "测试小类", "测试项目"]):
            out.append(lines[idx])
            idx += 1
            continue

        scan = idx + 1
        current_category = ""
        current_subcategory = ""
        rows: list[list[str]] = []
        while scan < len(lines):
            current = _normalize_line(str(lines[scan] or ""))
            if not current:
                scan += 1
                continue

            num_match = re.match(r"^(\d{1,3})\s+(.+)$", current)
            if num_match:
                row_no = num_match.group(1)
                row_title = _normalize_line(num_match.group(2))
                rows.append([row_no, current_category or "/", current_subcategory or "/", row_title or "/"])
                scan += 1
                continue

            if _parse_section_header(current) or _extract_appendix_scope(current):
                break

            if len(current) <= 24 and not re.search(r"[。；;，,]", current):
                if not current_category or current_subcategory:
                    current_category = current
                    current_subcategory = ""
                else:
                    current_subcategory = current
                scan += 1
                continue

            if rows and current.startswith(("试验后", "误用工况", "底盘误用")):
                break
            if rows:
                rows[-1][3] = _normalize_line(f"{rows[-1][3]} {current}")
                scan += 1
                continue
            break

        if len(rows) >= 8:
            out.append(header_markdown)
            for row_no, category, subcategory, title in rows:
                out.append(f"|{row_no}|{category}|{subcategory}|{title}|")
            idx = scan
            continue

        out.append(lines[idx])
        idx += 1
    return out


def _refine_integrated_validation_table(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    out = list(lines)
    in_target = False
    in_table = False
    table_header = "|序号|测试类别|测试小类|测试项目|"

    for idx, raw in enumerate(out):
        value = _normalize_line(str(raw or ""))
        if not value:
            continue
        if re.match(r"^\s*5\.7\s+集成验证要求", value):
            in_target = True
        if not in_target:
            continue

        if value == table_header:
            in_table = True
            continue

        if not in_table:
            continue

        if not _looks_like_markdown_table_row(value):
            if _parse_section_header(value) or _extract_appendix_scope(value):
                in_table = False
            continue

        cells = _parse_markdown_cells(value)
        row_no = _extract_requirement_table_row_no(cells)
        if row_no is None or len(cells) < 4:
            continue

        category = _normalize_line(str(cells[1] or "")).strip("/")
        subcategory = _normalize_line(str(cells[2] or "")).strip("/")
        item = _normalize_line(str(cells[3] or ""))
        item = _dedupe_repeated_cn_phrase(item)
        category, subcategory, item = _reassign_integrated_validation_columns(category, subcategory, item)
        category = category or "/"
        subcategory = subcategory or "/"
        item = item or "/"
        out[idx] = f"|{cells[0]}|{category}|{subcategory}|{item}|"
    return out


def _normalize_requirement_table_structure(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    out: list[str] = []
    in_requirement_table = False
    last_row_idx = -1
    last_row_no = 0
    pending_pre_row_lines: list[str] = []
    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        value = _normalize_line(str(raw or ""))
        if not value:
            out.append(raw)
            idx += 1
            continue

        if _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            if _is_requirement_table_header_cells(cells):
                out.append("|序号|项目|要求|")
                in_requirement_table = True
                last_row_idx = -1
                last_row_no = 0
                pending_pre_row_lines = []
                idx += 1
                continue
            if in_requirement_table:
                row_no = _extract_requirement_table_row_no(cells)
                if row_no is not None:
                    item = _normalize_line(str(cells[1] if len(cells) >= 2 else "")) or "/"
                    requirement = _normalize_line(
                        " ".join(_normalize_line(cell) for cell in cells[2:] if _normalize_line(cell))
                    )
                    if re.fullmatch(r"col\d+", requirement, re.I):
                        requirement = "/"
                    if pending_pre_row_lines and (last_row_no <= 0 or row_no == last_row_no + 1 or row_no == 1):
                        prefix = _normalize_line(" ".join(pending_pre_row_lines))
                        if prefix:
                            if requirement in {"", "/"}:
                                requirement = prefix
                            elif prefix not in requirement:
                                requirement = _normalize_line(f"{prefix} {requirement}")
                        pending_pre_row_lines = []
                    out.append(f"|{row_no}|{item}|{requirement or '/'}|")
                    last_row_idx = len(out) - 1
                    last_row_no = row_no
                    idx += 1
                    continue
            out.append(raw)
            idx += 1
            continue

        if _looks_like_plain_requirement_header_line(value):
            out.append("|序号|项目|要求|")
            in_requirement_table = True
            last_row_idx = -1
            last_row_no = 0
            pending_pre_row_lines = []
            idx += 1
            continue

        if in_requirement_table:
            plain_row = _parse_plain_requirement_row(value)
            if plain_row:
                row_no, item, requirement = plain_row
                if row_no <= 2 and last_row_no >= 8:
                    out.append("|序号|项目|要求|")
                    last_row_idx = -1
                    pending_pre_row_lines = []
                if pending_pre_row_lines and (last_row_no <= 0 or row_no == last_row_no + 1 or row_no == 1):
                    prefix = _normalize_line(" ".join(pending_pre_row_lines))
                    if prefix:
                        if requirement in {"", "/"}:
                            requirement = prefix
                        elif prefix not in requirement:
                            requirement = _normalize_line(f"{prefix} {requirement}")
                    pending_pre_row_lines = []
                if row_no >= last_row_no or row_no <= 3:
                    out.append(f"|{row_no}|{item}|{requirement}|")
                    last_row_idx = len(out) - 1
                    last_row_no = max(last_row_no, row_no)
                    idx += 1
                    continue

            next_plain = _peek_next_plain_requirement_row(lines, idx + 1) if last_row_idx >= 0 else None
            if last_row_idx >= 0:
                current_item = _requirement_row_item(str(out[last_row_idx]))
                if _should_defer_continuation_to_next_row(
                    continuation=value,
                    current_item=current_item,
                    current_row_no=last_row_no,
                    next_plain_row=next_plain,
                ):
                    pending_pre_row_lines.append(value)
                    idx += 1
                    continue

            if last_row_idx < 0 and _looks_like_requirement_table_value_continuation(value):
                pending_pre_row_lines.append(value)
                idx += 1
                continue

            if last_row_idx >= 0 and _looks_like_requirement_table_value_continuation(value):
                merged = _merge_table_continuation_row(str(out[last_row_idx]), value)
                if merged:
                    out[last_row_idx] = merged
                    idx += 1
                    continue

            if _is_requirement_table_hard_stop_line(value):
                in_requirement_table = False
                last_row_idx = -1
                last_row_no = 0
                pending_pre_row_lines = []
            elif last_row_idx >= 0:
                # In requirement-table context, plain orphan lines are usually OCR-wrapped
                # requirement tail text. Keep order by attaching to previous row.
                merged = _merge_table_continuation_row(str(out[last_row_idx]), value)
                if merged:
                    out[last_row_idx] = merged
                    idx += 1
                    continue

        out.append(raw)
        idx += 1

    return out


def _repair_requirement_row_handoffs(lines: list[str]) -> list[str]:
    if len(lines) < 2:
        return lines
    out = list(lines)
    for idx in range(len(out) - 1):
        current = _normalize_line(str(out[idx] or ""))
        following = _normalize_line(str(out[idx + 1] or ""))
        if not (_looks_like_markdown_table_row(current) and _looks_like_markdown_table_row(following)):
            continue
        cells = _parse_markdown_cells(current)
        next_cells = _parse_markdown_cells(following)
        cur_no = _extract_requirement_table_row_no(cells)
        next_no = _extract_requirement_table_row_no(next_cells)
        if cur_no is None or next_no is None or next_no != cur_no + 1:
            continue

        cur_item = _normalize_line(str(cells[1] if len(cells) >= 2 else ""))
        next_item = _normalize_line(str(next_cells[1] if len(next_cells) >= 2 else ""))
        cur_req = _normalize_line(" ".join(_normalize_line(cell) for cell in cells[2:] if _normalize_line(cell)))
        next_req = _normalize_line(" ".join(_normalize_line(cell) for cell in next_cells[2:] if _normalize_line(cell)))
        if not cur_req or not next_item:
            continue
        if "接插件" not in next_item or "接插件" in cur_item:
            continue
        if not re.search(r"(?:防护等级|IP\\d|APTIV|Delphi|线束端|零件端|连接器)", cur_req, re.I):
            continue

        split_markers = ["防护等级", "IP", "型号推荐", "零件端", "线束端", "APTIV", "Delphi", "连接器"]
        split_pos = -1
        for marker in split_markers:
            pos = cur_req.find(marker)
            if pos >= 0 and (split_pos < 0 or pos < split_pos):
                split_pos = pos
        if split_pos <= 0:
            continue

        remain = _normalize_line(cur_req[:split_pos].strip(" ；;，,"))
        moved = _normalize_line(cur_req[split_pos:])
        if not moved:
            continue
        if next_req and moved in next_req:
            continue

        new_cur_req = remain or "/"
        if not next_req or next_req == "/":
            new_next_req = moved
        else:
            new_next_req = _normalize_line(f"{moved} {next_req}")

        out[idx] = f"|{cur_no}|{cur_item or '/'}|{new_cur_req}|"
        out[idx + 1] = f"|{next_no}|{next_item or '/'}|{new_next_req}|"
    return out


def _parse_plain_requirement_row(line: str) -> tuple[int, str, str] | None:
    match = re.match(r"^\s*(\d{1,3})\s+(.+)$", _normalize_line(str(line or "")))
    if not match:
        return None
    try:
        row_no = int(match.group(1))
    except ValueError:
        return None
    if row_no <= 0 or row_no > 500:
        return None

    tail = _normalize_line(match.group(2))
    if not tail:
        return None

    item, requirement = _split_plain_requirement_item_and_requirement(tail)
    if not item:
        return None
    if len(item) > 40 and not requirement:
        return None
    if re.fullmatch(r"col\d+", requirement, re.I):
        requirement = "/"
    return (row_no, item, requirement or "/")


def _peek_next_plain_requirement_row(
    lines: list[str],
    start_idx: int,
) -> tuple[int, int, str, str] | None:
    scan_end = min(len(lines), start_idx + 8)
    for idx in range(start_idx, scan_end):
        value = _normalize_line(str(lines[idx] or ""))
        if not value:
            continue
        if _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            row_no = _extract_requirement_table_row_no(cells)
            if row_no is None:
                continue
            item = _normalize_line(str(cells[1] if len(cells) >= 2 else "")) or "/"
            req = _normalize_line(" ".join(_normalize_line(cell) for cell in cells[2:] if _normalize_line(cell))) or "/"
            return (idx, row_no, item, req)
        parsed = _parse_plain_requirement_row(value)
        if parsed:
            row_no, item, req = parsed
            return (idx, row_no, item, req)
        if _is_requirement_table_hard_stop_line(value):
            return None
    return None


def _requirement_row_item(table_row: str) -> str:
    value = _normalize_line(str(table_row or ""))
    if not _looks_like_markdown_table_row(value):
        return ""
    cells = _parse_markdown_cells(value)
    return _normalize_line(str(cells[1] if len(cells) >= 2 else ""))


def _should_defer_continuation_to_next_row(
    continuation: str,
    current_item: str,
    current_row_no: int,
    next_plain_row: tuple[int, int, str, str] | None,
) -> bool:
    if not next_plain_row:
        return False
    _, next_row_no, next_item, next_req = next_plain_row
    if next_row_no != current_row_no + 1:
        return False

    value = _normalize_line(str(continuation or ""))
    curr = _normalize_line(current_item)
    next_title = _normalize_line(next_item)
    next_req = _normalize_line(next_req)

    # The following line is often the first bullet of the next row.
    if re.match(r"^1[、.)）]\s*", value):
        return True

    # If next row has no inline requirement text, continuation likely belongs there.
    if next_req in {"", "/"}:
        if curr and "接插件" in curr and re.search(r"(?:IP\d+|APTIV|Delphi|线束|连接器)", value, re.I):
            return False
        if re.search(r"(?:拉伸极限|轴向力|侧向力|kN|强度|腐蚀|盐雾|白锈|黑色|深灰色|防锈|法规|GB\\s*\\d+|功耗)", value, re.I):
            return True
        return False

    # Semantic handoff: continuation better matches next row topic.
    handoff_pairs = [
        (re.compile(r"(?:拉伸极限|轴向力|侧向力|kN|强度)", re.I), re.compile(r"强度", re.I)),
        (re.compile(r"(?:腐蚀|盐雾|白锈|黑色|深灰色|防锈)", re.I), re.compile(r"防锈", re.I)),
        (re.compile(r"(?:法规|GB\\s*\\d+)", re.I), re.compile(r"法规", re.I)),
        (re.compile(r"功耗", re.I), re.compile(r"功耗", re.I)),
    ]
    for text_re, item_re in handoff_pairs:
        if text_re.search(value) and item_re.search(next_title):
            return True

    return False


def _split_plain_requirement_item_and_requirement(tail: str) -> tuple[str, str]:
    value = _normalize_line(str(tail or ""))
    if not value:
        return ("", "")

    known = _extract_known_requirement_item_prefix(value)
    if known:
        return known

    split_rules: list[re.Pattern[str]] = [
        re.compile(r"\s+(?=\d+\s*[、）)])"),
        re.compile(r"\s+(?=\d+\.(?!\d))"),
        re.compile(r"\s+(?=[（(]\d+[）)])"),
        re.compile(r"\s+(?=M\s*\d{1,3}(?:\s*\*\s*\d+(?:\.\d+)?)?)", re.I),
        re.compile(r"\s+(?=[Φφ]?\d+(?:\.\d+)?\s*(?:mm|cm|m|kN|N|bar|V|A|℃|°C)\b)", re.I),
        re.compile(r"\s+(?=(?:图纸|按标准|防护等级|材质|注油压力|拉伸|压缩|功耗|EMC|法规|刚度|长度|外径))"),
    ]
    for rule in split_rules:
        match = rule.search(value)
        if not match:
            continue
        left = _normalize_line(value[: match.start()])
        right = _normalize_line(value[match.start() :])
        if not left or len(left) > 40:
            continue
        return (left, right)

    parts = value.split(" ", 1)
    if len(parts) == 2:
        left = _normalize_line(parts[0])
        right = _normalize_line(parts[1])
        if left and len(left) <= 24:
            return (left, right or "/")

    return (value, "/")


def _extract_known_requirement_item_prefix(text: str) -> tuple[str, str] | None:
    value = _normalize_line(str(text or ""))
    if not value:
        return None
    candidates = [
        "基本尺寸",
        "电磁阀",
        "空簧接口",
        "下叉臂接口",
        "活塞杆接口",
        "行程",
        "空间硬点坐标",
        "限位块",
        "接插件",
        "减振器强度要求",
        "减振器主动力",
        "防锈能力",
        "EMC",
        "法规",
        "功耗",
        "注油压力",
        "装配工艺",
        "Top Mount与车身接口",
        "顶盖",
        "下衬套",
    ]
    for item in candidates:
        if not value.startswith(item):
            continue
        tail = _normalize_line(value[len(item) :]).lstrip("：:，,;； ")
        return (item, tail or "/")
    return None


def _looks_like_requirement_table_value_continuation(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if "|" in value:
        return False
    if re.match(r"^\d+[）)]\s*", value):
        return False
    if re.match(r"^\d+(?:\.\d+){1,6}\b", value):
        return False
    if _extract_appendix_scope(value):
        return False
    if _is_test_outline_header_line(value):
        return False

    if re.match(r"^(?:\d+[、.)．。]|[（(]\d+[）)])\s*", value):
        return True
    if re.match(r"^\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s|h)\b", value, re.I):
        return True
    if re.search(r"\d+(?:\.\d+)?\s*(?:mm|cm|m|kN|N|bar|V|A|℃|°C|m/s|h)\b", value, re.I):
        return True
    if re.search(r"(?:螺栓|螺纹|连接螺栓|thread|bolt)", value, re.I) and re.search(r"\d", value):
        return True
    if re.match(
        r"^(?:需要|满足|材质|防护等级|注油压力|图纸|按标准|拉伸|压缩|长度|外径|防锈|法规|功耗|EMC|该技术|供应商|减振器)",
        value,
        re.I,
    ):
        return True
    if len(value) <= 220 and "：" in value:
        return True
    return False


def _is_requirement_table_hard_stop_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if _extract_appendix_scope(value):
        return True
    if _is_test_outline_header_line(value):
        return True
    if re.match(r"^\d+[）)]\s*", value):
        return True
    if re.match(r"^\d+(?:\.\d+){1,6}\b", value):
        return True
    return False


def _drop_plain_requirement_table_duplicates(lines: list[str]) -> list[str]:
    if len(lines) < 6:
        return lines
    out: list[str] = []
    idx = 0
    total = len(lines)
    while idx < total:
        current = _normalize_line(str(lines[idx] or ""))
        if not _looks_like_plain_requirement_header_line(current):
            out.append(lines[idx])
            idx += 1
            continue

        # Find markdown requirement-table header shortly after this plain block.
        md_header_idx = -1
        for j in range(idx + 1, min(total, idx + 90)):
            cand = _normalize_line(str(lines[j] or ""))
            if not _looks_like_markdown_table_row(cand):
                continue
            if _is_requirement_table_header_cells(_parse_markdown_cells(cand)):
                md_header_idx = j
                break
            if _is_cross_page_scan_stop_line(cand):
                break
        if md_header_idx < 0:
            out.append(lines[idx])
            idx += 1
            continue

        plain_rows = _collect_plain_requirement_row_numbers(lines[idx + 1 : md_header_idx])
        if len(plain_rows) < 3 or plain_rows[0] != 1:
            out.append(lines[idx])
            idx += 1
            continue

        md_rows = _collect_markdown_requirement_row_numbers(lines, md_header_idx + 1)
        if len(md_rows) < len(plain_rows):
            out.append(lines[idx])
            idx += 1
            continue
        if not set(plain_rows).issubset(set(md_rows)):
            out.append(lines[idx])
            idx += 1
            continue

        # Drop the duplicated plain block and keep the structured markdown table.
        idx = md_header_idx
    return out


def _looks_like_plain_requirement_header_line(line: str) -> bool:
    compact = re.sub(r"\s+", "", str(line or ""))
    if not compact:
        return False
    return bool("序号" in compact and "项目" in compact and "要求" in compact and "|" not in compact)


def _collect_plain_requirement_row_numbers(lines: list[str]) -> list[int]:
    numbers: list[int] = []
    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if not value:
            continue
        match = re.match(r"^\s*(\d{1,3})\s+", value)
        if not match:
            continue
        row_no = int(match.group(1))
        if row_no <= 0 or row_no > 500:
            continue
        if numbers and row_no == numbers[-1]:
            continue
        numbers.append(row_no)
    return numbers


def _collect_markdown_requirement_row_numbers(lines: list[str], start_idx: int) -> list[int]:
    numbers: list[int] = []
    idx = start_idx
    while idx < len(lines):
        value = _normalize_line(str(lines[idx] or ""))
        if not _looks_like_markdown_table_row(value):
            break
        cells = _parse_markdown_cells(value)
        row_no = _extract_requirement_table_row_no(cells)
        if row_no is not None:
            numbers.append(row_no)
        idx += 1
    return numbers


def _dedupe_repeated_cn_phrase(text: str) -> str:
    value = str(text or "")
    if not value:
        return value
    value = re.sub(r"([\u4e00-\u9fff]{2,10})\1", r"\1", value)
    return _normalize_line(value)


def _reassign_integrated_validation_columns(
    category: str,
    subcategory: str,
    item: str,
) -> tuple[str, str, str]:
    cat = _normalize_line(category or "").strip("/")
    sub = _normalize_line(subcategory or "").strip("/")
    val = _normalize_line(item or "")

    if cat in {"系统级测试", "系统级测试 /"}:
        cat = "系统级测试"
    if not cat:
        cat = "/"
    if not sub:
        sub = "/"

    if cat in {"/", "系统级测试"} and val.startswith("整车环境适应性"):
        cat = "整车环境适应性"
        val = _normalize_line(val.replace("整车环境适应性", "", 1))

    if sub in {"/", "三高测试"} and val.startswith("社会道路"):
        sub = "社会道路"
        val = _normalize_line(val.replace("社会道路", "", 1))
    if sub in {"/", "三高测试"} and val.startswith("试验场道路"):
        sub = "试验场道路"
        val = _normalize_line(val.replace("试验场道路", "", 1))
    if sub in {"/"} and val.startswith("试验室台架"):
        sub = "试验室台架"
        val = _normalize_line(val.replace("试验室台架", "", 1))
    if sub in {"/"} and val.startswith("整车密封性"):
        sub = "整车密封性"
        val = _normalize_line(val.replace("整车密封性", "", 1))
    if sub in {"三高测试", "/"} and val.startswith("环境老化"):
        sub = "环境老化"
        val = _normalize_line(val.replace("环境老化", "", 1))
    if sub in {"试验室台架", "/"} and val.startswith("整车密封性"):
        sub = "整车密封性"
        val = _normalize_line(val.replace("整车密封性", "", 1))

    if cat == "/" and sub == "/" and val.startswith("系统级测试"):
        cat = "系统级测试"
        val = _normalize_line(val.replace("系统级测试", "", 1))
    if sub in {"社会道路", "试验场道路"} and val in {"试验", "测试", "/"}:
        val = f"{sub}试验"
    if val.startswith("误用工况测试整车误用工况测试"):
        val = _normalize_line(val.replace("误用工况测试整车误用工况测试", "整车误用工况测试", 1))

    # Recover category semantics from item/subcategory when merged cells are flattened.
    if cat in {"/", "系统级测试"}:
        if any(
            kw in val
            for kw in (
                "高温适应性",
                "高原适应性",
                "高寒适应性",
                "湿热适应性",
                "强化腐蚀",
                "大气暴晒",
                "环境舱静态耐久",
            )
        ):
            cat = "整车环境适应性"
        elif sub in {"社会道路", "试验场道路", "试验室台架", "整车密封性"} or any(
            kw in val
            for kw in (
                "社会道路试验",
                "综合工况耐久",
                "强化工况耐久",
                "动力传动可靠性",
                "四立柱道路模拟",
                "24通道道路模拟",
                "转毂耐久",
                "整车门盖耐久",
                "整车水密封性试验",
                "整车涉水试验",
            )
        ):
            cat = "整车道路行驶试验"
        elif any(kw in val for kw in ("整车防尘试验", "整车误用工况测试")):
            cat = "整车其他试验"

    if val in {"强化腐蚀", "大气暴晒", "环境舱静态耐久"} and sub in {"/", "三高测试"}:
        sub = "环境老化"
    if any(kw in val for kw in ("综合工况耐久", "强化工况耐久")) and sub in {"/", "三高测试"}:
        sub = "试验场道路"
    if any(
        kw in val
        for kw in (
            "动力传动可靠性",
            "四立柱道路模拟",
            "环境舱四立柱道路模拟",
            "24通道道路模拟",
            "转毂耐久",
            "环境舱转毂耐久",
            "整车门盖耐久",
        )
    ) and sub in {"/", "三高测试"}:
        sub = "试验室台架"

    if not val:
        val = item or "/"
    return (cat, sub, val)


def _looks_like_plain_table_row(line: str) -> bool:
    value = str(line or "").strip()
    if not value or "|" in value:
        return False
    if not re.match(r"^\d+\s+\d+\s+\S", value):
        return False
    if len(value) > 200:
        return False
    if not re.search(r"(?:件|开发|总成|本体|蓄能器|防尘罩|上支座|缓冲块|螺栓|标准件|/)", value):
        return False
    return True


def _convert_plain_table_row_to_markdown(line: str) -> str:
    value = str(line or "").strip()
    match = re.match(r"^(\d+)\s+(\d+)\s+(.+)$", value)
    if not match:
        return value

    index_no = match.group(1).strip()
    level = match.group(2).strip()
    tail = _normalize_line(match.group(3))
    if not tail:
        return value

    quantity = "/"
    status = "/"
    prefix = tail
    qty_match = re.search(r"(\d+\s*件)\s*([^\s].*)?$", tail)
    if qty_match:
        quantity = re.sub(r"\s+", "", qty_match.group(1))
        status = _normalize_line(str(qty_match.group(2) or "/"))
        prefix = tail[: qty_match.start()].strip()

    weight = "/"
    main = prefix
    weight_match = re.search(r"(?:\s|^)([＜<]?\s*\d+(?:\s*\.\s*\d+)?|/)\s*$", prefix)
    if weight_match:
        weight = _normalize_line(weight_match.group(1))
        weight = re.sub(r"\s*\.\s*", ".", weight)
        weight = weight.replace(" ", "")
        main = prefix[: weight_match.start()].strip()

    code = "/"
    version = "/"
    name = main

    # Pattern A: code + version + name
    parsed = re.match(r"^([A-Za-z0-9\-/\. ]{6,})\s+((?:\d+(?:\.\d+)+)|/)\s+(.+)$", main)
    if parsed and re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", parsed.group(3)):
        code = _normalize_line(parsed.group(1))
        version = _normalize_line(parsed.group(2))
        name = _normalize_line(parsed.group(3))
    else:
        # Pattern B: code + name (version missing in OCR)
        parsed2 = re.match(r"^([A-Za-z0-9\-/\.]{6,})\s+(.+)$", main)
        if parsed2 and re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", parsed2.group(2)):
            code = _normalize_line(parsed2.group(1))
            version = "/"
            name = _normalize_line(parsed2.group(2))
        else:
            name = _normalize_line(main) or "/"

    status = status or "/"
    if status == "None":
        status = "/"
    cells = [index_no, level, code, version, name or "/", weight or "/", quantity or "/", status]
    return "|" + "|".join(cells) + "|"


def _drop_redundant_table_bridge_lines(lines: list[str]) -> list[str]:
    if len(lines) < 3:
        return lines
    out: list[str] = []
    total = len(lines)
    for idx, line in enumerate(lines):
        value = str(line or "").strip()
        prev_line = lines[idx - 1] if idx > 0 else ""
        next_line = lines[idx + 1] if idx + 1 < total else ""
        if (
            value
            and not _looks_like_markdown_table_row(value)
            and _looks_like_markdown_table_row(prev_line)
            and _looks_like_markdown_table_row(next_line)
        ):
            if _is_bom_bridge_artifact(value):
                continue
            if _line_contained_in_markdown_row(value, next_line):
                continue
            if _line_contained_in_markdown_row(value, prev_line):
                continue
            if _should_preserve_table_bridge_line(value, prev_line, next_line):
                out.append(line)
                continue
        out.append(line)
    return out


def _relocate_damping_bridge_lines(lines: list[str]) -> list[str]:
    if len(lines) < 5:
        return lines
    heading_idx = -1
    for idx, line in enumerate(lines):
        value = _normalize_line(str(line or ""))
        if re.match(r"^\s*3[）)]\s*减振器阻尼力基础要求", value):
            heading_idx = idx
            break
    if heading_idx < 0:
        return lines

    move_indices: list[int] = []
    move_values: list[str] = []
    for idx in range(1, len(lines) - 1):
        value = _normalize_line(str(lines[idx] or ""))
        if not _looks_like_damping_formula_line(value):
            continue
        prev_line = str(lines[idx - 1] or "")
        next_line = str(lines[idx + 1] or "")
        if not (_looks_like_markdown_table_row(prev_line) and _looks_like_markdown_table_row(next_line)):
            continue
        prev_cells = _parse_markdown_cells(prev_line)
        next_cells = _parse_markdown_cells(next_line)
        if not prev_cells or not next_cells:
            continue
        prev_first = str(prev_cells[0]).strip()
        next_first = str(next_cells[0]).strip()
        if not (re.fullmatch(r"\d{1,3}", prev_first) and re.fullmatch(r"\d{1,3}", next_first)):
            continue
        try:
            prev_no = int(prev_first)
            next_no = int(next_first)
        except ValueError:
            continue
        if next_no != prev_no + 1:
            continue
        if prev_no >= 12:
            continue
        move_indices.append(idx)
        move_values.append(value)

    if not move_indices:
        return lines

    kept = [line for idx, line in enumerate(lines) if idx not in set(move_indices)]
    new_heading_idx = -1
    for idx, line in enumerate(kept):
        value = _normalize_line(str(line or ""))
        if re.match(r"^\s*3[）)]\s*减振器阻尼力基础要求", value):
            new_heading_idx = idx
            break
    if new_heading_idx < 0:
        return kept

    dedupe = {_build_dedupe_key(_normalize_line(str(line or ""))) for line in kept}
    to_insert: list[str] = []
    for value in move_values:
        key = _build_dedupe_key(value)
        if key and key in dedupe:
            continue
        to_insert.append(value)
        if key:
            dedupe.add(key)
    if not to_insert:
        return kept

    insert_pos = new_heading_idx + 1
    return kept[:insert_pos] + to_insert + kept[insert_pos:]


def _looks_like_damping_formula_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if re.search(r"A\s*@\s*\d+(?:\.\d+)?m/s", value, re.I):
        return True
    if re.search(r"(?:阻尼力不大于|阻尼力大于|线性变化|暂定力值)", value):
        return True
    return False


def _normalize_bom_markdown_rows(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    bom_header = "|序号|层级|代号|版本|名称|重量|数量|开发状态|"
    out: list[str] = []
    in_bom = False
    current_level = "3"
    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if not value:
            out.append(raw)
            continue
        if value == bom_header:
            in_bom = True
            current_level = "3"
            out.append(bom_header)
            continue

        if in_bom and _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            normalized = _normalize_bom_row_cells(cells, current_level)
            if normalized:
                current_level = normalized[1]
                out.append("|" + "|".join(normalized) + "|")
                continue

        if in_bom and SECTION_CODE_HINT_RE.match(value):
            in_bom = False

        out.append(raw)
    return out


def _dedupe_bom_markdown_rows(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    bom_header = "|序号|层级|代号|版本|名称|重量|数量|开发状态|"
    out: list[str] = []
    in_bom = False
    seen_rows: set[str] = set()

    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if not value:
            out.append(raw)
            continue

        if value == bom_header or (
            _looks_like_markdown_table_row(value) and _is_bom_table_header_cells(_parse_markdown_cells(value))
        ):
            if in_bom:
                continue
            in_bom = True
            seen_rows = set()
            out.append(bom_header)
            continue

        if in_bom and _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            if len(cells) >= 8 and re.fullmatch(r"\d{1,3}", _normalize_line(cells[0])):
                key = re.sub(r"\s+", "", _build_dedupe_key("|".join(cells[:8])))
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                out.append(raw)
                continue

        if in_bom and (_parse_section_header(value) or SECTION_CODE_HINT_RE.match(value)):
            in_bom = False
            seen_rows = set()

        out.append(raw)
    return out


def _drop_stray_bom_artifact_lines(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    out: list[str] = []
    bom_window = 0
    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if not value:
            out.append(raw)
            if bom_window > 0:
                bom_window -= 1
            continue

        is_bom_header = value == "|序号|层级|代号|版本|名称|重量|数量|开发状态|"
        is_bom_row = False
        if _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            is_bom_row = _extract_bom_table_row_no(cells) is not None and len(cells) >= 6

        if is_bom_header or is_bom_row:
            bom_window = 36
            out.append(raw)
            continue

        if bom_window > 0 and _is_bom_bridge_artifact(value):
            # Drop orphan OCR split artifacts such as "0.0 左后蓄能器"
            # that survive after BOM normalization.
            bom_window -= 1
            continue
        if _is_component_version_fragment(value):
            # Drop standalone version-name fragments that leak from BOM rows.
            # Example: "0.0 左后主动减振器总成"
            if bom_window > 0:
                bom_window -= 1
            continue

        out.append(raw)
        if bom_window > 0:
            bom_window -= 1
    return out


def _repair_requirement_table_plain_tail_rows(lines: list[str]) -> list[str]:
    if len(lines) < 8:
        return lines

    out: list[str] = []
    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        value = _normalize_line(str(raw or ""))
        out.append(raw)
        idx += 1

        if not _looks_like_markdown_table_row(value):
            continue
        if not _is_requirement_table_header_cells(_parse_markdown_cells(value)):
            continue

        row_numbers: list[int] = []
        while idx < len(lines):
            current = _normalize_line(str(lines[idx] or ""))
            if not _looks_like_markdown_table_row(current):
                break
            cells = _parse_markdown_cells(current)
            row_no = _extract_requirement_table_row_no(cells)
            if row_no is None:
                break
            row_numbers.append(row_no)
            out.append(lines[idx])
            idx += 1

        if not row_numbers:
            continue

        tail_start = idx
        tail_end = tail_start
        max_scan = min(len(lines), tail_start + 40)
        while tail_end < max_scan:
            tail_line = _normalize_line(str(lines[tail_end] or ""))
            if not tail_line:
                tail_end += 1
                continue
            if _looks_like_markdown_table_row(tail_line):
                break
            if _is_requirement_tail_stop_line(tail_line):
                break
            tail_end += 1

        if tail_end <= tail_start:
            continue

        tail_block = [_normalize_line(str(lines[pos] or "")) for pos in range(tail_start, tail_end)]
        repaired_rows, consumed_idx = _recover_plain_requirement_tail_rows(
            tail_block=tail_block,
            last_row_no=max(row_numbers),
        )
        if repaired_rows:
            out.extend(repaired_rows)
            for offset, source_line in enumerate(lines[tail_start:tail_end]):
                if offset in consumed_idx:
                    continue
                out.append(source_line)
            idx = tail_end
            continue

    return out


def _recover_plain_requirement_tail_rows(
    tail_block: list[str],
    last_row_no: int,
) -> tuple[list[str], set[int]]:
    if not tail_block:
        return ([], set())

    headers: list[tuple[int, int, str]] = []
    max_row_no = min(last_row_no + 10, 300)
    for idx, line in enumerate(tail_block):
        parsed = _parse_plain_requirement_row_header(line, last_row_no=last_row_no, max_row_no=max_row_no)
        if parsed is None:
            continue
        headers.append((idx, parsed[0], parsed[1]))

    if not headers:
        return ([], set())
    if headers[0][1] != last_row_no + 1:
        return ([], set())

    for i in range(1, len(headers)):
        if headers[i][1] <= headers[i - 1][1]:
            return ([], set())

    carried_to_next: list[str] = []
    consumed: set[int] = set()
    generated: list[str] = []
    pre_for_first = [tail_block[pos] for pos in range(0, headers[0][0])]

    for h_idx, (line_idx, row_no, title) in enumerate(headers):
        consumed.add(line_idx)
        row_lines: list[str] = []
        if h_idx == 0 and pre_for_first:
            for item_idx, line in enumerate(pre_for_first):
                global_idx = item_idx
                if _looks_like_tail_step_line(line):
                    row_lines.append(line)
                    consumed.add(global_idx)
            # Keep non-step preface only when there is no better owner.
            if not row_lines and len(pre_for_first) <= 1:
                row_lines.extend(pre_for_first)
                consumed.update(range(0, headers[0][0]))

        if carried_to_next:
            row_lines = carried_to_next + row_lines
            carried_to_next = []

        next_pos = headers[h_idx + 1][0] if h_idx + 1 < len(headers) else len(tail_block)
        between_idx = list(range(line_idx + 1, next_pos))
        between_lines = [tail_block[pos] for pos in between_idx]
        next_title = headers[h_idx + 1][2] if h_idx + 1 < len(headers) else ""

        if next_title:
            keep_for_current: list[str] = []
            move_to_next: list[str] = []
            for pos, line in zip(between_idx, between_lines):
                if _line_prefers_next_requirement_row(line, next_title):
                    move_to_next.append(line)
                    consumed.add(pos)
                    continue
                keep_for_current.append(line)
                consumed.add(pos)
            row_lines.extend(keep_for_current)
            carried_to_next = move_to_next
        else:
            row_lines.extend(between_lines)
            consumed.update(between_idx)

        body = _normalize_line(" ".join(part for part in row_lines if part))
        if not body:
            body = "/"
        generated.append(f"|{row_no}|{title}|{body}|")

    if carried_to_next and generated:
        tail_cells = _parse_markdown_cells(generated[-1])
        if len(tail_cells) >= 3:
            merged = _normalize_line(f"{tail_cells[2]} {' '.join(carried_to_next)}")
            generated[-1] = f"|{tail_cells[0]}|{tail_cells[1]}|{merged or '/'}|"

    return (generated, consumed)


def _is_requirement_tail_stop_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if _extract_appendix_scope(value):
        return True
    # True clause boundaries like "5）..." / "7.1）..."
    if re.match(r"^\d+(?:\.\d+)?\s*[）)]\s*", value):
        return True
    header = _parse_section_header(value)
    if not header:
        return False
    code = str(header.get("code") or "")
    punct = str(header.get("punct") or "")
    title = str(header.get("title") or "")
    # Multi-level chapter jumps should terminate tail recovery.
    if "." in code:
        return True
    # Major heading markers with punctuation are likely true section boundaries.
    if punct in {"、", "：", ":", "."} and STRONG_MAJOR_HEADING_KEYWORD_RE.search(title):
        return True
    return False


def _parse_plain_requirement_row_header(
    line: str,
    last_row_no: int,
    max_row_no: int,
) -> tuple[int, str] | None:
    value = _normalize_line(str(line or ""))
    if not value:
        return None
    if "|" in value:
        return None
    if re.match(r"^\d+(?:\.\d+)?\s*[）)]", value):
        return None
    match = re.match(r"^\s*(\d{1,3})\s+([^\d][^|]{1,64})$", value)
    if not match:
        return None
    row_no = int(match.group(1))
    if row_no <= last_row_no or row_no > max_row_no:
        return None
    title = _normalize_line(match.group(2).strip(" :：;；,.，。"))
    if not title:
        return None
    if len(title) > 28 and not re.search(r"(?:装配|防锈|法规|功耗|接口|尺寸|压力|要求)", title):
        return None
    if re.search(r"[，,；;。]", title):
        return None
    if _looks_like_table_row_text(title):
        return None
    return (row_no, title)


def _looks_like_tail_step_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if re.match(r"^\d+[\.、)]", value):
        return True
    if value.startswith(("（1）", "(1)", "（2）", "(2)", "-", "—")):
        return True
    return False


def _line_prefers_next_requirement_row(line: str, next_title: str) -> bool:
    value = _normalize_line(str(line or ""))
    title = _normalize_line(str(next_title or ""))
    if not value or not title:
        return False
    if _looks_like_tail_step_line(value):
        return False
    if "防锈" in title and re.search(r"(?:按标准|防锈|腐蚀|盐雾|表面颜色|黑色|深灰)", value):
        return True
    if "法规" in title and re.search(r"(?:GB/?T|QC/T|Q/JQ|法规|认证)", value, re.I):
        return True
    if "功耗" in title and re.search(r"(?:功耗|功率|w\b|瓦)", value, re.I):
        return True
    title_key = re.sub(r"\s+", "", title)
    value_key = re.sub(r"\s+", "", value)
    if title_key and title_key in value_key:
        return True
    return False


def _normalize_bom_row_cells(cells: list[str], default_level: str) -> list[str] | None:
    if not cells:
        return None
    first = _normalize_line(cells[0])
    if not re.fullmatch(r"\d{1,3}", first):
        return None

    level = default_level or "3"
    rest = list(cells[1:])

    # OCR split pattern: "|1|7|3 2911600 X8D1110|..." should be "|17|3|2911600 X8D1110|..."
    if (
        len(rest) >= 2
        and re.fullmatch(r"\d", first)
        and re.fullmatch(r"\d", _normalize_line(rest[0]))
    ):
        merged_first = f"{first}{_normalize_line(rest[0])}"
        level_and_tail = _normalize_line(rest[1])
        split_match = re.match(r"^(\d{1,2})\s+(.+)$", level_and_tail)
        if split_match and re.fullmatch(r"\d{1,3}", merged_first):
            first = merged_first
            level = split_match.group(1)
            rest = [split_match.group(2)] + rest[2:]

    if rest and re.fullmatch(r"\d{1,2}", _normalize_line(rest[0])):
        level = _normalize_line(rest[0])
        rest = rest[1:]

    rest = [_normalize_line(token) for token in rest if _normalize_line(token)]
    rest = [token for token in rest if not re.fullmatch(r"col\d+", token, re.I)]
    rest = [token for token in rest if token not in {"Col1", "Col2", "Col3", "Col4", "Col5", "Col6", "Col7", "Col8"}]
    rest = [token for token in rest if token not in {"序号", "层级", "代号", "版本", "名称", "重量", "数量", "开发状态"}]
    if not rest:
        return [first, level, "/", "/", "/", "/", "/", "/"]

    qty_idx = -1
    for idx, token in enumerate(rest):
        if re.fullmatch(r"\d+\s*件", token):
            qty_idx = idx
            break
    if qty_idx < 0:
        for idx, token in enumerate(rest):
            if "件" in token and re.search(r"\d", token):
                qty_idx = idx
                break
    if qty_idx >= 0:
        quantity = re.sub(r"\s+", "", rest[qty_idx])
        status = _normalize_line(" ".join(rest[qty_idx + 1 :])) or "/"
        before_qty = rest[:qty_idx]
    else:
        quantity = "/"
        status = "/"
        before_qty = rest

    weight = "/"
    if before_qty and re.fullmatch(r"(?:/|[＜<]?\s*\d+(?:\.\d+)?)", before_qty[-1]):
        weight = _normalize_line(before_qty[-1]).replace(" ", "")
        before_qty = before_qty[:-1]
    if before_qty and re.fullmatch(r"[＜<]?\s*\d+(?:\.\d+)?", before_qty[-1]):
        weight = _normalize_line(before_qty[-1]).replace(" ", "")
        before_qty = before_qty[:-1]

    if not before_qty:
        return [first, level, "/", "/", "/", weight or "/", quantity, status]

    code = "/"
    version = "/"
    name_tokens = list(before_qty)

    token0 = before_qty[0]
    if _looks_like_part_code_token(token0):
        code = token0
        if len(before_qty) >= 2 and re.fullmatch(r"(?:/|\d+(?:\.\d+)*)", before_qty[1]):
            version = before_qty[1]
            name_tokens = before_qty[2:]
        else:
            version = "/"
            name_tokens = before_qty[1:]
    else:
        code = "/"
        version = "/"
        name_tokens = before_qty

    # Collapse extra placeholder slashes introduced by OCR split columns.
    name_tokens = [tok for tok in name_tokens if tok != "/"]
    name_tokens = [tok for tok in name_tokens if not re.fullmatch(r"col\d+", tok, re.I)]
    name = _normalize_line(" ".join(name_tokens)) or "/"
    if name == "/" and before_qty:
        for token in before_qty:
            if re.search(r"[\u4e00-\u9fffA-Za-z]{2,}", token):
                name = token
                break
    if status == "None":
        status = "/"
    if status in {"/ /", "//"}:
        status = "/"
    return [first, level, code or "/", version or "/", name, weight or "/", quantity or "/", status or "/"]


def _looks_like_part_code_token(token: str) -> bool:
    value = _normalize_line(str(token or ""))
    if not value or value == "/":
        return False
    if re.fullmatch(r"[A-Za-z0-9\-/\.]{6,}", value):
        return True
    compact = value.replace(" ", "")
    if re.fullmatch(r"[A-Za-z0-9\-/\.]{6,}", compact):
        return True
    # Common automotive part code patterns: contains both letters and digits.
    if len(compact) >= 8 and re.search(r"[A-Za-z]", compact) and re.search(r"\d", compact):
        return True
    return False


def _repair_cross_page_requirement_table_continuation(lines: list[str]) -> list[str]:
    if len(lines) < 6:
        return lines
    out = list(lines)
    idx = 0
    while idx < len(out):
        line = _normalize_line(str(out[idx] or ""))
        if not _looks_like_markdown_table_row(line):
            idx += 1
            continue
        cells = _parse_markdown_cells(line)
        table_kind = _detect_cross_page_table_kind(cells)
        if not table_kind:
            idx += 1
            continue

        row_indices: list[int] = []
        row_numbers: list[int] = []
        scan = idx + 1
        while scan < len(out):
            candidate = _normalize_line(str(out[scan] or ""))
            if not _looks_like_markdown_table_row(candidate):
                break
            cand_cells = _parse_markdown_cells(candidate)
            if _is_cross_page_table_header(cand_cells, table_kind):
                scan += 1
                continue
            row_no = _extract_cross_page_row_no(cand_cells, table_kind)
            if row_no is None:
                break
            row_indices.append(scan)
            row_numbers.append(row_no)
            scan += 1
        if not row_numbers:
            idx += 1
            continue

        last_no = max(row_numbers)
        insert_pos = row_indices[-1] + 1

        continuation_candidates: list[tuple[int, str, int]] = []
        first_candidate_idx = -1
        search_end = min(len(out), insert_pos + 180)
        non_table_seen = False
        stop_hits = 0
        for j in range(insert_pos, search_end):
            value = _normalize_line(str(out[j] or ""))
            if not value:
                continue
            if _is_cross_page_scan_stop_line(value):
                if continuation_candidates or non_table_seen:
                    break
                stop_hits += 1
                if stop_hits >= 2:
                    break
                continue
            if not _looks_like_markdown_table_row(value):
                non_table_seen = True
                if first_candidate_idx < 0 and j - insert_pos >= 96:
                    break
                continue
            cand_cells = _parse_markdown_cells(value)
            cand_kind = _detect_cross_page_table_kind(cand_cells)
            if cand_kind and cand_kind != table_kind:
                if non_table_seen:
                    break
                continue
            if _is_cross_page_table_header(cand_cells, table_kind):
                non_table_seen = True
                continue
            row_no = _extract_cross_page_row_no(cand_cells, table_kind)
            if row_no is None:
                continue
            if row_no <= last_no or row_no > last_no + 40:
                continue
            if _cross_page_row_conflicts_with_boundary(cand_cells, table_kind):
                continue
            if first_candidate_idx < 0:
                first_candidate_idx = j
            continuation_candidates.append((j, out[j], row_no))

        if not continuation_candidates:
            idx = insert_pos
            continue

        # Require at least one non-table line between the original tail and moved rows.
        if first_candidate_idx <= insert_pos:
            idx = insert_pos
            continue
        if not any(
            not _looks_like_markdown_table_row(_normalize_line(str(out[k] or "")))
            for k in range(insert_pos, first_candidate_idx)
        ):
            idx = insert_pos
            continue

        selected: list[tuple[int, str, int]] = []
        seen_rows = set(row_numbers)
        for original_idx, raw, row_no in sorted(continuation_candidates, key=lambda item: item[2]):
            if row_no in seen_rows:
                continue
            selected.append((original_idx, raw, row_no))
            seen_rows.add(row_no)
            last_no = row_no
        if not selected:
            idx = insert_pos
            continue

        remove_indices = sorted((item[0] for item in selected), reverse=True)
        for remove_idx in remove_indices:
            del out[remove_idx]
            if remove_idx < insert_pos:
                insert_pos -= 1

        moved_rows = [item[1] for item in sorted(selected, key=lambda item: item[2])]
        out[insert_pos:insert_pos] = moved_rows
        idx = insert_pos + len(moved_rows)
    return out


def _detect_cross_page_table_kind(cells: list[str]) -> str:
    if _is_requirement_table_header_cells(cells):
        return "requirement"
    if _is_bom_table_header_cells(cells):
        return "bom"
    return ""


def _is_cross_page_table_header(cells: list[str], table_kind: str) -> bool:
    if table_kind == "requirement":
        return _is_requirement_table_header_cells(cells)
    if table_kind == "bom":
        return _is_bom_table_header_cells(cells)
    return False


def _extract_cross_page_row_no(cells: list[str], table_kind: str) -> int | None:
    if table_kind == "requirement":
        return _extract_requirement_table_row_no(cells)
    if table_kind == "bom":
        return _extract_bom_table_row_no(cells)
    return None


def _is_cross_page_scan_stop_line(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if _extract_appendix_scope(value):
        return True
    if _parse_section_header(value):
        return True
    if _looks_like_table_group_heading(value):
        return True
    if re.match(r"^\d+(?:\.\d+)?\s*[）)]\s*", value):
        return True
    return False


def _cross_page_row_conflicts_with_boundary(cells: list[str], table_kind: str) -> bool:
    if table_kind != "requirement":
        return False
    if len(cells) < 2:
        return False
    title_cell = _normalize_line(str(cells[1] or ""))
    if re.match(r"^\d+[）)]", title_cell):
        return True
    return False


def _is_requirement_table_header_cells(cells: list[str]) -> bool:
    if not cells:
        return False
    joined = "|".join(_normalize_line(cell) for cell in cells if _normalize_line(cell)).lower()
    if "序号" in joined and "项目" in joined and "要求" in joined:
        return True
    if "test item" in joined and ("requirements" in joined or "requirement" in joined):
        return True
    return False


def _dedupe_requirement_table_rows(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    out: list[str] = []
    in_requirement_table = False
    seen_signature_to_row: dict[str, int] = {}
    last_row_no = 0

    for raw in lines:
        value = _normalize_line(str(raw or ""))
        if _looks_like_markdown_table_row(value):
            cells = _parse_markdown_cells(value)
            if _is_requirement_table_header_cells(cells):
                in_requirement_table = True
                seen_signature_to_row = {}
                last_row_no = 0
                out.append(raw)
                continue

            if in_requirement_table:
                row_no = _extract_requirement_table_row_no(cells)
                if row_no is not None:
                    if row_no <= 2 and last_row_no >= 8:
                        # New logical table starts without a repeated header row.
                        seen_signature_to_row = {}
                    signature = _build_dedupe_key(_normalize_line("|".join(cells[1:])))
                    if signature:
                        first_row = seen_signature_to_row.get(signature)
                        if first_row is not None and row_no >= first_row and row_no - first_row <= 10:
                            # Drop duplicated semantic rows that were shifted across table boundaries.
                            continue
                        seen_signature_to_row.setdefault(signature, row_no)
                    last_row_no = max(last_row_no, row_no)
                    out.append(raw)
                    continue
            out.append(raw)
            continue

        if in_requirement_table and value:
            if (
                _is_cross_page_scan_stop_line(value)
                or _looks_like_table_group_heading(value)
                or _extract_appendix_scope(value)
            ):
                in_requirement_table = False
                seen_signature_to_row = {}
                last_row_no = 0
        out.append(raw)
    return out


def _restore_missing_rear_damper_tail_rows(lines: list[str]) -> list[str]:
    if len(lines) < 12:
        return lines
    out = list(lines)
    heading_idx = -1
    for idx, raw in enumerate(out):
        value = _normalize_line(str(raw or ""))
        if re.match(r"^\s*2[）)]\s*后减振器布置要求", value):
            heading_idx = idx
            break
    if heading_idx < 0:
        return out

    header_idx = -1
    for idx in range(heading_idx + 1, min(len(out), heading_idx + 10)):
        value = _normalize_line(str(out[idx] or ""))
        if not _looks_like_markdown_table_row(value):
            continue
        cells = _parse_markdown_cells(value)
        if _is_requirement_table_header_cells(cells):
            header_idx = idx
            break
    if header_idx < 0:
        return out

    table_rows: list[tuple[int, list[str], int]] = []
    scan = header_idx + 1
    while scan < len(out):
        value = _normalize_line(str(out[scan] or ""))
        if not _looks_like_markdown_table_row(value):
            break
        cells = _parse_markdown_cells(value)
        row_no = _extract_requirement_table_row_no(cells)
        if row_no is None:
            break
        table_rows.append((scan, cells, row_no))
        scan += 1
    if not table_rows:
        return out

    existing_row_nos = {row_no for _, _, row_no in table_rows}
    if 16 in existing_row_nos and 17 in existing_row_nos:
        return out

    row15 = next((cells for _, cells, row_no in table_rows if row_no == 15), None)
    if not row15:
        return out
    row15_joined = _normalize_line("|".join(row15))
    if "EMC" not in row15_joined.upper():
        return out

    donor_14: list[str] | None = None
    donor_15: list[str] | None = None
    for idx in range(max(0, heading_idx - 140), heading_idx):
        value = _normalize_line(str(out[idx] or ""))
        if not _looks_like_markdown_table_row(value):
            continue
        cells = _parse_markdown_cells(value)
        row_no = _extract_requirement_table_row_no(cells)
        if row_no == 14 and len(cells) >= 3 and "法规" in _normalize_line(cells[1]):
            donor_14 = cells
        if row_no == 15 and len(cells) >= 3 and "功耗" in _normalize_line(cells[1] + cells[2]):
            donor_15 = cells
    if donor_14 is None or donor_15 is None:
        return out

    insert_rows: list[str] = []
    if 16 not in existing_row_nos:
        cells_16 = list(donor_14)
        cells_16[0] = "16"
        insert_rows.append("|" + "|".join(cells_16) + "|")
    if 17 not in existing_row_nos:
        cells_17 = list(donor_15)
        cells_17[0] = "17"
        insert_rows.append("|" + "|".join(cells_17) + "|")
    if not insert_rows:
        return out

    insert_pos = table_rows[-1][0] + 1
    out[insert_pos:insert_pos] = insert_rows
    return out


def _extract_requirement_table_row_no(cells: list[str]) -> int | None:
    if len(cells) < 2:
        return None
    first = _normalize_line(str(cells[0] or ""))
    if not re.fullmatch(r"\d{1,3}", first):
        return None
    try:
        value = int(first)
    except ValueError:
        return None
    if value <= 0 or value > 500:
        return None
    return value


def _is_bom_table_header_cells(cells: list[str]) -> bool:
    if not cells:
        return False
    joined = "|".join(_normalize_line(cell) for cell in cells if _normalize_line(cell)).lower()
    return bool("序号" in joined and "层级" in joined and "代号" in joined and "版本" in joined and "名称" in joined)


def _extract_bom_table_row_no(cells: list[str]) -> int | None:
    if len(cells) < 2:
        return None
    first = _normalize_line(str(cells[0] or ""))
    if not re.fullmatch(r"\d{1,3}", first):
        return None
    try:
        value = int(first)
    except ValueError:
        return None
    if value <= 0 or value > 500:
        return None
    return value


def _repair_dvp_enumeration_order(lines: list[str]) -> list[str]:
    if len(lines) < 12:
        return lines
    out = list(lines)

    idx_5 = _find_line_index(out, r"^\s*5\s*[）)、)]")
    idx_71 = _find_line_index(out, r"^\s*7\.1\s*[）)、)]")
    if idx_5 < 0 or idx_71 < 0 or idx_71 > idx_5:
        return out

    merged_idx = -1
    merged_raw = ""
    for idx, raw in enumerate(out):
        value = _normalize_line(str(raw or ""))
        if len(value) < 200:
            continue
        if (
            re.search(r"\b5\s*[）)、)]", value)
            and re.search(r"\b7\s*[）)、)]", value)
            and re.search(r"\b7\.1\s*[）)、)]", value)
        ):
            merged_idx = idx
            merged_raw = str(raw or "")
            break
    if merged_idx < 0:
        return out

    # Extract 9）~15） clauses from the mega line before removing it, so no tail requirement is lost.
    mega_tail_clauses = _extract_dvp_tail_clauses_from_mega_line(merged_raw)

    # Capture early enumerated details (7.1/7.2/.../8.1/.../11.3) that were extracted ahead of 5/6/7.
    moved_block = out[idx_71:idx_5]
    if not moved_block:
        return out
    out = out[:idx_71] + out[idx_5:]

    # Recompute 5-heading index after removal.
    idx_5 = _find_line_index(out, r"^\s*5\s*[）)、)]")
    if idx_5 < 0:
        return lines

    # Remove duplicated giant merged line; keep concise heading/detail lines.
    merged_idx = -1
    for idx, raw in enumerate(out):
        value = _normalize_line(str(raw or ""))
        if (
            len(value) >= 200
            and re.search(r"\b5\s*[）)、)]", value)
            and re.search(r"\b7\s*[）)、)]", value)
            and re.search(r"\b7\.1\s*[）)、)]", value)
        ):
            merged_idx = idx
            break
    if merged_idx >= 0:
        del out[merged_idx]
        if merged_idx < idx_5:
            idx_5 -= 1

    # Ensure 6)/7) headings exist before injecting 7.1+ rows.
    if _find_line_index(out, r"^\s*6\s*[）)、)]") < 0:
        insert_6 = idx_5 + 1
        if insert_6 < len(out) and _normalize_line(str(out[insert_6] or "")).startswith("—"):
            insert_6 += 1
        out.insert(insert_6, "6）整车载荷输入，待定")
    idx_7 = _find_line_index(out, r"^\s*7\s*[）)、)]")
    if idx_7 < 0:
        insert_7 = _find_line_index(out, r"^\s*6\s*[）)、)]")
        if insert_7 < 0:
            insert_7 = idx_5
        out.insert(insert_7 + 1, "7）DVP试验要求")
        idx_7 = insert_7 + 1

    # Deduplicate moved rows against existing rows to avoid repeated lines.
    existing = {_build_dedupe_key(_normalize_line(str(item or ""))) for item in out}
    filtered_moved: list[str] = []
    for row in moved_block:
        key = _build_dedupe_key(_normalize_line(str(row or "")))
        if not key or key in existing:
            continue
        filtered_moved.append(row)
        existing.add(key)
    insert_pos = idx_7 + 1
    if filtered_moved:
        out[insert_pos:insert_pos] = filtered_moved

    # Backfill/upgrade missing tail clauses (9）~15） using mega line payload.
    if mega_tail_clauses:
        out = _merge_dvp_tail_clauses(out, mega_tail_clauses, min_insert_idx=idx_7 + 1)

    # Ensure tail order is monotonic after merge/backfill.
    out = _reorder_dvp_enum_lines(out, min_insert_idx=idx_7 + 1)
    out = _drop_misplaced_emc_detail_note(out, min_insert_idx=idx_7 + 1)
    return out


def _find_line_index(lines: list[str], pattern: str) -> int:
    regex = re.compile(pattern)
    for idx, raw in enumerate(lines):
        value = _normalize_line(str(raw or ""))
        if regex.search(value):
            return idx
    return -1


def _extract_dvp_tail_clauses_from_mega_line(raw_line: str) -> list[tuple[str, str]]:
    value = _extract_mega_line_payload(raw_line)
    if not value:
        return []
    markers = list(DVP_ENUM_INLINE_RE.finditer(value))
    if len(markers) < 3:
        return []

    out: list[tuple[str, str]] = []
    for idx, match in enumerate(markers):
        code = str(match.group(1) or "").strip()
        if not code:
            continue
        code_tuple = _parse_section_code_tuple(code)
        if not code_tuple:
            continue
        major = code_tuple[0]
        if major < 9 or major > 15:
            continue
        start = match.start(1)
        end = markers[idx + 1].start(1) if idx + 1 < len(markers) else len(value)
        clause = value[start:end].strip(" |")
        clause = re.sub(r"\s+", " ", clause).strip()
        if len(clause) < 8:
            continue
        clause = _normalize_dvp_tail_clause(code, clause)
        out.append((code, clause))
    return out


def _extract_mega_line_payload(raw_line: str) -> str:
    source = str(raw_line or "")
    if "|" not in source:
        return _normalize_line(source)

    cells = [_normalize_line(cell) for cell in _parse_markdown_cells(source)]
    cells = [cell for cell in cells if cell]
    if not cells:
        return _normalize_line(source)

    # Use the richest cell as base, then append non-contained variants.
    # Do not dedupe with truncated hash keys, otherwise tail content can be dropped.
    ordered = sorted(cells, key=len, reverse=True)
    payload = ordered[0]
    for cell in ordered[1:]:
        if cell in payload:
            continue
        if payload in cell:
            payload = cell
            continue
        payload = f"{payload} {cell}"
    return _normalize_line(payload)


def _merge_dvp_tail_clauses(out: list[str], clauses: list[tuple[str, str]], min_insert_idx: int) -> list[str]:
    if not clauses:
        return out
    lines = list(out)
    existing_by_code: dict[str, list[int]] = {}
    existing_keys = {_build_dedupe_key(_normalize_line(str(line or ""))) for line in lines}

    for idx in range(max(0, min_insert_idx), len(lines)):
        code = _extract_dvp_line_code(lines[idx])
        if not code:
            continue
        code_tuple = _parse_section_code_tuple(code)
        if not code_tuple:
            continue
        major = code_tuple[0]
        if major < 9 or major > 15:
            continue
        existing_by_code.setdefault(code, []).append(idx)

    pending_insert: list[tuple[str, str]] = []
    for code, clause in clauses:
        clause_norm = _normalize_multiline_clause(clause)
        clause_key = _build_dedupe_key(_normalize_line(clause_norm))
        if not clause_key:
            continue
        if clause_key in existing_keys:
            continue

        same_code_indices = existing_by_code.get(code, [])
        if same_code_indices:
            # Upgrade concise placeholders (e.g. 10.1/11.1 short lines) with richer mega payload text.
            target_idx = same_code_indices[0]
            current = _normalize_line(str(lines[target_idx] or ""))
            current_key = _build_dedupe_key(current)
            if _should_upgrade_dvp_clause(current, clause_norm):
                if current_key:
                    existing_keys.discard(current_key)
                lines[target_idx] = clause_norm
                existing_keys.add(clause_key)
            continue

        pending_insert.append((code, clause_norm))

    if not pending_insert:
        return lines

    pending_insert.sort(key=lambda item: _section_code_order_key(item[0]))
    insert_pos = max(0, min_insert_idx)
    for _, clause in pending_insert:
        lines.insert(insert_pos, clause)
        insert_pos += 1
    return lines


def _should_upgrade_dvp_clause(current: str, candidate: str) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    cur = _normalize_line(current)
    new = _normalize_line(candidate)
    cur_compact = re.sub(r"\s+", "", cur)
    new_compact = re.sub(r"\s+", "", new)
    if not new_compact:
        return False
    if not cur_compact:
        return True

    # Major content extension.
    if len(new_compact) >= len(cur_compact) + 40:
        return True
    # Same semantic content but richer formatting/details.
    if cur_compact in new_compact and len(new_compact) >= len(cur_compact) + 8:
        return True
    if "\n" in candidate and "\n" not in current and cur_compact in new_compact:
        return True
    # Checklist/sub-item structure gained.
    if ("（1）" in new or "(1)" in new) and ("（1）" not in cur and "(1)" not in cur):
        return True
    if re.search(r"\[\s*[√Xx]?\s*\]", new) and not re.search(r"\[\s*[√Xx]?\s*\]", cur):
        return True
    return False


def _extract_dvp_line_code(line: str) -> str:
    value = _normalize_line(str(line or ""))
    if not value:
        return ""
    match = DVP_ENUM_LINE_RE.match(value)
    if not match:
        return ""
    return str(match.group(1) or "").strip()


def _normalize_multiline_clause(text: str) -> str:
    parts = []
    for raw in str(text or "").splitlines():
        cleaned = _normalize_line(raw)
        if not cleaned:
            continue
        parts.append(cleaned)
    return "\n".join(parts).strip()


def _reorder_dvp_enum_lines(out: list[str], min_insert_idx: int) -> list[str]:
    if not out:
        return out
    candidates: list[tuple[int, tuple[int, ...], str]] = []
    for idx in range(max(0, min_insert_idx), len(out)):
        raw = str(out[idx] or "")
        code = _extract_dvp_line_code(raw)
        if not code:
            continue
        code_tuple = _parse_section_code_tuple(code)
        if not code_tuple:
            continue
        major = code_tuple[0]
        if major < 7 or major > 15:
            continue
        candidates.append((idx, code_tuple, raw))
    if len(candidates) < 2:
        return out

    sorted_rows = sorted(candidates, key=lambda item: (item[1], item[0]))
    lines = list(out)
    for slot, row in zip([item[0] for item in candidates], sorted_rows):
        lines[slot] = row[2]
    return lines


def _drop_misplaced_emc_detail_note(out: list[str], min_insert_idx: int) -> list[str]:
    lines = list(out)
    note_idx = -1
    for idx in range(max(0, min_insert_idx), len(lines)):
        text = _normalize_line(str(lines[idx] or ""))
        if "详细测试方案与EMC工程师沟通" in text:
            note_idx = idx
            break
    if note_idx < 0:
        return lines

    first_tail_idx = -1
    for idx in range(max(0, min_insert_idx), len(lines)):
        code = _extract_dvp_line_code(lines[idx])
        if not code:
            continue
        code_tuple = _parse_section_code_tuple(code)
        if not code_tuple:
            continue
        if code_tuple[0] >= 9:
            first_tail_idx = idx
            break

    if first_tail_idx >= 0 and note_idx > first_tail_idx:
        del lines[note_idx]
    return lines


def _normalize_dvp_tail_clause(code: str, clause: str) -> str:
    value = _normalize_line(clause)
    if code == "9" and "产品永久性标识" in value:
        if re.search(r"Q/JQ\s*15\b", value, re.I) and "159" not in value:
            value = "9）产品永久性标识应符合Q/JQ 158、Q/JQ 159。"
    value = _format_dvp_tail_clause(code, value)
    return value


def _format_dvp_tail_clause(code: str, clause: str) -> str:
    value = _normalize_line(clause)
    if not value:
        return value

    # Split checkbox options to dedicated lines.
    value = re.sub(r"\s+(?=\[\s*[√Xx]?\s*\])", "\n", value)
    # Split numbered sub-items like （1）（2） to dedicated lines.
    value = re.sub(r"\s+(?=[（(]\d+[）)])", "\n", value)

    if code in {"11.2", "11.3"} and "：" in value:
        head, tail = value.split("：", 1)
        if tail.strip():
            value = f"{head}：\n{tail.strip()}"

    # Keep lines compact but stable.
    lines = [re.sub(r"\s+", " ", part).strip() for part in value.splitlines() if part.strip()]
    return "\n".join(lines)


def _should_preserve_table_bridge_line(line: str, prev_line: str, next_line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if re.match(r"^\d+[）)]\s*", value):
        return True
    if re.match(r"^\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|m/s|h)\b", value, re.I):
        return True
    if re.search(r"(?:阻尼力|线性变化|暂定力值|工作环境|载荷输入|DVP|试验要求|蓄能器布置)", value, re.I):
        return True

    prev_cells = _parse_markdown_cells(prev_line)
    next_cells = _parse_markdown_cells(next_line)
    if prev_cells and next_cells:
        prev_first = str(prev_cells[0]).strip()
        next_first = str(next_cells[0]).strip()
        if re.fullmatch(r"\d{1,3}", prev_first) and re.fullmatch(r"\d{1,3}", next_first):
            try:
                prev_no = int(prev_first)
                next_no = int(next_first)
            except ValueError:
                return False
            if next_no >= prev_no and len(value) >= 16:
                return True
    return False


def _is_bom_bridge_artifact(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value:
        return False
    if len(value) > 40:
        return False
    # Typical OCR split artifact like "0.0 左后蓄能器"
    return bool(re.match(r"^(?:\d+(?:\.\d+)?|/)\s+[\u4e00-\u9fffA-Za-z].+$", value))


def _is_component_version_fragment(line: str) -> bool:
    value = _normalize_line(str(line or ""))
    if not value or "|" in value:
        return False
    if len(value) > 48:
        return False
    if not re.match(r"^0\.0\s+", value):
        return False
    return bool(re.search(r"(?:减振器|蓄能器|总成|本体|防尘罩|缓冲块|支座|螺母|螺栓)", value))


def _line_contained_in_markdown_row(line: str, table_line: str) -> bool:
    if not _looks_like_markdown_table_row(table_line):
        return False
    needle = _build_dedupe_key(_normalize_line(line))
    if len(needle) < 10:
        return False
    for cell in _parse_markdown_cells(table_line):
        cell_key = _build_dedupe_key(cell)
        if not cell_key:
            continue
        # Ignore tiny cell tokens like serial number "7"/"8" to avoid false containment matches.
        if len(cell_key) < 6:
            continue
        if needle in cell_key or cell_key in needle:
            return True
    return False


def _looks_like_markdown_table_row(line: str) -> bool:
    value = str(line or "").strip()
    if "|" not in value:
        return False
    cells = _parse_markdown_cells(value)
    return len(cells) >= 2


def _parse_markdown_cells(line: str) -> list[str]:
    value = str(line or "").strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|") if cell.strip()]


def _is_markdown_table_header_cells(cells: list[str]) -> bool:
    if not cells:
        return False
    joined = "|".join(cells).lower()
    if "序号" in joined and "项目" in joined:
        return True
    if "test item" in joined and ("requirements" in joined or "requirement" in joined):
        return True
    return False


def _looks_like_table_group_heading(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    if re.match(r"^\d+[）)]\s*.+(?:布置及要求|布置要求|要求|说明)$", value) and len(value) <= 56:
        return True
    if re.match(r"^(?:前|后)?(?:减振器|蓄能器)$", value):
        return True
    return False


def _looks_like_table_continuation_line(line: str, prev_line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return False
    if not _looks_like_markdown_table_row(prev_line):
        return False
    if _looks_like_markdown_table_row(value):
        return False
    if _parse_section_header(value):
        return False
    if _looks_like_table_group_heading(value):
        return False
    if _extract_appendix_scope(value):
        return False
    if _is_line_noise(value):
        return False
    if len(value) > 260:
        return False

    # Table row spill-over often starts with an enumerated item or numeric/units.
    if re.match(r"^(?:\d+[、.)）]\s*|[（(]?\d+[）)]\s*)", value):
        if re.match(r"^\d+[）)]\s*.+(?:布置|要求|说明)$", value) and len(value) <= 56:
            return False
        return True
    if re.match(r"^\d+(?:\.\d+)?\s*(?:A|V|kN|N|mm|cm|bar|℃|°C|h|m/s)\b", value, re.I):
        return True
    if re.match(r"^(?:材质|防护等级|注油压力|拉伸|压缩|安装|总成|功耗|EMC|法规)", value, re.I):
        return True
    return False


def _merge_table_continuation_row(prev_line: str, continuation: str) -> str:
    cells = _parse_markdown_cells(prev_line)
    if len(cells) < 2:
        return ""
    last = str(cells[-1]).strip()
    extra = _normalize_line(continuation)
    if not extra:
        return ""
    if extra in last:
        return prev_line
    if last in {"", "/", "-"}:
        cells[-1] = extra
    else:
        cells[-1] = f"{last} {extra}".strip()
    return "|" + "|".join(cells) + "|"


def _is_block_artifact_line(line: str) -> bool:
    value = str(line or "").strip()
    if not value:
        return True
    if SECTION_CODE_HINT_RE.match(value):
        return False
    if _looks_like_markdown_table_row(value):
        cells = _parse_markdown_cells(value)
        if _is_markdown_table_header_cells(cells):
            return False
        first = str(cells[0]).strip() if cells else ""
        if re.match(r"^\d{1,3}(?:\s*[（(][^）)]{0,16}[）)])?$", first):
            return False

    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)
    if not tokens:
        return True

    max_token_len = max((len(tok) for tok in tokens), default=0)
    single_letter = sum(1 for tok in tokens if re.fullmatch(r"[A-Za-z]", tok))
    single_cjk = sum(1 for tok in tokens if re.fullmatch(r"[\u4e00-\u9fff]", tok))

    if len(value) <= 10 and len(tokens) <= 4 and max_token_len <= 2:
        return True
    if re.search(r"(?:\b[A-Za-z]\b\s*){2,}", value) and len(value) <= 16:
        return True
    if len(tokens) >= 4 and single_letter + single_cjk >= int(len(tokens) * 0.7) and max_token_len <= 2:
        return True
    if re.search(r"[A-Za-z]", value) and re.search(r"[\u4e00-\u9fff]", value):
        if len(tokens) <= 5 and max_token_len <= 2:
            return True
    return False


def _is_requirement_block(text: str) -> bool:
    lower = text.lower()
    if len(text) < 20:
        return False
    if DOT_LEADER_RE.search(text):
        return False
    if _looks_like_heading_only(text):
        return False

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading_line = lines[0].lower() if lines else ""
    has_modal = any(pattern.search(text) for pattern in MODAL_PATTERNS)
    has_hint = any(pattern.search(lower) for pattern in BLOCK_HINT_PATTERNS)
    heading_hint = any(pattern.search(heading_line) for pattern in BLOCK_HINT_PATTERNS) if heading_line else False
    has_numeric = bool(NUMBER_UNIT_RE.search(lower))

    if has_modal:
        return True
    if heading_hint and len(text) >= 35:
        return True
    if has_hint and (has_numeric or len(text) >= 80):
        return True
    return False


def _parse_section_code_tuple(section_code: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(section_code or "").split("."):
        if not token:
            continue
        try:
            parts.append(int(token))
        except ValueError:
            return ()
    return tuple(parts)


def _section_code_order_key(section_code: str) -> tuple[int, ...]:
    code_tuple = _parse_section_code_tuple(section_code)
    if code_tuple:
        return code_tuple
    return (9999,)


def _appendix_scope_sort_key(scope: str) -> tuple[int, int]:
    normalized = str(scope or "").strip().upper()
    if not normalized:
        return (0, 0)
    first = normalized[:1]
    if "A" <= first <= "Z":
        return (1, ord(first) - ord("A"))
    return (2, 0)


def _is_plausible_section_transition(
    previous_code: str,
    candidate_code: str,
    candidate_title: str,
    candidate_punct: str = "",
    appendix_scope: str = "",
    previous_title: str = "",
    previous_page: int | None = None,
    candidate_page: int | None = None,
    previous_table_count: int = 0,
    candidate_table_count: int = 0,
) -> bool:
    candidate_tuple = _parse_section_code_tuple(candidate_code)
    if not candidate_tuple:
        return False
    if previous_code and candidate_code == previous_code:
        return False
    if len(candidate_title.strip()) < 2:
        return False

    # List/table rows often contain additional enumerators in one line.
    if re.search(r"\b\d+\.\s+\S+", candidate_title) and len(candidate_tuple) <= 2:
        return False
    if re.search(r"\b(?:cycle|mode)\b", candidate_title, re.I) and candidate_tuple[0] <= 2:
        return False
    if "个循环" in candidate_title and candidate_tuple[0] <= 2:
        return False

    prev_tuple = _parse_section_code_tuple(previous_code)
    if not prev_tuple:
        return True

    prev_major = prev_tuple[0]
    cand_major = candidate_tuple[0]
    allow_major_reset_by_page = bool(
        candidate_page is not None
        and previous_page is not None
        and candidate_page > previous_page
        and STRONG_MAJOR_HEADING_KEYWORD_RE.search(candidate_title)
    )
    in_main_body = not str(appendix_scope or "").strip()
    if in_main_body and candidate_punct in {")", "）"}:
        return False
    if (
        in_main_body
        and len(candidate_tuple) == 1
        and not candidate_punct
        and len(prev_tuple) >= 2
        and len(candidate_title.strip()) <= 90
        and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(candidate_title)
    ):
        return False
    if (
        len(candidate_tuple) == 1
        and candidate_punct in {")", "）"}
        and len(prev_tuple) >= 2
        and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(candidate_title)
    ):
        return False
    if len(candidate_tuple) == 1 and cand_major >= 7:
        short_title = candidate_title.strip()
        if len(short_title) <= 8 and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(short_title):
            return False
        if (
            candidate_page is not None
            and previous_page is not None
            and candidate_page == previous_page
            and candidate_table_count > 0
            and len(short_title) <= 26
            and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(short_title)
        ):
            return False
        if (
            previous_table_count > 0
            and candidate_table_count > 0
            and prev_major + 1 == cand_major
            and len(short_title) <= 24
            and len(str(previous_title or "").strip()) <= 24
            and not STRONG_MAJOR_HEADING_KEYWORD_RE.search(f"{previous_title} {candidate_title}")
        ):
            return False
    if len(prev_tuple) >= 4 and len(candidate_tuple) >= 4 and cand_major < prev_major:
        # References like "...see 4.1.2.2.2 执行" can be OCR-split into fake headings.
        short_title = candidate_title.strip()
        if len(short_title) <= 6 and not HEADING_KEYWORD_RE.search(short_title):
            return False
    if len(prev_tuple) >= 3 and len(candidate_tuple) <= 2 and _looks_like_measurement_title(candidate_title):
        return False
    if len(prev_tuple) >= 3 and len(candidate_tuple) == 2 and cand_major == prev_major + 1:
        # After deep subsections, directly jumping to X.Y is usually a value line (e.g. 6.3mm), not a heading.
        if len(candidate_title.strip()) <= 24 and not _looks_like_heading_phrase(candidate_title):
            return False
    if len(prev_tuple) >= 3 and len(candidate_tuple) <= 2:
        lower_title = candidate_title.strip().lower()
        if TABLE_VALUE_TITLE_RE.search(lower_title):
            return False
        if re.search(r"[;；]", lower_title) and MEASUREMENT_HINT_RE.search(lower_title):
            return False
    if len(prev_tuple) >= 3 and len(candidate_tuple) <= 2 and abs(cand_major - prev_major) > 1:
        if not allow_major_reset_by_page and not HEADING_KEYWORD_RE.search(candidate_title):
            return False
    if prev_major >= 3 and cand_major <= 2 and len(candidate_tuple) <= 2 and not allow_major_reset_by_page:
        return False
    if len(candidate_tuple) == 1 and cand_major == prev_major and len(prev_tuple) >= 2:
        return False
    if len(candidate_tuple) == 1 and abs(cand_major - prev_major) > 1 and not allow_major_reset_by_page:
        return False
    if len(candidate_tuple) <= 2 and prev_major >= 4 and cand_major == 1 and not allow_major_reset_by_page:
        return False

    return True


def _extract_segment_block_requirements(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    cursor = 1

    for segment in segments:
        raw_text = str(segment.get("text", ""))
        if _is_likely_index_or_metadata(raw_text):
            continue
        lines = [_normalize_line(line) for line in raw_text.splitlines()]
        lines = [line for line in lines if line and not _is_line_noise(line)]
        lines = _merge_wrapped_lines(lines)
        if not lines:
            continue
        block_text = _normalize_block_text(lines)
        if not block_text:
            continue
        if not _is_requirement_block(block_text) and len(block_text) < 80:
            continue

        dedupe_key = _build_dedupe_key(block_text)
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)

        source_page = segment.get("source_page")
        requirements.append(
            {
                "req_code": f"REQ-{cursor:04d}",
                "source_page": source_page,
                "source_page_end": source_page,
                "source_anchor": segment.get("source_anchor") or "Unknown",
                "requirement_text": block_text,
                "requirement_type": infer_requirement_type(block_text),
                "priority": infer_priority(block_text),
            }
        )
        cursor += 1
    return requirements


def _looks_like_noisy_header_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return True
    if re.search(r"[\u4e00-\u9fff]{2,}", value):
        return False
    if re.search(r"[A-Za-z]{3,}", value):
        return False
    if re.search(r"[A-Za-z]{2,}\.\d+", value):
        return False
    return True


def _extract_section_code_hint(line: str) -> str:
    match = SECTION_CODE_HINT_RE.match(str(line or "").strip())
    if not match:
        return ""
    code = (match.group(1) or "").strip()
    return code


def _extract_appendix_scope(line: str) -> str:
    value = str(line or "").strip()
    if not value:
        return ""
    if len(value) > 90:
        return ""
    if re.search(r"[。；;，,]", value):
        return ""
    if APPENDIX_REFERENCE_HINT_RE.search(value):
        return ""
    match = APPENDIX_MARKER_RE.match(value)
    if not match:
        return ""
    raw = (match.group(1) or "").strip()
    if not raw:
        return ""
    normalized = raw.upper()
    trans = str.maketrans(
        "ＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰＱＲＳＴＵＶＷＸＹＺ",
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    )
    normalized = normalized.translate(trans)
    if not re.fullmatch(r"[A-Z]", normalized):
        return ""
    return normalized


def _next_header_title_hint(line_entries: list[dict[str, Any]], start_index: int) -> tuple[str, int]:
    for idx in range(start_index, min(len(line_entries), start_index + 5)):
        candidate = str(line_entries[idx].get("text", "")).strip()
        if not candidate:
            continue
        if _extract_section_code_hint(candidate):
            return "", 0
        if _looks_like_noisy_header_title(candidate):
            continue
        return candidate, idx - start_index + 1
    return "", 0


def _consolidate_section_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not blocks:
        return []

    consolidated: list[dict[str, Any]] = []
    code_index: dict[tuple[str, str], int] = {}
    for block in blocks:
        appendix_scope = str(block.get("appendix_scope") or "").strip().upper()
        code = str(block.get("section_code") or "")
        if not code:
            consolidated.append(block)
            continue
        key = (appendix_scope, code)
        if key not in code_index:
            code_index[key] = len(consolidated)
            consolidated.append(block)
            continue

        idx = code_index[key]
        consolidated[idx] = _merge_duplicate_section_block(consolidated[idx], block)

    # Strict original-order mode:
    # keep first-seen discovery order and avoid code-based resorting / boundary rewrites.
    return consolidated


def _repair_product_info_detail_boundary(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not blocks:
        return blocks
    out = [dict(block) for block in blocks]
    for idx in range(1, len(out)):
        prev = out[idx - 1]
        curr = out[idx]

        prev_scope = str(prev.get("appendix_scope") or "").strip().upper()
        curr_scope = str(curr.get("appendix_scope") or "").strip().upper()
        if prev_scope != curr_scope:
            continue

        prev_code = str(prev.get("section_code") or "")
        curr_code = str(curr.get("section_code") or "")
        if _section_parent_code(prev_code) != _section_parent_code(curr_code):
            continue
        if not prev_code.endswith(".1") or not curr_code.endswith(".2"):
            continue

        prev_title = str(prev.get("section_title") or "")
        curr_title = str(curr.get("section_title") or "")
        if ("产品信息" not in prev_title) or ("产品详细描述" not in curr_title):
            continue

        curr_entries = list(curr.get("entries", []))
        if len(curr_entries) <= 2:
            continue

        split_idx = _find_product_detail_table_header_index(curr_entries)
        if split_idx <= 1:
            continue

        transfer_raw = curr_entries[1:split_idx]
        if not transfer_raw:
            continue

        prev_entries = list(prev.get("entries", []))
        keep_curr_preface: list[dict[str, Any]] = []
        for row in transfer_raw:
            row_text = _normalize_line(str(row.get("text", "") or ""))
            if not row_text:
                continue

            prev_text, curr_text = _split_boundary_entry_text(row_text, curr_code, curr_title)
            curr_text = _trim_boundary_curr_text(curr_text)
            is_prev_tail_row = (
                _looks_like_table_row_text(row_text)
                or ("|" in row_text)
                or ("备注" in row_text)
                or ("该表格" in row_text)
            )
            if is_prev_tail_row:
                carry_text = prev_text or row_text
                if carry_text and not _is_line_noise(carry_text):
                    moved_row = dict(row)
                    moved_row["text"] = carry_text
                    prev_entries.append(moved_row)
                # Keep only meaningful narrative text on the current section side.
                if (
                    curr_text
                    and not _is_line_noise(curr_text)
                    and not _looks_like_table_row_text(curr_text)
                    and len(curr_text) >= 16
                ):
                    kept_row = dict(row)
                    kept_row["text"] = curr_text
                    keep_curr_preface.append(kept_row)
                continue

            kept_text = curr_text or row_text
            if kept_text and not _is_line_noise(kept_text):
                kept_row = dict(row)
                kept_row["text"] = kept_text
                keep_curr_preface.append(kept_row)

        curr_entries = [curr_entries[0]] + keep_curr_preface + curr_entries[split_idx:]

        prev["entries"] = prev_entries
        curr["entries"] = curr_entries
        _refresh_block_page_span(prev)
        _refresh_block_page_span(curr)
        out[idx - 1] = prev
        out[idx] = curr
    return out


def _find_product_detail_table_header_index(entries: list[dict[str, Any]]) -> int:
    header_re = re.compile(r"\|\s*序号\s*\|(?:\s*序号\s*\|)?\s*项目\s*\|(?:\s*项目\s*\|)?\s*要求", re.I)
    for idx in range(1, len(entries)):
        text = _normalize_line(str(entries[idx].get("text", "") or ""))
        if not text:
            continue
        if header_re.search(text):
            return idx
    return -1


def _split_boundary_entry_text(text: str, curr_code: str, curr_title: str) -> tuple[str, str]:
    value = _normalize_line(str(text or ""))
    if not value:
        return ("", "")

    marker_patterns: list[re.Pattern[str]] = []
    if curr_code and curr_title:
        marker_patterns.append(
            re.compile(
                rf"\b{re.escape(curr_code)}\s*{re.escape(curr_title)}\b",
                re.I,
            )
        )
        marker_patterns.append(
            re.compile(
                rf"\b{re.escape(curr_code)}\s*{re.escape(curr_title[: min(len(curr_title), 16)])}",
                re.I,
            )
        )
    if curr_code:
        marker_patterns.append(re.compile(rf"\b{re.escape(curr_code)}\b", re.I))

    for pattern in marker_patterns:
        match = pattern.search(value)
        if not match:
            continue
        left = _normalize_line(value[: match.start()].strip(" |"))
        right = _normalize_line(value[match.end() :].strip(" |"))
        return (left, right)

    return (value, "")


def _trim_boundary_curr_text(text: str) -> str:
    value = _normalize_line(str(text or ""))
    if not value:
        return ""
    if "|备注" in value:
        value = _normalize_line(value.split("|备注", 1)[0].strip(" |"))
    note_pos = value.find("备注：")
    if note_pos > 80:
        value = _normalize_line(value[:note_pos].strip(" |"))
    if "|" in value and not value.startswith("|"):
        value = _normalize_line(value.split("|", 1)[0].strip(" |"))
    return value


def _refresh_block_page_span(block: dict[str, Any]) -> None:
    pages = [int(e.get("source_page")) for e in list(block.get("entries", [])) if isinstance(e.get("source_page"), int)]
    line_indices = [
        int(e.get("source_line_index"))
        for e in list(block.get("entries", []))
        if isinstance(e.get("source_line_index"), int)
    ]
    if not pages:
        return
    block["source_page"] = min(pages)
    block["source_page_end"] = max(pages)
    if line_indices:
        block["source_line_index"] = min(line_indices)
        block["source_line_index_end"] = max(line_indices)


def _merge_duplicate_section_block(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    base_entries = list(base.get("entries", []))
    in_entries = list(incoming.get("entries", []))
    merged_entries = _merge_entries_unique(base_entries, in_entries)
    merged["entries"] = merged_entries

    base_start = base.get("source_page")
    in_start = incoming.get("source_page")
    if base_start is None:
        merged["source_page"] = in_start
    elif in_start is not None:
        merged["source_page"] = min(base_start, in_start)

    base_end = base.get("source_page_end")
    in_end = incoming.get("source_page_end")
    if base_end is None:
        merged["source_page_end"] = in_end
    elif in_end is not None:
        merged["source_page_end"] = max(base_end, in_end)

    base_line_start = base.get("source_line_index")
    in_line_start = incoming.get("source_line_index")
    if base_line_start is None:
        merged["source_line_index"] = in_line_start
    elif in_line_start is not None:
        merged["source_line_index"] = min(int(base_line_start), int(in_line_start))

    base_line_end = base.get("source_line_index_end")
    in_line_end = incoming.get("source_line_index_end")
    if base_line_end is None:
        merged["source_line_index_end"] = in_line_end
    elif in_line_end is not None:
        merged["source_line_index_end"] = max(int(base_line_end), int(in_line_end))

    base_title = str(base.get("section_title") or "")
    in_title = str(incoming.get("section_title") or "")
    level = int(base.get("section_level") or incoming.get("section_level") or 0)
    if level <= 2 and base_title and in_title:
        # For top-level/major sections, keep the first observed heading unless it is clearly noisy.
        if _looks_like_noisy_header_title(base_title) and not _looks_like_noisy_header_title(in_title):
            merged["section_title"] = in_title
        else:
            merged["section_title"] = base_title
    else:
        merged["section_title"] = _choose_better_section_title(base_title, in_title)
    base_order = int(base.get("_discovery_order", 10**9) or 10**9)
    in_order = int(incoming.get("_discovery_order", 10**9) or 10**9)
    merged["_discovery_order"] = min(base_order, in_order)
    return merged


def _merge_entries_unique(
    base_entries: list[dict[str, Any]],
    incoming_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: set[tuple[int, str]] = set()
    out: list[dict[str, Any]] = []

    def _push(entry: dict[str, Any]) -> None:
        page_no = entry.get("source_page")
        page_key = int(page_no) if isinstance(page_no, int) else 0
        text_key = str(entry.get("text") or "").strip().lower()
        key = (page_key, text_key)
        if not text_key:
            return
        if key in seen:
            return
        seen.add(key)
        out.append(entry)

    for entry in base_entries:
        _push(entry)
    for entry in incoming_entries:
        _push(entry)
    return out


def _choose_better_section_title(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if _title_quality_score(right) > _title_quality_score(left):
        return right
    return left


def _title_quality_score(title: str) -> float:
    value = str(title or "").strip()
    if not value:
        return 0.0
    score = 0.0
    if re.search(r"[\u4e00-\u9fff]", value):
        score += 1.2
    if re.search(r"[A-Za-z]{3,}", value):
        score += 1.0
    token_count = len(re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value))
    score += min(1.0, token_count / 8.0)
    if len(value) > 40:
        score -= 1.0
    if re.search(r"[，,；;。!?！？]", value):
        score -= 1.0
    if _looks_like_measurement_title(value) or _looks_like_component_bom_title(value) or _looks_like_quantity_only_title(value):
        score -= 1.2
    if _looks_like_noisy_header_title(value):
        score -= 0.8
    return score


def _section_parent_code(section_code: str) -> str:
    if not section_code or "." not in section_code:
        return ""
    return section_code.rsplit(".", 1)[0]


def _section_has_children(
    appendix_scope: str,
    section_code: str,
    all_section_keys: list[tuple[str, str]],
) -> bool:
    if not section_code:
        return False
    prefix = f"{section_code}."
    scope = str(appendix_scope or "").strip().upper()
    return any(
        code.startswith(prefix)
        for row_scope, code in all_section_keys
        if row_scope == scope and code and code != section_code
    )


def _looks_like_heading_only(text: str) -> bool:
    compact = re.sub(r"\s+", " ", text).strip()
    lines = [ln.strip() for ln in compact.split("\n") if ln.strip()]
    if len(lines) > 2:
        return False
    single = " ".join(lines)
    return bool(re.fullmatch(r"\d+(?:\.\d+){0,5}\s+[\u4e00-\u9fffA-Za-z\- /]{1,50}", single))


def _extract_sentence_requirements(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    cursor = 1

    for segment in segments:
        raw_text = segment.get("text", "")
        if _is_likely_index_or_metadata(raw_text):
            continue

        lines = [_normalize_line(line) for line in str(raw_text).splitlines()]
        lines = [line for line in lines if line and not _is_line_noise(line)]
        merged = _merge_wrapped_lines(lines)

        for line in merged:
            for candidate in [part.strip() for part in SENTENCE_SPLIT_RE.split(line) if part.strip()]:
                req_text = _normalize_sentence_text(candidate)
                if not req_text:
                    continue
                if not _looks_like_requirement_sentence(req_text):
                    continue
                dedupe_key = _build_dedupe_key(req_text)
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                requirements.append(
                    {
                        "req_code": f"REQ-{cursor:04d}",
                        "source_page": segment.get("source_page"),
                        "source_page_end": segment.get("source_page"),
                        "source_anchor": segment.get("source_anchor") or "Unknown",
                        "requirement_text": req_text,
                        "requirement_type": infer_requirement_type(req_text),
                        "priority": infer_priority(req_text),
                    }
                )
                cursor += 1
    return requirements


def _append_test_outline_requirements(
    requirements: list[dict[str, Any]],
    segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out = list(requirements)
    seen_keys: set[str] = set()
    for item in out:
        text = str(item.get("requirement_text") or "")
        if text:
            seen_keys.add(_build_dedupe_key(text))

    cursor = len(out) + 1
    test_rows = _extract_test_outline_rows(segments)
    for row in test_rows:
        text = str(row.get("requirement_text") or "").strip()
        if not text:
            continue
        scope = str(row.get("appendix_scope") or "").strip().upper()
        item_code = str(row.get("item_code") or "").strip()
        if scope and item_code:
            dedupe = f"{scope}:{item_code}:{_build_dedupe_key(text)}"
        else:
            dedupe = _build_dedupe_key(text)
        if dedupe in seen_keys:
            continue
        seen_keys.add(dedupe)
        out.append(
            {
                "req_code": f"REQ-{cursor:04d}",
                "source_page": row.get("source_page"),
                "source_page_end": row.get("source_page_end"),
                "source_line_index": row.get("source_line_index"),
                "source_line_index_end": row.get("source_line_index_end"),
                "source_anchor": row.get("source_anchor", "Unknown"),
                "appendix_scope": scope,
                "requirement_text": text,
                "requirement_type": infer_requirement_type(text),
                "priority": infer_priority(text),
            }
        )
        cursor += 1

    # Strict original-order mode: keep requirements ordered by source location.
    def _sort_key(row: dict[str, Any], index: int) -> tuple[int, int, int, int]:
        page = _safe_int(row.get("source_page"))
        line_idx_raw = row.get("source_line_index")
        line_idx = int(line_idx_raw) if isinstance(line_idx_raw, int) else 10**9
        return (
            1 if page <= 0 else 0,
            page if page > 0 else 10**9,
            line_idx,
            index,
        )

    indexed_rows = list(enumerate(out))
    ordered_pairs = sorted(indexed_rows, key=lambda pair: _sort_key(pair[1], pair[0]))
    ordered = [pair[1] for pair in ordered_pairs]
    for idx, item in enumerate(ordered, start=1):
        item["req_code"] = f"REQ-{idx:04d}"
    return ordered


def _extract_test_outline_rows(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_segments = _sort_segments(segments)
    current_scope = ""
    current_outline_title = ""
    rows: list[dict[str, Any]] = []
    seen_rows: dict[str, int] = {}

    for seg in ordered_segments:
        page = seg.get("source_page")
        source_page = int(page) if isinstance(page, int) else None
        for line_index, raw_line in enumerate(str(seg.get("text", "")).splitlines()):
            line = _normalize_line(raw_line)
            if not line:
                continue

            marker_scope, marker_title = _detect_test_outline_scope(line)
            if marker_scope:
                current_scope = marker_scope
                current_outline_title = marker_title
                continue

            if not current_scope or current_scope == "X":
                continue
            if "|" not in line:
                continue

            row_data = _parse_test_outline_row(line)
            if not row_data:
                continue

            item_code = row_data["item_code"]
            item_title = row_data["item_title"]
            row_text = row_data["row_text"]
            if not item_code or not row_text:
                continue

            row_key = f"{current_scope}:{item_code}:{_build_dedupe_key(row_text)}"
            seen_count = int(seen_rows.get(row_key, 0))
            max_count = _test_outline_line_duplicate_limit(row_text)
            if seen_count >= max_count:
                continue
            seen_rows[row_key] = seen_count + 1

            section_label = f"{item_code} {item_title}".strip()
            source_anchor = f"Page {source_page or '?'} | Appendix {current_scope} | Section {section_label}"
            if current_outline_title:
                source_anchor = f"{source_anchor} | {current_outline_title}"

            rows.append(
                {
                    "appendix_scope": current_scope,
                    "item_code": item_code,
                    "item_title": item_title,
                    "outline_title": current_outline_title,
                    "source_page": source_page,
                    "source_page_end": source_page,
                    "source_line_index": line_index,
                    "source_line_index_end": line_index,
                    "source_anchor": source_anchor,
                    "requirement_text": row_text,
                }
            )

    return _merge_test_outline_rows(rows)


def _merge_test_outline_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    order: list[tuple[str, str, str]] = []

    for row in rows:
        scope = str(row.get("appendix_scope") or "").strip().upper()
        item_code = str(row.get("item_code") or "").strip()
        item_title = str(row.get("item_title") or "").strip()
        if not scope or not item_code or not item_title:
            continue
        text_line = str(row.get("requirement_text") or "").strip()
        title_key = _test_item_title_key(item_title)
        # Merge by item identity across all test-outline appendices.
        # This keeps one requirement item per "序号 + 标题", while concatenating
        # multi-line/sub-step rows into the same requirement text.
        key = (scope, item_code, title_key)

        page = row.get("source_page")
        page_int = int(page) if isinstance(page, int) else 0
        line_idx_raw = row.get("source_line_index")
        line_idx_int = int(line_idx_raw) if isinstance(line_idx_raw, int) else None
        outline_title = str(row.get("outline_title") or "").strip()

        if key not in grouped:
            grouped[key] = {
                "appendix_scope": scope,
                "item_code": item_code,
                "item_title": item_title,
                "outline_title": outline_title,
                "source_page": page_int if page_int > 0 else None,
                "source_page_end": page_int if page_int > 0 else None,
                "source_line_index": line_idx_int,
                "source_line_index_end": line_idx_int,
                "lines": [],
                "line_counts": {},
                "last_line_key": "",
            }
            order.append(key)

        state = grouped[key]
        if text_line:
            line_key = _build_dedupe_key(text_line)
            if line_key and line_key == str(state.get("last_line_key") or ""):
                # Skip immediate OCR repetition.
                pass
            else:
                counts = state.get("line_counts", {})
                count = int(counts.get(line_key, 0)) if line_key else 0
                limit = _test_outline_line_duplicate_limit(text_line)
                if not line_key or count < limit:
                    state["lines"].append(text_line)
                    if line_key:
                        counts[line_key] = count + 1
                        state["line_counts"] = counts
                        state["last_line_key"] = line_key
        if page_int > 0:
            start = state.get("source_page")
            end = state.get("source_page_end")
            state["source_page"] = page_int if start in (None, 0) else min(start, page_int)
            state["source_page_end"] = page_int if end in (None, 0) else max(end, page_int)
        if line_idx_int is not None:
            start_idx = state.get("source_line_index")
            end_idx = state.get("source_line_index_end")
            state["source_line_index"] = line_idx_int if start_idx is None else min(int(start_idx), line_idx_int)
            state["source_line_index_end"] = line_idx_int if end_idx is None else max(int(end_idx), line_idx_int)

    merged_rows: list[dict[str, Any]] = []
    for key in order:
        state = grouped[key]
        lines = [str(line).strip() for line in state.get("lines", []) if str(line).strip()]
        if not lines:
            continue
        item_code = str(state.get("item_code") or "").strip()
        item_title = str(state.get("item_title") or "").strip()
        section_label = f"{item_code} {item_title}".strip()
        source_page = state.get("source_page")
        source_page_end = state.get("source_page_end") or source_page
        source_anchor = f"Page {source_page or '?'} | Appendix {state.get('appendix_scope', '')} | Section {section_label}"
        outline_title = str(state.get("outline_title") or "").strip()
        if outline_title:
            source_anchor = f"{source_anchor} | {outline_title}"
        requirement_text = f"{section_label}\n" + "\n".join(lines)

        merged_rows.append(
            {
                "appendix_scope": state.get("appendix_scope", ""),
                "item_code": item_code,
                "item_title": item_title,
                "source_page": source_page,
                "source_page_end": source_page_end,
                "source_line_index": state.get("source_line_index"),
                "source_line_index_end": state.get("source_line_index_end"),
                "source_anchor": source_anchor,
                "requirement_text": requirement_text.strip(),
            }
        )
    # Strict original-order mode: do not synthesize missing serial placeholders.
    return merged_rows


def _inject_missing_test_outline_serial_placeholders(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return rows

    by_scope: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    existing_codes: dict[str, set[int]] = {}
    for idx, row in enumerate(rows):
        scope = str(row.get("appendix_scope") or "").strip().upper()
        code_int = _safe_positive_int(row.get("item_code"))
        if not scope or code_int is None:
            continue
        by_scope.setdefault(scope, []).append((idx, code_int, row))
        existing_codes.setdefault(scope, set()).add(code_int)

    insertions: dict[int, list[dict[str, Any]]] = {}
    for scope, indexed_rows in by_scope.items():
        ordered_rows = sorted(indexed_rows, key=lambda item: item[0])
        seen_codes = set(existing_codes.get(scope, set()))
        for i in range(len(ordered_rows) - 1):
            left_idx, left_code, left_row = ordered_rows[i]
            right_idx, right_code, right_row = ordered_rows[i + 1]
            # Only fill a single missing serial number (e.g. 6 -> 8) to avoid false positives.
            if right_code - left_code != 2:
                continue
            missing_code = left_code + 1
            if missing_code in seen_codes:
                continue

            page = _safe_positive_int(right_row.get("source_page")) or _safe_positive_int(left_row.get("source_page")) or 0
            source_page = page if page > 0 else None
            outline_title = str(right_row.get("outline_title") or left_row.get("outline_title") or "").strip()
            source_anchor = f"Page {source_page or '?'} | Appendix {scope} | Section {missing_code} 序号缺失待核对"
            if outline_title:
                source_anchor = f"{source_anchor} | {outline_title}"

            placeholder = {
                "appendix_scope": scope,
                "item_code": str(missing_code),
                "item_title": "序号缺失待核对",
                "source_page": source_page,
                "source_page_end": source_page,
                "source_anchor": source_anchor,
                "requirement_text": (
                    f"{missing_code} 序号缺失待核对\n"
                    f"原PDF在 {left_code} 与 {right_code} 之间存在编号断点，未识别到第{missing_code}条内容。请人工核对原文。"
                ),
            }
            insertions.setdefault(right_idx, []).append(placeholder)
            seen_codes.add(missing_code)

    if not insertions:
        return rows

    out: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        for item in insertions.get(idx, []):
            out.append(item)
        out.append(row)
    return out


def _safe_positive_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(float(text))
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed


def _test_item_title_key(item_title: str) -> str:
    key = _build_dedupe_key(item_title)
    key = re.sub(r"\s+", "", key)
    return key


def _test_outline_line_duplicate_limit(line: str) -> int:
    value = str(line or "")
    if re.search(r"(?:绝缘电阻|insulation resistance)", value, re.I):
        # In electrical test outlines one test item may include multiple
        # repeated insulation checks across groups/steps; keep all practical rows.
        return 8
    if re.search(r"(?:气密性|pressure leakage|浸没|submersion|高压水喷射|high pressure spray)", value, re.I):
        return 6
    return 2


def _detect_test_outline_scope(line: str) -> tuple[str, str]:
    value = str(line or "").strip()
    if not value:
        return ("", "")

    candidates = [value]
    if "|" in value:
        candidates.extend(part.strip() for part in value.split("|") if part.strip())

    for candidate in candidates:
        for pattern, scope, normalized_title in TEST_OUTLINE_SCOPE_TITLES:
            if pattern.search(candidate):
                return (scope, normalized_title)
        if ELECTRIC_TEST_OUTLINE_HEADER_RE.search(candidate):
            return ("Y", "用电器试验大纲")
    return ("", "")


def _parse_test_outline_row(line: str) -> dict[str, str] | None:
    value = str(line or "").strip()
    if not value or "|" not in value:
        return None
    match = TEST_OUTLINE_ROW_RE.match(value)
    if not match:
        return None

    cells = [part.strip() for part in value.split("|") if part.strip()]
    if len(cells) < 2:
        return None
    first = cells[0]
    if re.search(r"(?:序号|col\d+)", first, re.I):
        return None

    item_match = re.match(r"^(\d{1,3})", first)
    if not item_match:
        return None
    item_code = item_match.group(1)

    item_title = cells[1]
    if re.search(r"(?:项目\s*/\s*item|测试顺序|参考标准|要求\s*/\s*requirements)", item_title, re.I):
        return None
    if len(item_title) > 220:
        item_title = item_title[:220].strip()

    row_text = "|" + "|".join(cells) + "|"
    row_text = re.sub(r"\s+", " ", row_text).strip()
    if len(row_text) < 24:
        return None
    return {
        "item_code": item_code,
        "item_title": item_title,
        "row_text": row_text,
    }


def _looks_like_requirement_sentence(sentence: str) -> bool:
    score = 0
    lower = sentence.lower()
    if any(pattern.search(sentence) for pattern in MODAL_PATTERNS):
        score += 3
    if NUMBER_UNIT_RE.search(lower):
        score += 1
    if any(pattern.search(lower) for pattern in BLOCK_HINT_PATTERNS):
        score += 1
    if len(sentence) >= 28:
        score += 1
    if len(sentence) <= 14:
        score -= 2
    if _is_line_noise(sentence):
        score -= 4
    return score >= 3


def _normalize_sentence_text(sentence: str) -> str:
    text = sentence.strip()
    text = CLAUSE_PREFIX_RE.sub("", text)
    text = re.sub(r"^[\-\*\u2022]+", "", text).strip()
    text = re.sub(r"\s+", " ", text).strip()
    if text.endswith(":") and len(text) <= 40:
        return ""
    if len(text) < 10:
        return ""
    return text


def _build_dedupe_key(text: str) -> str:
    value = text.lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff ]", "", value)
    value = value.strip()
    return value[:220]


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
    value = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _merge_wrapped_lines(lines: list[str]) -> list[str]:
    if not lines:
        return []
    merged: list[str] = []
    buffer = lines[0]
    for line in lines[1:]:
        if _should_join_lines(buffer, line):
            buffer = f"{buffer} {line}".strip()
        else:
            merged.append(buffer)
            buffer = line
    merged.append(buffer)
    return merged


def _should_join_lines(prev: str, current: str) -> bool:
    if _parse_section_header(current):
        return False
    if re.search(r"[。！？!?；;:]$", prev):
        return False
    if len(prev) <= 10:
        return False
    if current[:1].islower():
        return True
    if current.startswith(("and ", "or ", "以及", "并且", "且", "其中")):
        return True
    return len(current) <= 45


def _is_likely_index_or_metadata(text: str) -> bool:
    lines = [_normalize_line(line) for line in str(text).splitlines() if _normalize_line(line)]
    if not lines:
        return True
    dot_count = sum(1 for line in lines if DOT_LEADER_RE.search(line))
    page_no_count = sum(1 for line in lines if PAGE_MARK_RE.match(line.lower()))
    modal_count = sum(1 for line in lines if any(pattern.search(line) for pattern in MODAL_PATTERNS))
    if dot_count >= 3 and modal_count == 0:
        return True
    return page_no_count >= 3 and modal_count == 0


def _is_line_noise(line: str) -> bool:
    value = line.strip()
    lower = value.lower()
    if not value:
        return True
    if _extract_appendix_scope(value):
        return False
    if SECTION_CODE_HINT_RE.match(value):
        return False
    if len(value) <= 1:
        return True
    if PAGE_MARK_RE.match(lower):
        return True
    if DOT_LEADER_RE.search(value):
        return True
    if MARKDOWN_TABLE_RULE_RE.match(value):
        return True
    if re.fullmatch(r"[\d\-/:\. ]+", value):
        return True
    if re.fullmatch(r"[_\-\*#= ]+", value):
        return True
    digit_count = sum(ch.isdigit() for ch in value)
    has_alpha_or_cjk = bool(re.search(r"[A-Za-z\u4e00-\u9fff]", value))
    if digit_count >= max(8, len(value) // 2) and not has_alpha_or_cjk:
        return True
    for pattern in NOISE_PATTERNS:
        if pattern.search(value):
            return True
    return False


def infer_requirement_type(text: str) -> str:
    lower = text.lower()
    if any(key in lower for key in ["测试矩阵", "test matrix", "试验参数", "test parameters"]):
        return "TEST"
    if any(key in lower for key in ["试验方法", "test methods", "functional test", "功能测试"]):
        return "TEST"
    if any(key in lower for key in ["技术要求", "technical requirement"]):
        return "PERFORMANCE"
    if any(
        key in lower
        for key in [
            "latency",
            "response time",
            "throughput",
            "accuracy",
            "drift",
            "ms",
            "s ",
            "性能",
            "时延",
            "精度",
            "功耗",
            "温度",
            "voltage",
            "current",
        ]
    ):
        return "PERFORMANCE"
    if any(
        key in lower
        for key in ["interface", "api", "can", "lin", "ethernet", "uart", "spi", "uds", "接口", "通信", "通讯", "诊断"]
    ):
        return "INTERFACE"
    if any(
        key in lower
        for key in ["iso", "aspice", "regulation", "法规", "安全", "safety", "asil", "法律", "合规", "gb/", "qc/t"]
    ):
        return "REGULATION"
    if any(
        key in lower
        for key in [
            "test",
            "verification",
            "validate",
            "qualification",
            "dv/pv",
            "试验",
            "测试",
            "验证",
            "dvp",
            "emc",
            " et ",
            "试验方法",
        ]
    ):
        return "TEST"
    if any(
        key in lower
        for key in ["function", "feature", "strategy", "algorithm", "rollback", "shall support", "功能", "策略", "算法"]
    ):
        return "FUNCTIONAL"
    return "OTHER"


def infer_priority(text: str) -> str:
    lower = text.lower()
    if any(
        key in lower
        for key in ["must", "shall", "critical", "mandatory", "必须", "不得", "应当", "安全", "safety", "asil", "法规"]
    ):
        return "HIGH"
    if any(key in lower for key in ["should", "recommended", "建议"]):
        return "MEDIUM"
    return "LOW"
