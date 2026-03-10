from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path
from typing import Any


SECTION_RE = re.compile(r"Section\s+([0-9]+(?:\.[0-9]+){0,8})", re.I)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build high-risk split-label queue from phase0 outputs.")
    parser.add_argument(
        "--phase0-dir",
        type=Path,
        required=True,
        help="Iteration directory containing phase0_item_annotation_template.csv",
    )
    parser.add_argument(
        "--quality-gate-dir",
        type=Path,
        default=None,
        help="Optional quality gate output dir with structural_quality_docs.csv and issues.csv",
    )
    parser.add_argument("--queue-size", type=int, default=80)
    parser.add_argument("--target-labeled", type=int, default=60)
    parser.add_argument("--out-csv", type=Path, default=None)
    parser.add_argument("--out-md", type=Path, default=None)
    args = parser.parse_args()

    phase0_dir = args.phase0_dir.expanduser().resolve()
    item_csv = phase0_dir / "phase0_item_annotation_template.csv"
    if not item_csv.exists():
        raise SystemExit(f"Missing item CSV: {item_csv}")

    quality_gate_dir = args.quality_gate_dir.expanduser().resolve() if args.quality_gate_dir else None
    docs_score = _load_doc_scores(quality_gate_dir)
    section_issues = _load_section_issues(quality_gate_dir)

    rows = _read_csv(item_csv)
    predicted = [row for row in rows if row.get("source_type") == "predicted"]
    labeled_split = [row for row in predicted if _is_binary_label(row.get("is_split_correct", ""))]
    unlabeled_split = [row for row in predicted if not _is_binary_label(row.get("is_split_correct", ""))]

    queue_size = max(1, int(args.queue_size))
    target_labeled = max(1, int(args.target_labeled))
    need_more = max(0, target_labeled - len(labeled_split))
    recommended_size = max(need_more, queue_size)

    ranked = []
    for row in unlabeled_split:
        scored = _score_item(row, docs_score, section_issues)
        ranked.append(scored)
    ranked.sort(key=lambda item: (item["risk_score"], item["text_length"]), reverse=True)

    selected = ranked[:recommended_size]
    out_rows = []
    for idx, item in enumerate(selected, start=1):
        row = item["row"]
        out_rows.append(
            {
                "rank": idx,
                "item_id": row.get("item_id", ""),
                "sample_id": row.get("sample_id", ""),
                "file_name": row.get("file_name", ""),
                "source_page_start": row.get("source_page_start", ""),
                "source_page_end": row.get("source_page_end", ""),
                "section_code": item["section_code"],
                "source_anchor": row.get("source_anchor", ""),
                "risk_score": f"{item['risk_score']:.2f}",
                "risk_reasons": ",".join(item["risk_reasons"]),
                "text_length": item["text_length"],
                "auto_requirement_text": row.get("auto_requirement_text", ""),
                "is_split_correct": row.get("is_split_correct", ""),
                "notes": row.get("notes", ""),
            }
        )

    out_csv = args.out_csv.expanduser().resolve() if args.out_csv else phase0_dir / "phase0_split_active_queue.csv"
    out_md = args.out_md.expanduser().resolve() if args.out_md else phase0_dir / "phase0_split_active_queue_summary.md"
    _write_csv(out_csv, out_rows)
    out_md.write_text(
        _render_summary_md(
            phase0_dir=phase0_dir,
            labeled_count=len(labeled_split),
            unlabeled_count=len(unlabeled_split),
            target_labeled=target_labeled,
            need_more=need_more,
            selected_count=len(out_rows),
            out_csv=out_csv,
        ),
        encoding="utf-8",
    )

    print(f"[split-queue] labeled_split={len(labeled_split)}")
    print(f"[split-queue] unlabeled_split={len(unlabeled_split)}")
    print(f"[split-queue] target_labeled={target_labeled} need_more={need_more}")
    print(f"[split-queue] selected={len(out_rows)}")
    print(f"[split-queue] out_csv={out_csv}")
    print(f"[split-queue] out_md={out_md}")


def _score_item(
    row: dict[str, str],
    docs_score: dict[str, float],
    section_issues: dict[tuple[str, str], set[str]],
) -> dict[str, Any]:
    file_name = str(row.get("file_name", ""))
    section_code = _extract_section_code(str(row.get("source_anchor", "")))
    page_start = _to_int(row.get("source_page_start"))
    page_end = _to_int(row.get("source_page_end")) or page_start
    text = str(row.get("auto_requirement_text", ""))

    score = 0.0
    reasons: list[str] = []

    doc_score = docs_score.get(file_name)
    if doc_score is not None:
        if doc_score < 60:
            score += 3.0
            reasons.append("doc_score_lt_60")
        elif doc_score < 80:
            score += 2.0
            reasons.append("doc_score_lt_80")
        elif doc_score < 90:
            score += 1.0
            reasons.append("doc_score_lt_90")

    if (file_name, section_code) in section_issues:
        tags = section_issues[(file_name, section_code)]
        score += 3.0
        reasons.append(f"section_issue:{'+'.join(sorted(tags))}")

    if not section_code:
        score += 1.5
        reasons.append("missing_section_code")

    if page_start >= 30 and section_code.split(".")[0] in {"1", "2"}:
        score += 2.0
        reasons.append("late_low_major")

    span = max(1, page_end - page_start + 1)
    if span >= 2:
        score += 0.7
        reasons.append("multi_page_span")

    text_len = len(text.strip())
    if text_len >= 1200:
        score += 1.0
        reasons.append("very_long_text")
    elif text_len >= 700:
        score += 0.6
        reasons.append("long_text")

    table_hits = len(re.findall(r"(?:\btable\b|表|figure|图|test matrix|测试矩阵|试验参数)", text, re.I))
    if table_hits >= 4:
        score += 1.0
        reasons.append("table_dense")
    if text.count("|") >= 8:
        score += 1.0
        reasons.append("pipe_table_artifact")

    if not reasons:
        reasons.append("coverage_fill")

    return {
        "row": row,
        "risk_score": score,
        "risk_reasons": reasons,
        "section_code": section_code,
        "text_length": text_len,
    }


def _load_doc_scores(quality_gate_dir: Path | None) -> dict[str, float]:
    if not quality_gate_dir:
        return {}
    csv_path = quality_gate_dir / "structural_quality_docs.csv"
    if not csv_path.exists():
        return {}
    rows = _read_csv(csv_path)
    out: dict[str, float] = {}
    for row in rows:
        name = str(row.get("pdf_name", ""))
        if not name:
            continue
        out[name] = _to_float(row.get("score_pct"))
    return out


def _load_section_issues(quality_gate_dir: Path | None) -> dict[tuple[str, str], set[str]]:
    if not quality_gate_dir:
        return {}
    csv_path = quality_gate_dir / "structural_quality_issues.csv"
    if not csv_path.exists():
        return {}
    rows = _read_csv(csv_path)
    out: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        pdf = str(row.get("pdf_name", ""))
        code = str(row.get("section_code", ""))
        issue_type = str(row.get("issue_type", ""))
        if not pdf or not code:
            continue
        out.setdefault((pdf, code), set()).add(issue_type)
    return out


def _extract_section_code(anchor: str) -> str:
    match = SECTION_RE.search(anchor or "")
    return match.group(1) if match else ""


def _render_summary_md(
    phase0_dir: Path,
    labeled_count: int,
    unlabeled_count: int,
    target_labeled: int,
    need_more: int,
    selected_count: int,
    out_csv: Path,
) -> str:
    return (
        "# Split Active Queue\n\n"
        f"- phase0_dir: `{phase0_dir}`\n"
        f"- current labeled split rows: `{labeled_count}`\n"
        f"- unlabeled split rows: `{unlabeled_count}`\n"
        f"- target labeled split rows: `{target_labeled}`\n"
        f"- additional labels needed: `{need_more}`\n"
        f"- queue selected rows: `{selected_count}`\n"
        f"- queue file: `{out_csv}`\n"
    )


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


def _is_binary_label(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"0", "1", "true", "false", "yes", "no", "y", "n"}


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


if __name__ == "__main__":
    main()
