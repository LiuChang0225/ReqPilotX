from __future__ import annotations

import re
from typing import Any


MAIN_SCOPE_ID = "MAIN"
MAIN_SCOPE_TITLE = "正文（Main）"
APPENDIX_EVIDENCE_EXPLICIT = "explicit_marker"
APPENDIX_EVIDENCE_TOC = "toc_derived"
APPENDIX_EVIDENCE_HEADING = "heading_derived"
APPENDIX_EVIDENCE_TABLE_HEADER = "table_header_derived"
TEST_OUTLINE_SCOPE_FRONT_DAMPER = "主动前减振器试验大纲"
TEST_OUTLINE_SCOPE_REAR_DAMPER = "主动后减振器试验大纲"
TEST_OUTLINE_SCOPE_ACCUMULATOR = "蓄能器试验大纲"
TEST_OUTLINE_SCOPE_VEHICLE_WIRING = "整车用电器与线束匹配评估表"
TEST_OUTLINE_SCOPE_GROUPS = "测试分组 / Test Groups（推荐）"
TEST_OUTLINE_SCOPE_ELECTRIC = "用电器试验大纲"
TEST_OUTLINE_INFERRED_GENERIC_TITLE = "试验大纲"
TEST_OUTLINE_SCOPE_TITLES = (
    TEST_OUTLINE_SCOPE_FRONT_DAMPER,
    TEST_OUTLINE_SCOPE_REAR_DAMPER,
    TEST_OUTLINE_SCOPE_ACCUMULATOR,
    TEST_OUTLINE_SCOPE_VEHICLE_WIRING,
    TEST_OUTLINE_SCOPE_GROUPS,
    TEST_OUTLINE_SCOPE_ELECTRIC,
)
LEGACY_TEST_OUTLINE_SCOPE_ALIASES = {
    "T": TEST_OUTLINE_SCOPE_FRONT_DAMPER,
    "U": TEST_OUTLINE_SCOPE_REAR_DAMPER,
    "V": TEST_OUTLINE_SCOPE_ACCUMULATOR,
    "W": TEST_OUTLINE_SCOPE_VEHICLE_WIRING,
    "X": TEST_OUTLINE_SCOPE_GROUPS,
    "Y": TEST_OUTLINE_SCOPE_ELECTRIC,
}
_TEST_OUTLINE_SCOPE_IDENTITIES = {
    re.sub(r"\s+", " ", title).strip().upper() for title in TEST_OUTLINE_SCOPE_TITLES
}
_APPENDIX_EVIDENCE_RANK = {
    APPENDIX_EVIDENCE_EXPLICIT: 4,
    APPENDIX_EVIDENCE_TOC: 3,
    APPENDIX_EVIDENCE_HEADING: 2,
    APPENDIX_EVIDENCE_TABLE_HEADER: 1,
}
_GENERIC_TEST_OUTLINE_TITLE_RE = re.compile(
    r"(?:试验大纲|匹配评估表|测试分组)",
    re.I,
)
_GENERIC_TEST_OUTLINE_EXCLUDE_RE = re.compile(
    r"(?:交付|提交|清单|计划|能力|评审|报审|报告|文件|BOM|初版|阶段)",
    re.I,
)


def normalize_scope_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def scope_evidence_rank(value: str) -> int:
    return int(_APPENDIX_EVIDENCE_RANK.get(str(value or "").strip(), 0))


def normalize_scope_id(scope: str) -> str:
    value = normalize_scope_text(scope).upper()
    if not value:
        return MAIN_SCOPE_ID
    return value


def normalize_test_outline_scope(scope: str) -> str:
    value = normalize_scope_text(scope)
    if not value:
        return ""
    return LEGACY_TEST_OUTLINE_SCOPE_ALIASES.get(value.upper(), value)


def test_outline_scope_identity(scope: str) -> str:
    value = normalize_test_outline_scope(scope)
    return re.sub(r"\s+", " ", value).strip().upper()


def is_reference_only_test_outline_scope(scope: str) -> bool:
    return test_outline_scope_identity(scope) == test_outline_scope_identity(TEST_OUTLINE_SCOPE_GROUPS)


def is_test_outline_scope_value(scope: str) -> bool:
    identity = test_outline_scope_identity(scope)
    if identity in _TEST_OUTLINE_SCOPE_IDENTITIES:
        return True
    value = normalize_scope_text(scope)
    if not value:
        return False
    return bool(re.search(r"(?:试验大纲|匹配评估表|测试分组)", value, re.I))


def infer_generic_test_outline_title(value: str, *, allow_table_cell: bool = False) -> str:
    normalized = normalize_scope_text(value)
    if not normalized:
        return ""
    if len(normalized) > 80:
        return ""
    if not allow_table_cell and "|" in normalized:
        return ""
    if re.search(r"[。！？!?；;]", normalized):
        return ""
    if re.match(r"^\d+(?:[\s、.)）]|$)", normalized):
        return ""
    if _GENERIC_TEST_OUTLINE_EXCLUDE_RE.search(normalized):
        return ""
    if not _GENERIC_TEST_OUTLINE_TITLE_RE.search(normalized):
        return ""
    return normalized


def is_synthetic_scope(scope: str) -> bool:
    return bool(re.fullmatch(r"SUB\d+", normalize_scope_id(scope)))


def scope_sort_key(scope: str) -> tuple[int, int, str]:
    normalized = normalize_scope_id(scope)
    if normalized == MAIN_SCOPE_ID:
        return (0, 0, "")
    sub_match = re.fullmatch(r"SUB(\d+)", normalized)
    if sub_match:
        return (1, int(sub_match.group(1)), "")
    if re.fullmatch(r"[A-Z]", normalized):
        return (2, ord(normalized) - ord("A"), "")
    return (3, 0, normalized)