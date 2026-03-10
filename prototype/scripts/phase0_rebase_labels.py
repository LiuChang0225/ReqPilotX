from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


LABEL_FIELDS = [
    "is_true_requirement",
    "is_split_correct",
    "is_type_correct",
    "gold_requirement_text",
    "gold_requirement_type",
    "error_tags",
    "annotator",
    "notes",
]
SPLIT_CONTEXT_FIELDS = {"is_split_correct", "gold_requirement_text", "error_tags", "notes"}
SECTION_RE = re.compile(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})", re.I)


@dataclass(frozen=True)
class MappingCandidate:
    score: float
    old_index: int
    new_index: int
    similarity: float


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rebase old item labels onto a newly generated phase0 item CSV."
    )
    parser.add_argument(
        "--old-item-csv",
        type=Path,
        required=True,
        help="Previous labeled phase0_item_annotation_template.csv",
    )
    parser.add_argument(
        "--new-item-csv",
        type=Path,
        required=True,
        help="Newly generated phase0_item_annotation_template.csv (will be updated in-place)",
    )
    parser.add_argument(
        "--focus-csv",
        type=Path,
        default=None,
        help="Optional output CSV with changed mapped rows that need re-review.",
    )
    parser.add_argument(
        "--split-recheck-csv",
        type=Path,
        default=None,
        help="Optional CSV of mapped rows where split label was intentionally not transferred.",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=35.0,
        help="Minimum mapping score to accept a match.",
    )
    parser.add_argument(
        "--split-transfer-min-sim",
        type=float,
        default=0.92,
        help="Minimum text similarity to transfer split labels.",
    )
    parser.add_argument(
        "--allow-span-change-for-split",
        action="store_true",
        help="Allow split label transfer even if source_page_start/end changed.",
    )
    args = parser.parse_args()

    old_rows = _read_csv(args.old_item_csv)
    new_rows = _read_csv(args.new_item_csv)
    if not old_rows or not new_rows:
        raise SystemExit("Input CSV is empty or missing.")

    old_labeled = [
        (idx, row)
        for idx, row in enumerate(old_rows)
        if row.get("source_type") == "predicted" and any((row.get(f, "").strip() for f in LABEL_FIELDS))
    ]
    new_predicted = [
        (idx, row) for idx, row in enumerate(new_rows) if row.get("source_type") == "predicted"
    ]
    new_by_sample: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for idx, row in new_predicted:
        new_by_sample.setdefault(row.get("sample_id", ""), []).append((idx, row))

    candidates: list[MappingCandidate] = []
    for old_idx, old_row in old_labeled:
        sample_id = old_row.get("sample_id", "")
        for new_idx, new_row in new_by_sample.get(sample_id, []):
            similarity = _text_similarity(
                old_row.get("auto_requirement_text", ""),
                new_row.get("auto_requirement_text", ""),
            )
            score = _mapping_score(old_row, new_row, similarity)
            candidates.append(
                MappingCandidate(
                    score=score,
                    old_index=old_idx,
                    new_index=new_idx,
                    similarity=similarity,
                )
            )

    candidates.sort(key=lambda x: x.score, reverse=True)
    assigned_old: set[int] = set()
    assigned_new: set[int] = set()
    mapping: list[MappingCandidate] = []
    for cand in candidates:
        if cand.score < args.min_score:
            continue
        if cand.old_index in assigned_old or cand.new_index in assigned_new:
            continue
        assigned_old.add(cand.old_index)
        assigned_new.add(cand.new_index)
        mapping.append(cand)

    split_recheck_rows: list[dict[str, Any]] = []
    for cand in mapping:
        old_row = old_rows[cand.old_index]
        new_row = new_rows[cand.new_index]
        transfer_split = _should_transfer_split_label(old_row, new_row, cand, args)
        for field in LABEL_FIELDS:
            if field in SPLIT_CONTEXT_FIELDS and not transfer_split:
                continue
            new_row[field] = old_row.get(field, "")
        if not transfer_split and str(old_row.get("is_split_correct", "")).strip():
            split_recheck_rows.append(_build_split_recheck_row(old_row, new_row, cand))

    _write_csv(args.new_item_csv, new_rows)

    unmapped = [old_rows[idx] for idx, _ in old_labeled if idx not in assigned_old]
    print(f"[rebase] old labeled predicted rows: {len(old_labeled)}")
    print(f"[rebase] mapped rows: {len(mapping)}")
    print(f"[rebase] unmapped rows: {len(unmapped)}")
    if unmapped:
        print("[rebase] unmapped item_ids:")
        for row in unmapped:
            print(f"  - {row.get('item_id', '')}")

    if args.focus_csv:
        focus_rows = _build_focus_rows(old_rows, new_rows, mapping)
        _write_csv(args.focus_csv, focus_rows)
        print(f"[rebase] focus rows: {len(focus_rows)} -> {args.focus_csv}")
    if args.split_recheck_csv:
        _write_csv(args.split_recheck_csv, split_recheck_rows)
        print(f"[rebase] split recheck rows: {len(split_recheck_rows)} -> {args.split_recheck_csv}")


def _mapping_score(old_row: dict[str, str], new_row: dict[str, str], similarity: float) -> float:
    score = similarity * 35
    old_sec = _section_code(old_row.get("source_anchor", ""))
    new_sec = _section_code(new_row.get("source_anchor", ""))
    if old_sec and old_sec == new_sec:
        score += 40
    if str(old_row.get("source_page_start", "")).strip() == str(new_row.get("source_page_start", "")).strip():
        score += 15
    if str(old_row.get("source_page_end", "")).strip() == str(new_row.get("source_page_end", "")).strip():
        score += 10
    if str(old_row.get("auto_requirement_type", "")).strip() == str(new_row.get("auto_requirement_type", "")).strip():
        score += 5
    return score


def _build_focus_rows(
    old_rows: list[dict[str, str]],
    new_rows: list[dict[str, str]],
    mapping: list[MappingCandidate],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cand in mapping:
        old_row = old_rows[cand.old_index]
        new_row = new_rows[cand.new_index]
        span_changed = (
            str(old_row.get("source_page_start", "")) != str(new_row.get("source_page_start", ""))
            or str(old_row.get("source_page_end", "")) != str(new_row.get("source_page_end", ""))
        )
        type_changed = old_row.get("auto_requirement_type", "") != new_row.get("auto_requirement_type", "")
        item_changed = old_row.get("item_id", "") != new_row.get("item_id", "")
        if not (span_changed or type_changed or item_changed or cand.similarity < 0.97):
            continue
        out.append(
            {
                "old_item_id": old_row.get("item_id", ""),
                "new_item_id": new_row.get("item_id", ""),
                "sample_id": old_row.get("sample_id", ""),
                "section_old": _section_code(old_row.get("source_anchor", "")),
                "section_new": _section_code(new_row.get("source_anchor", "")),
                "old_span": f"{old_row.get('source_page_start', '')}-{old_row.get('source_page_end', '')}",
                "new_span": f"{new_row.get('source_page_start', '')}-{new_row.get('source_page_end', '')}",
                "old_type": old_row.get("auto_requirement_type", ""),
                "new_type": new_row.get("auto_requirement_type", ""),
                "text_similarity": f"{cand.similarity:.4f}",
                "old_split_label": old_row.get("is_split_correct", ""),
                "old_type_label": old_row.get("is_type_correct", ""),
                "notes": old_row.get("error_tags", "") or old_row.get("notes", ""),
            }
        )
    out.sort(key=lambda row: (row["sample_id"], row["new_item_id"]))
    return out


def _build_split_recheck_row(
    old_row: dict[str, str],
    new_row: dict[str, str],
    cand: MappingCandidate,
) -> dict[str, Any]:
    return {
        "sample_id": old_row.get("sample_id", ""),
        "old_item_id": old_row.get("item_id", ""),
        "new_item_id": new_row.get("item_id", ""),
        "old_split_label": old_row.get("is_split_correct", ""),
        "section_old": _section_code(old_row.get("source_anchor", "")),
        "section_new": _section_code(new_row.get("source_anchor", "")),
        "old_span": f"{old_row.get('source_page_start', '')}-{old_row.get('source_page_end', '')}",
        "new_span": f"{new_row.get('source_page_start', '')}-{new_row.get('source_page_end', '')}",
        "text_similarity": f"{cand.similarity:.4f}",
        "old_text_preview": _preview(old_row.get("auto_requirement_text", "")),
        "new_text_preview": _preview(new_row.get("auto_requirement_text", "")),
    }


def _section_code(anchor: str) -> str:
    match = SECTION_RE.search(anchor or "")
    return match.group(1) if match else ""


def _text_similarity(left: str, right: str) -> float:
    a = _normalize_text(left)[:2600]
    b = _normalize_text(right)[:2600]
    if not a and not b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _normalize_text(text: str) -> str:
    value = (text or "").lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff ]", "", value)
    return value.strip()


def _preview(text: str, max_len: int = 260) -> str:
    value = str(text or "").strip().replace("\n", " ")
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}..."


def _should_transfer_split_label(
    old_row: dict[str, str],
    new_row: dict[str, str],
    candidate: MappingCandidate,
    args: argparse.Namespace,
) -> bool:
    old_split = str(old_row.get("is_split_correct", "")).strip()
    if not old_split:
        return True

    if candidate.similarity < float(args.split_transfer_min_sim):
        return False

    old_sec = _section_code(old_row.get("source_anchor", ""))
    new_sec = _section_code(new_row.get("source_anchor", ""))
    if old_sec and new_sec and old_sec != new_sec:
        return False

    if not bool(args.allow_span_change_for_split):
        if str(old_row.get("source_page_start", "")).strip() != str(new_row.get("source_page_start", "")).strip():
            return False
        if str(old_row.get("source_page_end", "")).strip() != str(new_row.get("source_page_end", "")).strip():
            return False

    return True


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
