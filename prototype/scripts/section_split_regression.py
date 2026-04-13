from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.services.extraction_preview import build_extraction_preview


DEFAULT_REQUIRED_CODES = [
    "3.1",
    "3.2",
    "3.3",
    "3.4",
    "3.5",
    "3.6",
    "3.7",
    "3.8",
    "3.9",
    "4.6.2",
    "4.6.4",
    "4.6.5",
    "4.6.6",
    "4.6.7",
    "4.6.8",
]
DEFAULT_GUARD_SECTIONS = ["5.1.12.2", "5.1.14.2", "5.3.9.4.3"]
SUSPICIOUS_TITLE_PATTERNS = [
    re.compile(r"个循环"),
    re.compile(r"engine/motor", re.I),
    re.compile(r"\bbased on\b", re.I),
    re.compile(r"\bbefore conducting\b", re.I),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Section split regression checks for Q-JLY style PDFs.")
    parser.add_argument("pdf_path", type=Path, help="Target PDF path.")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional JSON output path.")
    parser.add_argument("--csv-out", type=Path, default=None, help="Optional CSV output path.")
    args = parser.parse_args()

    preview = build_extraction_preview(str(args.pdf_path.expanduser().resolve()))
    report = evaluate_preview(preview, pdf_path=str(args.pdf_path))
    summary = report["summary"]
    detail = report["detail"]
    score_pct = float(summary["score_pct"])
    passed_count = int(summary["passed_checks"])
    total_count = int(summary["total_checks"])
    missing_required = list(detail["missing_required"])
    suspicious_global = list(detail["suspicious_global"])
    suspicious_near_guards = list(detail["suspicious_near_guards"])
    default_candidate_violations = int(detail["default_candidate_violations"])

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.csv_out:
        args.csv_out.parent.mkdir(parents=True, exist_ok=True)
        _write_csv(
            args.csv_out,
            [
                {
                    "pdf_path": str(args.pdf_path),
                    "passed": int(bool(report["summary"]["passed"])),
                    "score_pct": score_pct,
                    "outline_count": report["summary"]["outline_count"],
                    "requirements_count": report["summary"]["requirements_count"],
                    "missing_required_count": len(missing_required),
                    "suspicious_global_count": len(suspicious_global),
                    "suspicious_near_guard_count": len(suspicious_near_guards),
                    "candidate_violation_count": default_candidate_violations,
                }
            ],
        )

    print(f"[section-reg] score: {score_pct:.2f}% ({passed_count}/{total_count})")
    print(f"[section-reg] outline_count: {report['summary']['outline_count']}")
    print(f"[section-reg] requirements_count: {report['summary']['requirements_count']}")
    print(f"[section-reg] missing_required: {len(missing_required)}")
    print(f"[section-reg] suspicious_global: {len(suspicious_global)}")
    print(f"[section-reg] suspicious_near_guard: {len(suspicious_near_guards)}")
    print(f"[section-reg] candidate_violations: {default_candidate_violations}")

    if missing_required:
        print("[section-reg] missing codes:")
        for code in missing_required:
            print(f"  - {code}")

    if suspicious_global:
        print("[section-reg] suspicious global rows:")
        for row in suspicious_global[:20]:
            print(f"  - {row['section_code']} | p{row['source_page_start']} | {row['section_title'][:120]}")

    if suspicious_near_guards:
        print("[section-reg] suspicious near guard sections:")
        for row in suspicious_near_guards[:20]:
            print(
                f"  - guard={row['guard_section']} code={row['section_code']} "
                f"p{row['source_page_start']} title={row['section_title'][:100]}"
            )


def _find_suspicious_rows(outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in outline:
        code = str(row.get("section_code") or "")
        title = str(row.get("section_title") or "")
        page = _to_int(row.get("source_page_start"))
        if page < 30:
            continue
        major = code.split(".")[0] if code else ""
        if major in {"1", "2"} and not code.startswith("5."):
            out.append(row)
            continue
        if any(pattern.search(title) for pattern in SUSPICIOUS_TITLE_PATTERNS):
            if major in {"1", "2"} or code in {"11", "12"}:
                out.append(row)
    return out


def evaluate_preview(preview: dict[str, Any], *, pdf_path: str = "") -> dict[str, Any]:
    outline = preview.get("section_outline", []) or []
    by_code = {str(row.get("section_code") or ""): row for row in outline}

    missing_required = [code for code in DEFAULT_REQUIRED_CODES if code not in by_code]
    suspicious_global = _find_suspicious_rows(outline)
    suspicious_near_guards = _find_suspicious_near_sections(outline, DEFAULT_GUARD_SECTIONS)
    default_candidate_violations = sum(1 for row in outline if not bool(row.get("requirement_like", False)))

    checks: list[dict[str, Any]] = [
        {
            "name": "required_codes_present",
            "passed": len(missing_required) == 0,
            "detail": {"missing_required_codes": missing_required},
        },
        {
            "name": "no_suspicious_global_rows",
            "passed": len(suspicious_global) == 0,
            "detail": {"suspicious_global_rows": suspicious_global[:80]},
        },
        {
            "name": "no_suspicious_rows_near_guard_sections",
            "passed": len(suspicious_near_guards) == 0,
            "detail": {"suspicious_near_guard_sections": suspicious_near_guards[:80]},
        },
        {
            "name": "default_requirement_candidate_is_true",
            "passed": default_candidate_violations == 0,
            "detail": {"non_default_candidate_rows": default_candidate_violations},
        },
    ]

    passed_count = sum(1 for item in checks if item["passed"])
    total_count = len(checks)
    score_pct = round((passed_count / total_count) * 100, 2) if total_count else 0.0

    return {
        "pdf_path": pdf_path,
        "summary": {
            "passed_checks": passed_count,
            "total_checks": total_count,
            "score_pct": score_pct,
            "outline_count": len(outline),
            "requirements_count": int(preview.get("summary", {}).get("requirements_count", 0)),
            "passed": passed_count == total_count,
        },
        "checks": checks,
        "detail": {
            "missing_required": missing_required,
            "suspicious_global": suspicious_global,
            "suspicious_near_guards": suspicious_near_guards,
            "default_candidate_violations": default_candidate_violations,
        },
    }


def _find_suspicious_near_sections(
    outline: list[dict[str, Any]],
    guard_sections: list[str],
) -> list[dict[str, Any]]:
    by_code = {str(row.get("section_code") or ""): row for row in outline}
    out: list[dict[str, Any]] = []
    for guard in guard_sections:
        guard_row = by_code.get(guard)
        if not guard_row:
            continue
        start = _to_int(guard_row.get("source_page_start"))
        end = _to_int(guard_row.get("source_page_end")) or start
        for row in outline:
            page = _to_int(row.get("source_page_start"))
            if page < start - 1 or page > end + 1:
                continue
            code = str(row.get("section_code") or "")
            major = code.split(".")[0] if code else ""
            if major in {"1", "2"} and not code.startswith("5."):
                out.append(
                    {
                        "guard_section": guard,
                        "section_code": code,
                        "section_title": str(row.get("section_title") or ""),
                        "source_page_start": page,
                    }
                )
    return out


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


if __name__ == "__main__":
    main()
