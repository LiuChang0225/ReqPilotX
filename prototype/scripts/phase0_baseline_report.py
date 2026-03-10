from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path
from statistics import mean, median
from typing import Any


FALSE_POSITIVE_PATTERNS = [
    ("header_footer_noise", re.compile(r"page\s+\d+|pages?\s+\d+\s+of\s+\d+", re.I)),
    ("copyright_confidential", re.compile(r"copyright|confidential|internal use only", re.I)),
    ("signature_block", re.compile(r"编制|审核|批准|校对|会签|审定", re.I)),
    ("toc_heading", re.compile(r"contents|目次|目录|foreword|前言", re.I)),
    ("history_meta", re.compile(r"revision history|version history|document key|decision date", re.I)),
    ("legal_disclaimer", re.compile(r"patent|rights reserved|不得|禁止", re.I)),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Phase-0 baseline report.")
    parser.add_argument(
        "--phase0-dir",
        type=Path,
        default=Path("eval/phase0"),
        help="Directory containing phase0 annotation files.",
    )
    args = parser.parse_args()

    phase0_dir = args.phase0_dir.resolve()
    page_csv = phase0_dir / "phase0_page_annotation_template.csv"
    item_csv = phase0_dir / "phase0_item_annotation_template.csv"
    summary_json = phase0_dir / "phase0_sampling_summary.json"
    report_md = phase0_dir / "phase0_baseline_report.md"
    priority_csv = phase0_dir / "phase0_labeling_priority.csv"

    pages = _read_csv(page_csv)
    items = _read_csv(item_csv)
    summary = json.loads(summary_json.read_text(encoding="utf-8")) if summary_json.exists() else {}

    if not pages:
        raise SystemExit(f"No page rows found: {page_csv}")

    baseline = _build_baseline_metrics(pages, items)
    manual = _build_manual_metrics(pages, items)
    fp_patterns = _build_fp_patterns(items, manual["has_item_labels"])
    priority_rows = _build_labeling_priority(pages)

    report_md.write_text(
        _render_report(summary, baseline, manual, fp_patterns),
        encoding="utf-8",
    )
    _write_csv(priority_csv, priority_rows)
    print(f"[phase0] baseline report: {report_md}")
    print(f"[phase0] labeling priority: {priority_csv}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        return list(reader)


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _build_baseline_metrics(pages: list[dict[str, str]], items: list[dict[str, str]]) -> dict[str, Any]:
    total_pages = len(pages)
    parse_has_text = [int(_to_int(row.get("parse_has_text"))) for row in pages]
    parse_coverage = sum(parse_has_text) / total_pages if total_pages else 0.0

    raw_noise_ratios = [_to_float(row.get("raw_noise_ratio")) for row in pages]
    auto_item_counts = [_to_int(row.get("auto_predicted_item_count")) for row in pages]
    pages_with_items = sum(1 for n in auto_item_counts if n > 0)

    predicted_items = [row for row in items if row.get("source_type") == "predicted"]
    unique_text_keys = {_norm_text(row.get("auto_requirement_text", "")) for row in predicted_items}
    unique_text_keys.discard("")
    duplicate_rate = 0.0
    if predicted_items:
        duplicate_rate = 1 - len(unique_text_keys) / len(predicted_items)

    type_counter = Counter(row.get("auto_requirement_type", "") for row in predicted_items)
    priority_counter = Counter(row.get("auto_priority", "") for row in predicted_items)

    text_lengths = [len((row.get("auto_requirement_text") or "").strip()) for row in predicted_items]
    short_item_rate = (
        sum(1 for n in text_lengths if n and n < 20) / len(text_lengths) if text_lengths else 0.0
    )

    return {
        "total_pages": total_pages,
        "parse_coverage": parse_coverage,
        "avg_raw_noise_ratio": mean(raw_noise_ratios) if raw_noise_ratios else 0.0,
        "median_raw_noise_ratio": median(raw_noise_ratios) if raw_noise_ratios else 0.0,
        "pages_with_items": pages_with_items,
        "total_predicted_items": len(predicted_items),
        "avg_items_per_page": mean(auto_item_counts) if auto_item_counts else 0.0,
        "duplicate_rate": duplicate_rate,
        "short_item_rate": short_item_rate,
        "type_counter": dict(type_counter),
        "priority_counter": dict(priority_counter),
    }


def _build_manual_metrics(pages: list[dict[str, str]], items: list[dict[str, str]]) -> dict[str, Any]:
    predicted_rows = [row for row in items if row.get("source_type") == "predicted"]
    labeled_predicted = [
        row for row in predicted_rows if _is_labeled_binary(row.get("is_true_requirement", ""))
    ]

    has_item_labels = len(labeled_predicted) > 0
    tp = sum(1 for row in labeled_predicted if _to_bool(row.get("is_true_requirement", "")))
    fp = sum(1 for row in labeled_predicted if not _to_bool(row.get("is_true_requirement", "")))
    precision = tp / (tp + fp) if (tp + fp) > 0 else None

    # Recall source 1: explicit missed_gold rows.
    missed_gold_rows = [row for row in items if row.get("source_type") == "missed_gold"]
    fn = len([row for row in missed_gold_rows if (row.get("gold_requirement_text") or "").strip()])

    # Recall source 2: page-level gold totals.
    page_gold_totals = [_to_int(row.get("gold_total_requirements")) for row in pages]
    total_gold_by_pages = sum(n for n in page_gold_totals if n >= 0)
    has_page_gold = any(str(row.get("gold_total_requirements", "")).strip() for row in pages)

    recall = None
    recall_method = "not_available"
    if has_item_labels and fn > 0:
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        recall_method = "item_missed_gold"
    elif has_item_labels and has_page_gold and total_gold_by_pages > 0:
        approx_fn = max(0, total_gold_by_pages - tp)
        recall = tp / (tp + approx_fn) if (tp + approx_fn) > 0 else None
        recall_method = "page_gold_total"

    split_rows = [row for row in labeled_predicted if _is_labeled_binary(row.get("is_split_correct", ""))]
    type_rows = [row for row in labeled_predicted if _is_labeled_binary(row.get("is_type_correct", ""))]
    split_accuracy = (
        sum(1 for row in split_rows if _to_bool(row.get("is_split_correct", ""))) / len(split_rows)
        if split_rows
        else None
    )
    type_accuracy = (
        sum(1 for row in type_rows if _to_bool(row.get("is_type_correct", ""))) / len(type_rows)
        if type_rows
        else None
    )

    return {
        "has_item_labels": has_item_labels,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "recall_method": recall_method,
        "labeled_predicted_count": len(labeled_predicted),
        "split_labeled_count": len(split_rows),
        "type_labeled_count": len(type_rows),
        "split_accuracy": split_accuracy,
        "type_accuracy": type_accuracy,
    }


def _build_fp_patterns(items: list[dict[str, str]], has_item_labels: bool) -> list[tuple[str, int]]:
    if has_item_labels:
        candidates = [
            row
            for row in items
            if row.get("source_type") == "predicted"
            and _is_labeled_binary(row.get("is_true_requirement", ""))
            and not _to_bool(row.get("is_true_requirement", ""))
        ]
    else:
        # Proxy: treat all predicted rows as candidates and count likely noise signatures.
        candidates = [row for row in items if row.get("source_type") == "predicted"]

    counter = Counter()
    for row in candidates:
        text = (row.get("auto_requirement_text") or "").strip()
        lowered = text.lower()
        matched = False
        for tag, pattern in FALSE_POSITIVE_PATTERNS:
            if pattern.search(lowered):
                counter[tag] += 1
                matched = True
        if not matched and text:
            counter["other"] += 1
    return counter.most_common(10)


def _build_labeling_priority(pages: list[dict[str, str]]) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for row in pages:
        parse_has_text = _to_int(row.get("parse_has_text"))
        noise_ratio = _to_float(row.get("raw_noise_ratio"))
        item_count = _to_int(row.get("auto_predicted_item_count"))
        parse_char_count = _to_int(row.get("parse_char_count"))
        # Prioritize pages that are noisy, unreadable, or suspiciously empty.
        score = 0.0
        if parse_has_text == 0:
            score += 1.0
        score += min(1.0, noise_ratio) * 0.6
        if item_count == 0 and parse_char_count > 150:
            score += 0.25
        if item_count >= 5:
            score += 0.1
        if parse_char_count < 60:
            score += 0.1
        scored.append(
            {
                "sample_id": row.get("sample_id", ""),
                "file_name": row.get("file_name", ""),
                "page_no": _to_int(row.get("page_no")),
                "priority_score": round(score, 4),
                "parse_has_text": parse_has_text,
                "raw_noise_ratio": round(noise_ratio, 4),
                "auto_predicted_item_count": item_count,
                "parse_char_count": parse_char_count,
                "cleaned_text_preview": row.get("cleaned_text_preview", ""),
            }
        )
    scored.sort(key=lambda r: r["priority_score"], reverse=True)
    return scored


def _render_report(
    summary: dict[str, Any],
    baseline: dict[str, Any],
    manual: dict[str, Any],
    fp_patterns: list[tuple[str, int]],
) -> str:
    docs_lines = []
    for doc in summary.get("docs", []):
        docs_lines.append(
            f"- `{doc['file_name']}`: total `{doc['total_pages']}` pages, sampled `{doc['sampled_pages']}` pages"
        )
    docs_text = "\n".join(docs_lines) if docs_lines else "- N/A"

    precision = _fmt_pct(manual["precision"])
    recall = _fmt_pct(manual["recall"])
    split_acc = _fmt_pct(manual["split_accuracy"])
    type_acc = _fmt_pct(manual["type_accuracy"])

    fp_lines = "\n".join([f"- `{tag}`: {count}" for tag, count in fp_patterns]) or "- N/A"
    type_dist = ", ".join(f"{k}:{v}" for k, v in sorted(baseline["type_counter"].items())) or "N/A"
    pri_dist = ", ".join(f"{k}:{v}" for k, v in sorted(baseline["priority_counter"].items())) or "N/A"

    return f"""# Phase-0 Baseline Report

## 1) Sampling Overview
- Source PDF dir: `{summary.get('pdf_dir', 'N/A')}`
- Target sample pages: `{summary.get('target_pages', 'N/A')}`
- Actual sample pages: `{baseline['total_pages']}`
- Auto predicted items: `{baseline['total_predicted_items']}`

{docs_text}

## 2) Baseline Metrics (Current Parser + Extractor)
- Parse coverage (`parse_has_text=1`): `{baseline['parse_coverage']:.2%}`
- Avg raw noise ratio: `{baseline['avg_raw_noise_ratio']:.2%}`
- Median raw noise ratio: `{baseline['median_raw_noise_ratio']:.2%}`
- Pages with extracted items: `{baseline['pages_with_items']}/{baseline['total_pages']}`
- Avg extracted items per page: `{baseline['avg_items_per_page']:.2f}`
- Duplicate item rate: `{baseline['duplicate_rate']:.2%}`
- Short item rate (`len<20`): `{baseline['short_item_rate']:.2%}`
- Auto type distribution: `{type_dist}`
- Auto priority distribution: `{pri_dist}`

## 3) Supervised Metrics (After Manual Annotation)
- Labeled predicted items: `{manual['labeled_predicted_count']}`
- Precision: `{precision}`
- Recall: `{recall}` (method: `{manual['recall_method']}`)
- Split accuracy: `{split_acc}` (split-labeled rows: `{manual['split_labeled_count']}`)
- Type accuracy: `{type_acc}` (type-labeled rows: `{manual['type_labeled_count']}`)

## 4) Top False-Positive Patterns
{fp_lines}

## 5) Interpretation
- If Precision/Recall are `PENDING`, complete manual labels in:
  - `phase0_page_annotation_template.csv`
  - `phase0_item_annotation_template.csv`
- For strict Recall, append missed items with `source_type=missed_gold`.
- Use this report as the gate for Parse/Extract iteration acceptance.
"""


def _norm_text(text: str) -> str:
    value = (text or "").strip().lower()
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\u4e00-\u9fff ]", "", value)
    value = re.sub(r"\d+", "#", value)
    return value


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
        return float(text)
    except ValueError:
        return 0.0


def _to_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "t", "yes", "y"}


def _is_labeled_binary(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "0", "true", "false", "t", "f", "yes", "no", "y", "n"}


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "PENDING"
    return f"{value:.2%}"


if __name__ == "__main__":
    main()
