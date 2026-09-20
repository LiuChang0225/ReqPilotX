from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from scripts.preview_snapshot_scope import build_preview_in_isolated_workspace


ITEM_CSV_NAME = "phase0_item_annotation_template.csv"

TABLE_LIKE_TITLE_PATTERNS = [
    re.compile(r"个循环"),
    re.compile(r"test case", re.I),
    re.compile(r"engine/motor", re.I),
    re.compile(r"\bbased on\b", re.I),
    re.compile(r"\bbefore conducting\b", re.I),
]


@dataclass
class Profile:
    name: str
    file_name_regex: str
    required_codes: list[str]
    guard_sections: list[str]


def main() -> None:
    parser = argparse.ArgumentParser(description="Reusable structural quality gate for SOR section splitting.")
    parser.add_argument("pdf_dir", type=Path, help="Directory containing PDF files.")
    parser.add_argument(
        "--profile-config",
        type=Path,
        default=Path("eval/quality_gate_profiles.json"),
        help="Quality gate profile JSON.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/quality_gate/latest"),
        help="Output directory for gate reports.",
    )
    parser.add_argument(
        "--phase0-dir",
        type=Path,
        default=None,
        help="Optional phase0 dir for split metric confidence (contains item CSV).",
    )
    parser.add_argument(
        "--exclude-name-regex",
        action="append",
        default=[],
        help="Exclude PDFs whose file name matches this regex. Can be used multiple times.",
    )
    parser.add_argument(
        "--fail-below-score-pct",
        type=float,
        default=None,
        help="Fail with non-zero exit when gate score percentage is below this threshold.",
    )
    parser.add_argument(
        "--fail-on-doc-score-below-pct",
        type=float,
        default=None,
        help="Fail with non-zero exit when any document score percentage is below this threshold.",
    )
    parser.add_argument(
        "--max-high-severity-issues",
        type=int,
        default=None,
        help="Fail with non-zero exit when total high-severity issues exceed this value.",
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir.expanduser().resolve()
    out_dir = (PROJECT_DIR / args.out_dir).resolve()
    profile_cfg_path = (PROJECT_DIR / args.profile_config).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config = _load_profile_config(profile_cfg_path)
    defaults = config.get("defaults", {})
    profiles = _load_profiles(config.get("profiles", []))

    exclude_patterns = [re.compile(pattern, re.I) for pattern in args.exclude_name_regex]
    pdf_paths = [
        path
        for path in sorted(pdf_dir.glob("*.pdf"))
        if not any(pattern.search(path.name) for pattern in exclude_patterns)
    ]
    if not pdf_paths:
        raise SystemExit(f"No PDF files found: {pdf_dir}")

    doc_rows: list[dict[str, Any]] = []
    issue_rows: list[dict[str, Any]] = []
    checks_total = 0
    checks_passed = 0

    for pdf_path in pdf_paths:
        profile = _match_profile(profiles, pdf_path.name)
        preview = build_preview_in_isolated_workspace(
            pdf_path,
            working_dir=out_dir,
        )
        outline = preview.get("section_outline", []) or []
        requirements = preview.get("complete_requirement_entries", []) or []
        parser_diagnostics = preview.get("parser_diagnostics", {}) or {}

        doc_result = _evaluate_document(
            pdf_name=pdf_path.name,
            outline=outline,
            requirements=requirements,
            parser_diagnostics=parser_diagnostics,
            section_consistency=preview.get("section_consistency", {}) or {},
            defaults=defaults,
            profile=profile,
        )
        checks_total += doc_result["checks_total"]
        checks_passed += doc_result["checks_passed"]
        doc_rows.append(doc_result["doc_row"])
        issue_rows.extend(doc_result["issue_rows"])

    split_conf = _compute_split_metric_confidence(args.phase0_dir) if args.phase0_dir else None

    doc_csv = out_dir / "structural_quality_docs.csv"
    issues_csv = out_dir / "structural_quality_issues.csv"
    summary_md = out_dir / "structural_quality_summary.md"
    summary_json = out_dir / "structural_quality_summary.json"

    _write_csv(doc_csv, doc_rows)
    _write_csv(issues_csv, issue_rows)

    gate_score_pct = round((checks_passed / checks_total) * 100, 2) if checks_total else 0.0
    high_severity_issues = sum(1 for row in issue_rows if str(row.get("severity", "")).strip().lower() == "high")
    doc_score_failures: list[str] = []
    if args.fail_on_doc_score_below_pct is not None:
        threshold = float(args.fail_on_doc_score_below_pct)
        doc_score_failures = [
            str(row.get("pdf_name", ""))
            for row in doc_rows
            if float(row.get("score_pct", 0.0)) < threshold
        ]

    gate_failure_reasons: list[str] = []
    if args.fail_below_score_pct is not None and gate_score_pct < float(args.fail_below_score_pct):
        gate_failure_reasons.append(
            f"gate_score_pct={gate_score_pct:.2f} < {float(args.fail_below_score_pct):.2f}"
        )
    if args.max_high_severity_issues is not None and high_severity_issues > int(args.max_high_severity_issues):
        gate_failure_reasons.append(
            f"high_severity_issues={high_severity_issues} > {int(args.max_high_severity_issues)}"
        )
    if doc_score_failures:
        gate_failure_reasons.append(
            "doc_score_below_threshold=" + ",".join(doc_score_failures)
        )

    gate_pass = len(gate_failure_reasons) == 0
    summary_payload = {
        "pdf_dir": str(pdf_dir),
        "docs": len(doc_rows),
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "gate_score_pct": gate_score_pct,
        "high_severity_issues": high_severity_issues,
        "gate_pass": gate_pass,
        "gate_failure_reasons": gate_failure_reasons,
        "split_metric_confidence": split_conf,
    }
    summary_json.write_text(json.dumps(summary_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(
        _render_summary_md(summary_payload, doc_rows, issue_rows),
        encoding="utf-8",
    )

    print(f"[quality-gate] docs: {len(doc_rows)}")
    print(f"[quality-gate] checks: {checks_passed}/{checks_total} ({gate_score_pct:.2f}%)")
    print(f"[quality-gate] high severity issues: {high_severity_issues}")
    print(f"[quality-gate] gate pass: {gate_pass}")
    if gate_failure_reasons:
        print(f"[quality-gate] gate failure reasons: {' | '.join(gate_failure_reasons)}")
    print(f"[quality-gate] doc csv: {doc_csv}")
    print(f"[quality-gate] issues csv: {issues_csv}")
    print(f"[quality-gate] summary md: {summary_md}")
    if split_conf:
        print(
            "[quality-gate] split confidence "
            f"p={split_conf['point_estimate']:.4f}, n={split_conf['n']}, "
            f"95%CI=({split_conf['wilson_low']:.4f}, {split_conf['wilson_high']:.4f})"
        )
    if not gate_pass:
        raise SystemExit(1)


def _evaluate_document(
    pdf_name: str,
    outline: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    parser_diagnostics: dict[str, Any],
    section_consistency: dict[str, Any] | None,
    defaults: dict[str, Any],
    profile: Profile | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    checks: list[tuple[str, bool, str]] = []

    max_duplicate_code_count = int(defaults.get("max_duplicate_code_count", 2))
    max_suspicious_heading_ratio = float(defaults.get("max_suspicious_heading_ratio", 0.03))
    max_sibling_gap_alerts = int(defaults.get("max_sibling_gap_alerts", 3))
    late_low_major_page_threshold = int(defaults.get("late_low_major_page_threshold", 30))
    min_outline_count = int(defaults.get("min_outline_count", 20))
    require_strict_merge_mode = bool(defaults.get("require_strict_merge_mode", True))
    max_line_injection_count = int(defaults.get("max_line_injection_count", 0))
    max_line_reorder_count = int(defaults.get("max_line_reorder_count", 0))

    merge_mode = str(parser_diagnostics.get("merge_mode") or "")
    injection_disabled = bool(parser_diagnostics.get("injection_disabled", False))
    line_injection_count = _to_int(parser_diagnostics.get("line_injection_count"))
    line_reorder_count = _to_int(parser_diagnostics.get("line_reorder_count"))
    order_integrity_ok = bool(parser_diagnostics.get("order_integrity_ok", True))

    if require_strict_merge_mode:
        strict_ok = merge_mode == "strict_single_engine" and injection_disabled
        checks.append(
            (
                "strict_merge_mode",
                strict_ok,
                f"merge_mode={merge_mode} injection_disabled={injection_disabled}",
            )
        )
        if not strict_ok:
            issues.append(
                {
                    "pdf_name": pdf_name,
                    "issue_type": "strict_merge_mode_violation",
                    "severity": "high",
                    "appendix_scope": "",
                    "section_code": "",
                    "detail": f"merge_mode={merge_mode} injection_disabled={injection_disabled}",
                }
            )

    checks.append(
        (
            "line_injection_count_limit",
            line_injection_count <= max_line_injection_count,
            f"line_injection_count={line_injection_count} max={max_line_injection_count}",
        )
    )
    if line_injection_count > max_line_injection_count:
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "line_injection_count_limit",
                "severity": "high",
                "appendix_scope": "",
                "section_code": "",
                "detail": f"line_injection_count={line_injection_count} max={max_line_injection_count}",
            }
        )

    checks.append(
        (
            "line_reorder_count_limit",
            line_reorder_count <= max_line_reorder_count,
            f"line_reorder_count={line_reorder_count} max={max_line_reorder_count}",
        )
    )
    if line_reorder_count > max_line_reorder_count:
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "line_reorder_count_limit",
                "severity": "high",
                "appendix_scope": "",
                "section_code": "",
                "detail": f"line_reorder_count={line_reorder_count} max={max_line_reorder_count}",
            }
        )

    checks.append(("order_integrity_ok", order_integrity_ok, f"order_integrity_ok={order_integrity_ok}"))
    if not order_integrity_ok:
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "order_integrity_violation",
                "severity": "high",
                "appendix_scope": "",
                "section_code": "",
                "detail": f"order_integrity_ok={order_integrity_ok}",
            }
        )

    outline_count = len(outline)
    checks.append(("outline_count_min", outline_count >= min_outline_count, f"outline_count={outline_count}"))

    duplicate_codes: dict[str, int] = {}
    duplicate_issue_by_scoped_code: dict[str, dict[str, Any]] = {}
    consistency_payload = section_consistency or {}
    if consistency_payload:
        for row in consistency_payload.get("issues", []) or []:
            if str(row.get("issue_type") or "").strip() != "duplicate_code":
                continue
            scoped_code = _join_scope_code(str(row.get("appendix_scope") or ""), str(row.get("section_code") or ""))
            if not scoped_code:
                continue
            duplicate_issue_by_scoped_code[scoped_code] = dict(row)
            duplicate_codes[scoped_code] = _extract_duplicate_count(str(row.get("detail") or ""))
    else:
        scoped_code_counts = Counter(
            _join_scope_code(_row_scope(row), str(row.get("section_code") or ""))
            for row in outline
            if row.get("section_code")
        )
        duplicate_codes = {code: count for code, count in scoped_code_counts.items() if count > 1}
    checks.append(
        (
            "duplicate_code_limit",
            len(duplicate_codes) <= max_duplicate_code_count,
            f"duplicate_codes={len(duplicate_codes)}",
        )
    )
    for scoped_code, count in sorted(duplicate_codes.items()):
        appendix_scope, code = _split_scope_code(scoped_code)
        issue_row = duplicate_issue_by_scoped_code.get(scoped_code, {})
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "duplicate_section_code",
                "severity": str(issue_row.get("severity") or ("high" if count >= 3 else "medium")).lower(),
                "appendix_scope": appendix_scope,
                "section_code": code,
                "detail": str(issue_row.get("detail") or f"duplicate_count={count}"),
            }
        )

    suspicious_rows = _find_suspicious_heading_rows(outline, late_low_major_page_threshold)
    suspicious_ratio = (len(suspicious_rows) / outline_count) if outline_count else 1.0
    checks.append(
        (
            "suspicious_heading_ratio_limit",
            suspicious_ratio <= max_suspicious_heading_ratio,
            f"suspicious_ratio={suspicious_ratio:.4f}",
        )
    )
    for row in suspicious_rows:
        reason = str(row.get("reason") or "")
        severity = "medium"
        if reason in {"late_low_major", "enum_promoted_heading"}:
            severity = "high"
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "suspicious_heading",
                "severity": severity,
                "appendix_scope": row.get("appendix_scope", ""),
                "section_code": row.get("section_code", ""),
                "detail": reason,
            }
        )

    gap_alerts = _find_sibling_gap_alerts(outline)
    checks.append(
        (
            "sibling_gap_alert_limit",
            len(gap_alerts) <= max_sibling_gap_alerts,
            f"sibling_gap_alerts={len(gap_alerts)}",
        )
    )
    for alert in gap_alerts:
        issues.append(
            {
                "pdf_name": pdf_name,
                "issue_type": "sibling_gap_alert",
                "severity": "medium",
                "appendix_scope": alert.get("appendix_scope", ""),
                "section_code": alert["parent_code"],
                "detail": f"missing={alert['missing_codes'][:12]}",
            }
        )

    required_missing: list[str] = []
    guard_violations: list[dict[str, Any]] = []
    if profile:
        required_missing = _missing_required_codes(outline, requirements, profile.required_codes)
        checks.append(
            (
                "profile_required_codes_present",
                len(required_missing) == 0,
                f"missing_required={len(required_missing)}",
            )
        )
        for scoped_required in required_missing:
            appendix_scope, required_code = _split_scope_code(scoped_required)
            issues.append(
                {
                    "pdf_name": pdf_name,
                    "issue_type": "required_code_missing",
                    "severity": "high",
                    "appendix_scope": appendix_scope,
                    "section_code": required_code,
                    "detail": f"profile={profile.name}",
                }
            )

        guard_violations = _guard_section_violations(outline, profile.guard_sections, late_low_major_page_threshold)
        checks.append(
            (
                "profile_guard_sections_clean",
                len(guard_violations) == 0,
                f"guard_violations={len(guard_violations)}",
            )
        )
        for row in guard_violations:
            issues.append(
                {
                    "pdf_name": pdf_name,
                    "issue_type": "guard_section_contamination",
                    "severity": "high",
                    "appendix_scope": row.get("appendix_scope", ""),
                    "section_code": row.get("section_code", ""),
                    "detail": f"guard={row.get('guard_section', '')}",
                }
            )

    checks_total = len(checks)
    checks_passed = sum(1 for _, ok, _ in checks if ok)
    score_pct = round((checks_passed / checks_total) * 100, 2) if checks_total else 0.0

    doc_row = {
        "pdf_name": pdf_name,
        "profile": profile.name if profile else "",
        "outline_count": outline_count,
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "score_pct": score_pct,
        "duplicate_code_count": len(duplicate_codes),
        "source_duplicate_code_count": int(consistency_payload.get("source_duplicate_code_count") or 0),
        "suspicious_heading_count": len(suspicious_rows),
        "suspicious_heading_ratio": round(suspicious_ratio, 6),
        "sibling_gap_alert_count": len(gap_alerts),
        "required_missing_count": len(required_missing),
        "guard_violation_count": len(guard_violations),
        "merge_mode": merge_mode,
        "line_injection_count": line_injection_count,
        "line_reorder_count": line_reorder_count,
        "order_integrity_ok": int(order_integrity_ok),
    }
    for name, ok, detail in checks:
        doc_row[f"check_{name}"] = int(bool(ok))
        doc_row[f"detail_{name}"] = detail

    return {
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "doc_row": doc_row,
        "issue_rows": issues,
    }


def _find_suspicious_heading_rows(
    outline: list[dict[str, Any]],
    late_low_major_page_threshold: int,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in outline:
        code = str(row.get("section_code") or "")
        appendix_scope = _row_scope(row)
        title = str(row.get("section_title") or "")
        page = _to_int(row.get("source_page_start"))
        major = code.split(".")[0] if code else ""

        reason = ""
        if (
            not appendix_scope
            and page >= late_low_major_page_threshold
            and major in {"1", "2"}
            and not code.startswith("5.")
        ):
            reason = "late_low_major"
        elif any(pattern.search(title) for pattern in TABLE_LIKE_TITLE_PATTERNS):
            reason = "table_like_title"
        elif (
            _section_level(code) >= 3
            and re.match(r"^\s*[（(]", title)
            and page > 0
        ):
            reason = "enum_promoted_heading"
        elif _is_noisy_title(title):
            reason = "noisy_title"

        if reason:
            out.append(
                {
                    "appendix_scope": appendix_scope,
                    "section_code": code,
                    "section_title": title,
                    "source_page_start": page,
                    "reason": reason,
                }
            )
    return out


def _find_sibling_gap_alerts(outline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    child_map: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in outline:
        code = str(row.get("section_code") or "")
        appendix_scope = _row_scope(row)
        parts = _code_parts(code)
        if len(parts) < 2:
            continue
        parent = ".".join(str(p) for p in parts[:-1])
        child_map[(appendix_scope, parent)].add(parts[-1])

    alerts: list[dict[str, Any]] = []
    for (appendix_scope, parent), nums in child_map.items():
        if len(nums) < 6:
            continue
        lo = min(nums)
        hi = max(nums)
        expected = set(range(lo, hi + 1))
        missing = sorted(expected - nums)
        if len(missing) < 3:
            continue
        gap_ratio = len(missing) / len(expected) if expected else 0.0
        if gap_ratio < 0.2:
            continue
        alerts.append(
            {
                "appendix_scope": appendix_scope,
                "parent_code": parent,
                "missing_count": len(missing),
                "missing_codes": [f"{parent}.{n}" for n in missing],
                "gap_ratio": round(gap_ratio, 4),
            }
        )
    return alerts


def _missing_required_codes(
    outline: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    required_codes: list[str],
) -> list[str]:
    seen_codes = _collect_scoped_codes(outline)
    seen_codes.update(_collect_scoped_codes(requirements))
    missing: list[str] = []
    for code in required_codes:
        scope, raw_code = _split_scope_code(code)
        scoped_required = _join_scope_code(scope, raw_code)
        if scoped_required and scoped_required not in seen_codes:
            missing.append(scoped_required)
    return missing


def _collect_scoped_codes(rows: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        code = str(row.get("section_code") or "").strip()
        if not code:
            continue
        out.add(_join_scope_code(_row_scope(row), code))
    return out


def _guard_section_violations(
    outline: list[dict[str, Any]],
    guard_sections: list[str],
    late_low_major_page_threshold: int,
) -> list[dict[str, Any]]:
    by_code = {
        _join_scope_code(_row_scope(row), str(row.get("section_code") or "")): row
        for row in outline
        if row.get("section_code")
    }
    out: list[dict[str, Any]] = []
    for guard in guard_sections:
        guard_scope, guard_code = _split_scope_code(guard)
        guard_key = _join_scope_code(guard_scope, guard_code)
        guard_row = by_code.get(guard_key)
        if not guard_row:
            continue
        start = _to_int(guard_row.get("source_page_start"))
        end = _to_int(guard_row.get("source_page_end")) or start
        for row in outline:
            page = _to_int(row.get("source_page_start"))
            if page < start - 1 or page > end + 1:
                continue
            code = str(row.get("section_code") or "")
            row_scope = _row_scope(row)
            if row_scope != guard_scope:
                continue
            if code == guard_code:
                continue
            major = code.split(".")[0] if code else ""
            if (
                not row_scope
                and page >= late_low_major_page_threshold
                and major in {"1", "2"}
                and not code.startswith("5.")
            ):
                out.append(
                    {
                        "appendix_scope": row_scope,
                        "guard_section": guard_key,
                        "section_code": code,
                        "section_title": str(row.get("section_title") or ""),
                        "source_page_start": page,
                    }
                )
    return out


def _is_noisy_title(title: str) -> bool:
    value = str(title or "").strip()
    if not value:
        return True
    if re.search(r"[\u4e00-\u9fff]{2,}", value):
        return False
    if re.search(r"[A-Za-z]{3,}", value):
        return False
    tokens = re.findall(r"[A-Za-z\u4e00-\u9fff0-9]+", value)
    if len(tokens) >= 6:
        single_tokens = sum(1 for token in tokens if len(token) == 1)
        if single_tokens >= int(len(tokens) * 0.7):
            return True
    return False


def _compute_split_metric_confidence(phase0_dir: Path | None) -> dict[str, Any] | None:
    if not phase0_dir:
        return None
    phase0 = phase0_dir.expanduser().resolve()
    item_csv = phase0 / ITEM_CSV_NAME
    if not item_csv.exists():
        return None

    rows = _read_csv(item_csv)
    labeled = [
        row
        for row in rows
        if row.get("source_type") == "predicted" and _is_binary_label(row.get("is_split_correct", ""))
    ]
    n = len(labeled)
    if n == 0:
        return None
    k = sum(1 for row in labeled if _to_bool(row.get("is_split_correct", "")))
    p = k / n
    low, high = _wilson_interval(k, n, z=1.96)
    return {
        "n": n,
        "k": k,
        "point_estimate": p,
        "wilson_low": low,
        "wilson_high": high,
    }


def _wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    z2 = z * z
    den = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / den
    spread = (z * math.sqrt((p * (1 - p) + z2 / (4 * n)) / n)) / den
    return max(0.0, center - spread), min(1.0, center + spread)


def _render_summary_md(summary: dict[str, Any], docs: list[dict[str, Any]], issues: list[dict[str, Any]]) -> str:
    lines = [
        "# Structural Quality Gate",
        "",
        f"- PDF dir: `{summary['pdf_dir']}`",
        f"- Documents: `{summary['docs']}`",
        f"- Checks passed: `{summary['checks_passed']}/{summary['checks_total']}`",
        f"- Gate score: `{summary['gate_score_pct']:.2f}%`",
        f"- High severity issues: `{summary.get('high_severity_issues', 0)}`",
        f"- Gate pass: `{summary.get('gate_pass', True)}`",
    ]
    reasons = summary.get("gate_failure_reasons", []) or []
    if reasons:
        lines.append("")
        lines.append("## Gate Failure Reasons")
        for reason in reasons:
            lines.append(f"- `{reason}`")

    lines.append("")
    lines.append("## Document Scores")
    for row in sorted(docs, key=lambda item: (float(item.get("score_pct", 0.0)), item.get("pdf_name", ""))):
        lines.append(
            f"- `{row['pdf_name']}`: score `{row['score_pct']}%`, "
            f"outline `{row['outline_count']}`, suspicious `{row['suspicious_heading_count']}`, "
            f"gap alerts `{row['sibling_gap_alert_count']}`"
        )

    lines.append("")
    lines.append("## Top Issues")
    if not issues:
        lines.append("- None")
    else:
        for row in issues[:30]:
            lines.append(
                f"- `{row.get('pdf_name', '')}` | `{row.get('issue_type', '')}` | "
                f"`{row.get('section_code', '')}` | {row.get('detail', '')}"
            )

    split_conf = summary.get("split_metric_confidence")
    if split_conf:
        lines.append("")
        lines.append("## Split Confidence")
        lines.append(
            f"- Point estimate: `{split_conf['point_estimate']:.2%}` with `n={split_conf['n']}` labeled split rows"
        )
        lines.append(
            f"- Wilson 95% CI: `{split_conf['wilson_low']:.2%} ~ {split_conf['wilson_high']:.2%}`"
        )
    return "\n".join(lines) + "\n"


def _load_profile_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _load_profiles(payload: list[dict[str, Any]]) -> list[Profile]:
    out: list[Profile] = []
    for item in payload:
        out.append(
            Profile(
                name=str(item.get("name", "")),
                file_name_regex=str(item.get("file_name_regex", "")),
                required_codes=[str(code) for code in item.get("required_codes", [])],
                guard_sections=[str(code) for code in item.get("guard_sections", [])],
            )
        )
    return out


def _match_profile(profiles: list[Profile], file_name: str) -> Profile | None:
    for profile in profiles:
        if profile.file_name_regex and re.search(profile.file_name_regex, file_name, re.I):
            return profile
    return None


def _normalize_scope(scope: str) -> str:
    token = str(scope or "").strip().upper()
    if token in {"MAIN", "ROOT", "NONE", "-"}:
        return ""
    return token


def _row_scope(row: dict[str, Any]) -> str:
    return _normalize_scope(str(row.get("appendix_scope") or ""))


def _join_scope_code(scope: str, code: str) -> str:
    normalized_scope = _normalize_scope(scope)
    normalized_code = str(code or "").strip()
    if not normalized_code:
        return ""
    return f"{normalized_scope}:{normalized_code}" if normalized_scope else normalized_code


def _split_scope_code(value: str) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return ("", "")
    if ":" not in text:
        return ("", text)
    left, right = text.split(":", 1)
    return (_normalize_scope(left), right.strip())


def _extract_duplicate_count(detail: str) -> int:
    match = re.search(r"duplicate_count=(\d+)", str(detail or ""))
    if not match:
        return 2
    return max(int(match.group(1)), 2)


def _code_parts(code: str) -> list[int]:
    out: list[int] = []
    for token in str(code or "").split("."):
        if not token:
            return []
        try:
            out.append(int(token))
        except ValueError:
            return []
    return out


def _section_level(code: str) -> int:
    parts = _code_parts(code)
    return len(parts)


def _is_binary_label(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"0", "1", "true", "false", "y", "n", "yes", "no"}


def _to_bool(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "y", "yes"}


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8-sig") as fp:
        return list(csv.DictReader(fp))


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
