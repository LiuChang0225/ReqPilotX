from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
PROTOTYPE_DIR = APP_DIR.parent
ITERATIONS_DIR = PROTOTYPE_DIR / "eval" / "iterations"
BOARD_CSV = ITERATIONS_DIR / "phase0_iteration_board.csv"
SECTION_RE = re.compile(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})", re.I)
SPLIT_FAIL_VALUES = {"0", "false", "n", "no"}
TAG_SPLIT_RE = re.compile(r"[,;，；\|/]+")

TAG_ACTIONS = {
    "table_value_as_section": "加强表格值误识别抑制规则（数值+单位+上下文）。",
    "header_footer_leak": "增强页眉页脚重复行检测并在解析层前置去除。",
    "watermark_noise": "增加水印碎片词典与行内单字符清洗策略。",
    "cross_page_heading": "强化跨页标题合并（页尾章节号+次页标题）。",
    "missing_section": "补充章节注入与层级一致性回补逻辑。",
    "over_split": "提高完整块提取权重，降低句子级切分优先级。",
    "under_split": "补充分节命中规则，避免大段粘连。",
}


def list_iteration_rounds(limit: int = 40) -> list[dict[str, Any]]:
    rows = _load_iteration_board_rows()
    output: list[dict[str, Any]] = []
    for row in rows:
        round_id = str(row.get("round_id") or "").strip()
        if not round_id:
            continue
        output.append(
            {
                "round_id": round_id,
                "timestamp": str(row.get("timestamp") or ""),
                "split_accuracy_pct": _to_float(row.get("split_accuracy_pct")),
                "parse_coverage_pct": _to_float(row.get("parse_coverage_pct")),
                "labeled_predicted_items": _to_int(row.get("labeled_predicted_items")),
            }
        )
    output.sort(key=lambda item: str(item.get("timestamp") or ""))
    if limit > 0:
        output = output[-limit:]
    output.reverse()
    return output


def build_round_diff(round_a: str, round_b: str, limit: int = 160) -> dict[str, Any]:
    data_a = _load_round_predicted_items(round_a)
    data_b = _load_round_predicted_items(round_b)

    map_a = {item["fingerprint"]: item for item in data_a}
    map_b = {item["fingerprint"]: item for item in data_b}
    keys_a = set(map_a.keys())
    keys_b = set(map_b.keys())

    added_keys = sorted(keys_b - keys_a)
    removed_keys = sorted(keys_a - keys_b)

    added = [map_b[key] for key in added_keys[:limit]]
    removed = [map_a[key] for key in removed_keys[:limit]]

    by_doc_added: dict[str, int] = {}
    by_doc_removed: dict[str, int] = {}
    by_section_added: dict[str, int] = {}
    by_section_removed: dict[str, int] = {}

    for key in added_keys:
        row = map_b[key]
        doc = row.get("file_name") or "Unknown"
        section = row.get("section_code") or ""
        by_doc_added[doc] = by_doc_added.get(doc, 0) + 1
        if section:
            by_section_added[section] = by_section_added.get(section, 0) + 1

    for key in removed_keys:
        row = map_a[key]
        doc = row.get("file_name") or "Unknown"
        section = row.get("section_code") or ""
        by_doc_removed[doc] = by_doc_removed.get(doc, 0) + 1
        if section:
            by_section_removed[section] = by_section_removed.get(section, 0) + 1

    doc_changes = _merge_change_counts(by_doc_added, by_doc_removed)
    section_changes = _merge_change_counts(by_section_added, by_section_removed)

    return {
        "round_a": round_a,
        "round_b": round_b,
        "summary": {
            "items_a": len(data_a),
            "items_b": len(data_b),
            "added_count": len(added_keys),
            "removed_count": len(removed_keys),
            "unchanged_count": len(keys_a & keys_b),
        },
        "by_doc": doc_changes[:80],
        "by_section": section_changes[:120],
        "added": [_shape_diff_row(row) for row in added],
        "removed": [_shape_diff_row(row) for row in removed],
    }


def build_error_closure(round_id: str, top_n: int = 20) -> dict[str, Any]:
    rows = _load_round_item_rows(round_id)
    predicted_rows = [row for row in rows if str(row.get("source_type") or "") == "predicted"]
    labeled_rows = [row for row in predicted_rows if _is_binary_label(row.get("is_split_correct"))]
    fail_rows = [row for row in labeled_rows if _is_split_fail(row.get("is_split_correct"))]

    tag_counter: dict[str, int] = {}
    for row in fail_rows:
        tags = _split_error_tags(str(row.get("error_tags") or ""))
        if not tags:
            tags = ["unlabeled_reason"]
        for tag in tags:
            tag_counter[tag] = tag_counter.get(tag, 0) + 1

    if not tag_counter:
        for row in labeled_rows:
            tags = _split_error_tags(str(row.get("error_tags") or ""))
            for tag in tags:
                tag_counter[tag] = tag_counter.get(tag, 0) + 1

    top_tags = sorted(tag_counter.items(), key=lambda kv: kv[1], reverse=True)[:max(1, top_n)]
    actions = [
        {
            "tag": tag,
            "count": count,
            "suggested_action": _suggest_action(tag),
        }
        for tag, count in top_tags
    ]

    return {
        "round_id": round_id,
        "summary": {
            "predicted_rows": len(predicted_rows),
            "labeled_rows": len(labeled_rows),
            "split_fail_rows": len(fail_rows),
            "unlabeled_rows": max(0, len(predicted_rows) - len(labeled_rows)),
            "top_tag_count": len(actions),
        },
        "actions": actions,
    }


def load_high_risk_queue(round_id: str, limit: int = 80) -> dict[str, Any]:
    round_dir = _round_dir(round_id)
    queue_csv = round_dir / "phase0_split_active_queue.csv"
    if not queue_csv.exists():
        return {"round_id": round_id, "summary": {"rows": 0}, "rows": []}

    rows = _read_csv(queue_csv)
    shaped = [
        {
            "rank": _to_int(row.get("rank")),
            "item_id": str(row.get("item_id") or ""),
            "file_name": str(row.get("file_name") or ""),
            "section_code": str(row.get("section_code") or ""),
            "source_page_start": _to_int(row.get("source_page_start")),
            "source_page_end": _to_int(row.get("source_page_end")),
            "risk_score": _to_float(row.get("risk_score")),
            "risk_reasons": str(row.get("risk_reasons") or ""),
            "text_preview": _truncate(str(row.get("auto_requirement_text") or ""), 220),
        }
        for row in rows[: max(1, limit)]
    ]
    return {
        "round_id": round_id,
        "summary": {
            "rows": len(rows),
            "returned_rows": len(shaped),
        },
        "rows": shaped,
    }


def _shape_diff_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": row.get("item_id", ""),
        "file_name": row.get("file_name", ""),
        "section_code": row.get("section_code", ""),
        "source_page_start": row.get("source_page_start", 0),
        "source_page_end": row.get("source_page_end", 0),
        "text_preview": _truncate(str(row.get("auto_requirement_text") or ""), 240),
    }


def _merge_change_counts(added: dict[str, int], removed: dict[str, int]) -> list[dict[str, Any]]:
    keys = set(added.keys()) | set(removed.keys())
    rows = [
        {
            "key": key,
            "added": added.get(key, 0),
            "removed": removed.get(key, 0),
            "delta": added.get(key, 0) - removed.get(key, 0),
        }
        for key in keys
    ]
    rows.sort(key=lambda row: (abs(int(row.get("delta", 0))), int(row.get("added", 0)) + int(row.get("removed", 0))), reverse=True)
    return rows


def _load_round_predicted_items(round_id: str) -> list[dict[str, Any]]:
    rows = _load_round_item_rows(round_id)
    output: list[dict[str, Any]] = []
    for row in rows:
        if str(row.get("source_type") or "") != "predicted":
            continue
        text = str(row.get("auto_requirement_text") or "")
        if not text.strip():
            continue
        section_code = _extract_section_code(str(row.get("source_anchor") or ""))
        output.append(
            {
                "item_id": str(row.get("item_id") or ""),
                "file_name": str(row.get("file_name") or ""),
                "sample_id": str(row.get("sample_id") or ""),
                "source_page_start": _to_int(row.get("source_page_start")),
                "source_page_end": _to_int(row.get("source_page_end")),
                "source_anchor": str(row.get("source_anchor") or ""),
                "section_code": section_code,
                "auto_requirement_text": text,
                "fingerprint": _item_fingerprint(
                    file_name=str(row.get("file_name") or ""),
                    section_code=section_code,
                    text=text,
                ),
            }
        )
    return output


def _item_fingerprint(*, file_name: str, section_code: str, text: str) -> str:
    norm_text = _normalize_text(text)
    return "|".join(
        [
            file_name.strip().lower(),
            section_code.strip().lower(),
            norm_text[:260],
        ]
    )


def _normalize_text(text: str) -> str:
    value = str(text or "").lower().strip()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff ]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _extract_section_code(anchor: str) -> str:
    match = SECTION_RE.search(anchor or "")
    return match.group(1) if match else ""


def _suggest_action(tag: str) -> str:
    lowered = tag.strip().lower()
    if lowered in TAG_ACTIONS:
        return TAG_ACTIONS[lowered]
    for key, action in TAG_ACTIONS.items():
        if key in lowered:
            return action
    return "补充该错误标签样本并加入下一轮规则回归，验证修复前后差异。"


def _split_error_tags(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    parts = [part.strip().lower() for part in TAG_SPLIT_RE.split(text) if part.strip()]
    return parts


def _is_binary_label(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered in {"0", "1", "true", "false", "y", "n", "yes", "no"}


def _is_split_fail(value: Any) -> bool:
    lowered = str(value or "").strip().lower()
    return lowered in SPLIT_FAIL_VALUES


def _load_round_item_rows(round_id: str) -> list[dict[str, str]]:
    csv_path = _round_dir(round_id) / "phase0_item_annotation_template.csv"
    if not csv_path.exists():
        return []
    return _read_csv(csv_path)


def _load_iteration_board_rows() -> list[dict[str, str]]:
    if not BOARD_CSV.exists():
        return []
    return _read_csv(BOARD_CSV)


def _round_dir(round_id: str) -> Path:
    return ITERATIONS_DIR / str(round_id or "").strip()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _to_float(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return round(float(text), 4)
    except ValueError:
        return 0.0


def _truncate(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"
