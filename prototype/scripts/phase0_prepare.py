from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from app.services.extractor import extract_atomic_requirements
from app.services.parser import parse_document


NOISE_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in [
        r"copyright",
        r"confidential",
        r"internal use only",
        r"uncontrolled copy",
        r"revision history",
        r"version history",
        r"document key",
        r"\besign\b",
        r"designed by",
        r"released by",
        r"\bto[:：]",
        r"\bby[:：]",
        r"\bof[:：]",
        r"\bat[:：]",
        r"编制[:：_]",
        r"校对[:：_]",
        r"审核[:：_]",
        r"标审[:：_]",
        r"会签[:：_]",
        r"审定[:：_]",
        r"批准[:：_]",
    ]
]


@dataclass
class DocMeta:
    file_path: Path
    file_name: str
    total_pages: int


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Phase-0 sampling and annotation files.")
    parser.add_argument("pdf_dir", type=Path, help="Directory that contains PDF samples.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/phase0"),
        help="Output directory for sampling files.",
    )
    parser.add_argument("--target-pages", type=int, default=100, help="Number of sampled pages.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for stable sampling.")
    parser.add_argument("--min-per-doc", type=int, default=8, help="Minimum sample pages per PDF.")
    parser.add_argument(
        "--exclude-name-regex",
        action="append",
        default=[],
        help="Exclude PDFs whose file name matches this regex. Can be used multiple times.",
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir.expanduser().resolve()
    out_dir = args.out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)

    exclude_patterns = [re.compile(pattern, re.I) for pattern in args.exclude_name_regex]
    docs = _collect_docs(pdf_dir, exclude_patterns)
    if not docs:
        raise SystemExit(f"No PDF files found in {pdf_dir}")

    allocation = _allocate_pages(docs, args.target_pages, args.min_per_doc, rng)
    sample_rows: list[dict[str, Any]] = []
    item_rows: list[dict[str, Any]] = []

    for doc in docs:
        sample_pages = _sample_pages(doc.total_pages, allocation[doc.file_name], rng)
        _, segments, _ = parse_document(str(doc.file_path))
        doc_requirements = extract_atomic_requirements(segments)
        reqs_start_by_page: dict[int, list[dict[str, Any]]] = {}
        reqs_span_by_page: dict[int, list[dict[str, Any]]] = {}
        for req in doc_requirements:
            start = _safe_int(req.get("source_page"))
            end = _safe_int(req.get("source_page_end")) or start
            if start <= 0:
                continue
            if end < start:
                end = start
            reqs_start_by_page.setdefault(start, []).append(req)
            for page in range(start + 1, end + 1):
                reqs_span_by_page.setdefault(page, []).append(req)

        segment_by_page = {
            int(segment["source_page"]): segment["text"]
            for segment in segments
            if segment.get("source_page") is not None
        }
        pdf_reader = PdfReader(str(doc.file_path))

        for page_no in sample_pages:
            sample_id = _build_sample_id(doc.file_name, page_no)
            raw_text = _normalize_text(pdf_reader.pages[page_no - 1].extract_text() or "")
            cleaned_text = _normalize_text(segment_by_page.get(page_no, ""))
            raw_lines = [line for line in raw_text.splitlines() if line.strip()]
            noise_line_count = sum(1 for line in raw_lines if _is_noise_line(line))
            parse_has_text = 1 if cleaned_text.strip() else 0
            parse_char_count = len(cleaned_text)
            parse_line_count = len([line for line in cleaned_text.splitlines() if line.strip()])
            raw_noise_ratio = round(noise_line_count / len(raw_lines), 4) if raw_lines else 1.0
            raw_text_preview = _preview(raw_text, 700)
            cleaned_text_preview = _preview(cleaned_text, 700)

            predicted_items = reqs_start_by_page.get(page_no, []) or reqs_span_by_page.get(page_no, [])
            predicted_items = sorted(predicted_items, key=lambda item: item.get("req_code", ""))
            for idx, req in enumerate(predicted_items, start=1):
                item_rows.append(
                    {
                        "sample_id": sample_id,
                        "item_id": f"{sample_id}-I{idx:03d}",
                        "file_name": doc.file_name,
                        "page_no": page_no,
                        "source_type": "predicted",
                        "source_page_start": req.get("source_page"),
                        "source_page_end": req.get("source_page_end", req.get("source_page")),
                        "source_anchor": req.get("source_anchor", ""),
                        "auto_requirement_text": req["requirement_text"],
                        "auto_requirement_type": req["requirement_type"],
                        "auto_priority": req["priority"],
                        "is_true_requirement": "",
                        "is_split_correct": "",
                        "is_type_correct": "",
                        "gold_requirement_text": "",
                        "gold_requirement_type": "",
                        "error_tags": "",
                        "annotator": "",
                        "notes": "",
                    }
                )

            sample_rows.append(
                {
                    "sample_id": sample_id,
                    "file_name": doc.file_name,
                    "file_path": str(doc.file_path),
                    "page_no": page_no,
                    "total_pages": doc.total_pages,
                    "parse_has_text": parse_has_text,
                    "parse_char_count": parse_char_count,
                    "parse_line_count": parse_line_count,
                    "raw_noise_ratio": raw_noise_ratio,
                    "auto_predicted_item_count": len(predicted_items),
                    "raw_text_preview": raw_text_preview,
                    "cleaned_text_preview": cleaned_text_preview,
                    "is_page_readable": "",
                    "gold_total_requirements": "",
                    "gold_noise_lines_pct": "",
                    "annotator": "",
                    "notes": "",
                }
            )

    sample_rows.sort(key=lambda row: (row["file_name"], int(row["page_no"])))
    item_rows.sort(key=lambda row: (row["file_name"], int(row["page_no"]), row["item_id"]))

    sample_csv = out_dir / "phase0_page_annotation_template.csv"
    item_csv = out_dir / "phase0_item_annotation_template.csv"
    summary_json = out_dir / "phase0_sampling_summary.json"
    guide_md = out_dir / "phase0_annotation_guide.md"

    _write_csv(sample_csv, sample_rows)
    _write_csv(item_csv, item_rows)

    summary = {
        "pdf_dir": str(pdf_dir),
        "target_pages": args.target_pages,
        "actual_sample_pages": len(sample_rows),
        "docs": [
            {
                "file_name": doc.file_name,
                "total_pages": doc.total_pages,
                "sampled_pages": allocation[doc.file_name],
            }
            for doc in docs
        ],
        "total_auto_items": len(item_rows),
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    guide_md.write_text(_build_annotation_guide(), encoding="utf-8")

    print(f"[phase0] sample pages: {len(sample_rows)}")
    print(f"[phase0] auto items:   {len(item_rows)}")
    print(f"[phase0] page template: {sample_csv}")
    print(f"[phase0] item template: {item_csv}")
    print(f"[phase0] summary:       {summary_json}")


def _collect_docs(pdf_dir: Path, exclude_patterns: list[re.Pattern[str]] | None = None) -> list[DocMeta]:
    docs: list[DocMeta] = []
    exclude_patterns = exclude_patterns or []
    for file_path in sorted(pdf_dir.glob("*.pdf")):
        if any(pattern.search(file_path.name) for pattern in exclude_patterns):
            continue
        reader = PdfReader(str(file_path))
        docs.append(
            DocMeta(
                file_path=file_path,
                file_name=file_path.name,
                total_pages=len(reader.pages),
            )
        )
    return docs


def _allocate_pages(
    docs: list[DocMeta], target_pages: int, min_per_doc: int, rng: random.Random
) -> dict[str, int]:
    if target_pages < len(docs):
        raise ValueError("target-pages must be >= number of documents")

    total_pages = sum(doc.total_pages for doc in docs)
    allocation = {
        doc.file_name: max(min_per_doc, int(round(target_pages * doc.total_pages / total_pages)))
        for doc in docs
    }

    # Never sample more than available pages.
    for doc in docs:
        allocation[doc.file_name] = min(allocation[doc.file_name], doc.total_pages)

    current = sum(allocation.values())
    if current > target_pages:
        over = current - target_pages
        for doc in sorted(docs, key=lambda x: allocation[x.file_name], reverse=True):
            while over > 0 and allocation[doc.file_name] > min(min_per_doc, doc.total_pages):
                allocation[doc.file_name] -= 1
                over -= 1
            if over == 0:
                break
    elif current < target_pages:
        under = target_pages - current
        doc_pool = docs[:]
        rng.shuffle(doc_pool)
        idx = 0
        while under > 0 and doc_pool:
            doc = doc_pool[idx % len(doc_pool)]
            if allocation[doc.file_name] < doc.total_pages:
                allocation[doc.file_name] += 1
                under -= 1
            idx += 1
            if idx > 10000:
                break
    return allocation


def _sample_pages(total_pages: int, sample_count: int, rng: random.Random) -> list[int]:
    if sample_count >= total_pages:
        return list(range(1, total_pages + 1))
    if sample_count <= 0:
        return []

    pages = set()
    for i in range(sample_count):
        # Stratified position in [1, total_pages]
        pos = int(round((i + 0.5) * total_pages / sample_count))
        pos = max(1, min(total_pages, pos))
        pages.add(pos)

    while len(pages) < sample_count:
        pages.add(rng.randint(1, total_pages))
    return sorted(pages)


def _normalize_text(text: str) -> str:
    lines = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", line)
        line = re.sub(r"\s+", " ", line)
        lines.append(line)
    return "\n".join(lines)


def _safe_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _is_noise_line(line: str) -> bool:
    text = line.strip()
    if not text:
        return True
    if len(text) <= 2:
        return True
    if re.fullmatch(r"[\d\-/:\. ]+", text):
        return True
    if sum(ch.isdigit() for ch in text) >= max(8, len(text) // 2):
        return True
    for pattern in NOISE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _build_sample_id(file_name: str, page_no: int) -> str:
    digest = hashlib.md5(f"{file_name}::{page_no}".encode("utf-8")).hexdigest()[:10]
    return f"S{digest.upper()}"


def _preview(text: str, max_len: int) -> str:
    value = text.strip()
    if len(value) <= max_len:
        return value
    return f"{value[:max_len]}..."


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _build_annotation_guide() -> str:
    return """# Phase-0 Annotation Guide

## 1. Page-Level (`phase0_page_annotation_template.csv`)
- `is_page_readable`: `1` readable, `0` unreadable (scan/garbled).
- `gold_total_requirements`: this page's true requirement count.
- `gold_noise_lines_pct`: estimated noise line percentage (0-100).
- `notes`: record special issue (table-only, watermark-heavy, etc.).

## 2. Item-Level (`phase0_item_annotation_template.csv`)
- `source_type=predicted` rows are auto-extracted candidates.
- `is_true_requirement`: `1` true requirement, `0` false positive.
- `is_split_correct`: `1` if one atomic requirement; `0` if over/under split.
- `is_type_correct`: `1` if type classification is correct.
- `gold_requirement_text`: corrected requirement text when needed.
- `gold_requirement_type`: corrected type when needed.
- `error_tags`: comma-separated tags such as
  `header_footer_noise, table_parse_error, heading_leakage, broken_ocr, over_split, under_split`.

## 3. Optional Recall Support
When you find missing requirements (model did not extract), append new rows with:
- `source_type=missed_gold`
- `auto_requirement_text` empty
- fill `gold_requirement_text` and `gold_requirement_type`

This enables strict Recall computation in baseline reports.
"""


if __name__ == "__main__":
    main()
