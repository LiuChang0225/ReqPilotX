from __future__ import annotations

# Install the repository import root before application imports.
# ruff: noqa: E402

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.services.extraction_preview import counts_toward_fidelity_section
from scripts.preview_snapshot_scope import build_preview_in_isolated_workspace


LIST_STYLE_HEADING_RE = re.compile(r"^\s*\d+\s*[)）]")
LEADING_PUNCT_HEADING_RE = re.compile(r"^\s*[)）,，;；:：]")


@dataclass
class FidelityProfile:
    name: str
    file_name_regex: str
    min_sections_count: int = 0
    min_requirements_count: int = 0
    expected_sections_count: int | None = None
    max_section_count_drift: int | None = None
    expected_requirements_count: int | None = None
    max_requirement_count_drift: int | None = None
    max_section_consistency_issues: int = 999
    required_scoped_codes: list[str] = field(default_factory=list)
    forbidden_scoped_codes: list[str] = field(default_factory=list)
    required_appendix_scopes: list[str] = field(default_factory=list)
    min_appendix_marker_count: int = 0
    required_title_contains: dict[str, str] = field(default_factory=dict)
    required_requirement_text_contains: dict[str, str] = field(default_factory=dict)
    required_requirement_ordered_snippets: dict[str, list[str]] = field(default_factory=dict)
    required_table_row_snippets: dict[str, list[str]] = field(default_factory=dict)
    required_visual_asset_codes: list[str] = field(default_factory=list)
    required_tree_children: dict[str, list[str]] = field(default_factory=dict)
    forbidden_cross_section_snippets: list[dict[str, Any]] = field(default_factory=list)
    required_single_requirement_codes: list[str] = field(default_factory=list)
    required_duplicate_code_titles: dict[str, list[str]] = field(default_factory=dict)
    forbidden_requirement_text_contains: dict[str, str] = field(default_factory=dict)
    max_list_style_heading_count: int = 0
    max_leading_punct_heading_count: int = 0
    max_short_deep_heading_count: int = 0
    require_strict_merge_mode: bool = False
    max_supplemental_heading_total: int | None = None
    max_table_block_injected_total: int | None = None
    max_line_injection_count: int | None = None
    max_line_reorder_count: int | None = None
    require_order_integrity_ok: bool = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Profile-based fidelity benchmark for document extraction.")
    parser.add_argument("pdf_dir", type=Path, help="Directory containing PDFs to benchmark.")
    parser.add_argument(
        "--profile-config",
        type=Path,
        default=Path("eval/fidelity_profiles.json"),
        help="Fidelity profile JSON path.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/fidelity/latest"),
        help="Output directory for benchmark artifacts.",
    )
    parser.add_argument(
        "--exclude-name-regex",
        action="append",
        default=[],
        help="Exclude PDFs whose file name matches this regex. Can be used multiple times.",
    )
    parser.add_argument(
        "--allow-missing-profiles",
        action="store_true",
        help="Do not fail when a configured profile does not match any document in pdf_dir.",
    )
    args = parser.parse_args()

    pdf_dir = args.pdf_dir.expanduser().resolve()
    out_dir = (PROJECT_DIR / args.out_dir).resolve()
    cfg_path = (PROJECT_DIR / args.profile_config).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config = _load_json(cfg_path)
    defaults = config.get("defaults", {}) if isinstance(config, dict) else {}
    profiles = _load_profiles(config.get("profiles", []), defaults) if isinstance(config, dict) else []
    if not profiles:
        raise SystemExit(f"No profiles found in: {cfg_path}")

    exclude_patterns = [re.compile(pattern, re.I) for pattern in args.exclude_name_regex]
    pdf_paths = [
        path
        for path in sorted(pdf_dir.glob("*.pdf"))
        if not any(pattern.search(path.name) for pattern in exclude_patterns)
    ]
    if not pdf_paths:
        raise SystemExit(f"No PDF files found: {pdf_dir}")

    selected_docs: list[tuple[Path, FidelityProfile]] = []
    skipped_docs: list[str] = []
    matched_profile_names: set[str] = set()
    for pdf_path in pdf_paths:
        profile = _match_profile(profiles, pdf_path.name)
        if not profile:
            skipped_docs.append(pdf_path.name)
            continue
        selected_docs.append((pdf_path, profile))
        matched_profile_names.add(profile.name)

    if not selected_docs:
        raise SystemExit("No benchmark target documents matched configured profiles.")

    doc_rows: list[dict[str, Any]] = []
    issue_rows: list[dict[str, Any]] = []
    checks_total = 0
    checks_passed = 0
    failed_docs = 0

    for pdf_path, profile in selected_docs:
        result = _evaluate_document(
            pdf_path,
            profile,
            snapshot_work_dir=out_dir,
        )
        doc_rows.append(result["doc_row"])
        issue_rows.extend(result["issue_rows"])
        checks_total += result["checks_total"]
        checks_passed += result["checks_passed"]
        if not result["passed"]:
            failed_docs += 1

    profile_names = {profile.name for profile in profiles}
    missing_profiles = sorted(profile_names - matched_profile_names)
    missing_profiles_fail = bool(missing_profiles) and (not args.allow_missing_profiles)

    score_pct = round((checks_passed / checks_total) * 100, 2) if checks_total else 0.0
    overall_pass = (failed_docs == 0) and (not missing_profiles_fail)

    summary = {
        "pdf_dir": str(pdf_dir),
        "target_docs": len(selected_docs),
        "skipped_docs_count": len(skipped_docs),
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "score_pct": score_pct,
        "failed_docs": failed_docs,
        "matched_profiles": sorted(matched_profile_names),
        "missing_profiles": missing_profiles,
        "overall_pass": overall_pass,
    }

    docs_csv = out_dir / "fidelity_docs.csv"
    issues_csv = out_dir / "fidelity_issues.csv"
    summary_json = out_dir / "fidelity_summary.json"
    summary_md = out_dir / "fidelity_summary.md"

    _write_csv(docs_csv, doc_rows)
    _write_csv(issues_csv, issue_rows)
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(_render_summary_md(summary, doc_rows, issue_rows, skipped_docs), encoding="utf-8")

    print(f"[fidelity] target docs: {len(selected_docs)}")
    print(f"[fidelity] checks: {checks_passed}/{checks_total} ({score_pct:.2f}%)")
    print(f"[fidelity] failed docs: {failed_docs}")
    if missing_profiles:
        print(f"[fidelity] missing profiles: {', '.join(missing_profiles)}")
    if skipped_docs:
        print(f"[fidelity] skipped docs: {len(skipped_docs)}")
    print(f"[fidelity] docs csv: {docs_csv}")
    print(f"[fidelity] issues csv: {issues_csv}")
    print(f"[fidelity] summary md: {summary_md}")

    if not overall_pass:
        raise SystemExit(1)


def _evaluate_document(
    pdf_path: Path,
    profile: FidelityProfile,
    *,
    snapshot_work_dir: Path,
) -> dict[str, Any]:
    preview = build_preview_in_isolated_workspace(
        pdf_path,
        working_dir=snapshot_work_dir,
    )
    summary = preview.get("summary", {}) if isinstance(preview, dict) else {}
    parser_diagnostics = preview.get("parser_diagnostics", {}) if isinstance(preview, dict) else {}
    all_sections = preview.get("sections", []) if isinstance(preview, dict) else []
    requirements = preview.get("requirements", []) if isinstance(preview, dict) else []
    complete_entries = preview.get("complete_requirement_entries", []) if isinstance(preview, dict) else []
    appendix_markers = preview.get("appendix_markers", []) if isinstance(preview, dict) else []
    test_outline_markers = preview.get("test_outline_markers", []) if isinstance(preview, dict) else []
    sections = [
        row
        for row in all_sections
        if str(row.get("section_code") or "").strip()
    ]
    countable_sections = [row for row in sections if counts_toward_fidelity_section(row)]

    requirements_count = _to_int(summary.get("requirements_count"))
    sections_count = len(countable_sections)
    section_consistency_issues = _to_int(summary.get("section_consistency_issues_total"))
    appendix_marker_count = _to_int(summary.get("appendix_marker_count")) + _to_int(
        summary.get("test_outline_marker_count")
    )
    supplemental_heading_total = _to_int(parser_diagnostics.get("supplemental_heading_total"))
    table_block_injected_total = _to_int(parser_diagnostics.get("table_block_injected_total"))
    line_injection_count = _to_int(parser_diagnostics.get("line_injection_count"))
    line_reorder_count = _to_int(parser_diagnostics.get("line_reorder_count"))
    merge_mode = str(parser_diagnostics.get("merge_mode") or "")
    order_integrity_ok = bool(parser_diagnostics.get("order_integrity_ok", True))

    scoped_index: dict[str, dict[str, Any]] = {}
    appendix_scopes: set[str] = set()
    for row in sections:
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        code = str(row.get("section_code") or "").strip()
        key = _join_scope_code(scope, code)
        if not key:
            continue
        if key not in scoped_index:
            scoped_index[key] = row
        if scope:
            appendix_scopes.add(scope)
    for row in all_sections:
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        if scope:
            appendix_scopes.add(scope)
    for marker in appendix_markers:
        scope = _normalize_scope(str((marker or {}).get("appendix_scope") or ""))
        if scope:
            appendix_scopes.add(scope)
    for marker in test_outline_markers:
        scope = _normalize_scope(str((marker or {}).get("appendix_scope") or ""))
        if scope:
            appendix_scopes.add(scope)
    scoped_keys = set(scoped_index.keys())
    requirement_text_by_scoped_code: dict[str, str] = {}
    requirement_rows_by_scoped_code: dict[str, list[dict[str, Any]]] = {}
    requirement_titles_by_scoped_code: dict[str, set[str]] = {}
    visual_asset_counts_by_scoped_code: dict[str, int] = {}
    tree_child_codes_by_parent = _collect_tree_child_codes(sections)
    for row in requirements:
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        code = str(row.get("section_code") or "").strip()
        scoped_code = _join_scope_code(scope, code)
        if not scoped_code:
            continue
        text = str(row.get("requirement_text") or "").strip()
        requirement_rows_by_scoped_code.setdefault(scoped_code, []).append(row)
        title_key = _normalize_match_text(_requirement_title_from_row(row))
        if title_key:
            requirement_titles_by_scoped_code.setdefault(scoped_code, set()).add(title_key)
        if not text:
            continue
        if scoped_code not in requirement_text_by_scoped_code:
            requirement_text_by_scoped_code[scoped_code] = text
            continue
        merged = requirement_text_by_scoped_code[scoped_code]
        if text not in merged:
            requirement_text_by_scoped_code[scoped_code] = f"{merged}\n{text}".strip()
    for row in complete_entries:
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        code = str(row.get("section_code") or "").strip()
        scoped_code = _join_scope_code(scope, code)
        if not scoped_code:
            continue
        refs = row.get("table_refs") or []
        visual_asset_counts_by_scoped_code[scoped_code] = (
            visual_asset_counts_by_scoped_code.get(scoped_code, 0)
            + sum(
                1
                for ref in refs
                if str(ref.get("source_snapshot_path") or ref.get("slice_snapshot_path") or "").strip()
            )
        )

    list_style_heading_count = sum(
        1 for row in sections if LIST_STYLE_HEADING_RE.search(str(row.get("section_title") or ""))
    )
    leading_punct_heading_count = sum(
        1 for row in sections if LEADING_PUNCT_HEADING_RE.search(str(row.get("section_title") or ""))
    )
    short_deep_heading_count = sum(
        1
        for row in sections
        if _section_level(str(row.get("section_code") or "")) >= 4 and len(str(row.get("section_title") or "").strip()) <= 4
    )

    checks: list[tuple[str, bool, str]] = []
    issues: list[dict[str, Any]] = []

    def add_check(name: str, passed: bool, detail: str) -> None:
        checks.append((name, passed, detail))

    def add_issue(issue_type: str, severity: str, scoped_code: str, detail: str) -> None:
        scope, code = _split_scope_code(scoped_code)
        issues.append(
            {
                "pdf_name": pdf_path.name,
                "profile": profile.name,
                "issue_type": issue_type,
                "severity": severity,
                "appendix_scope": scope,
                "section_code": code,
                "detail": detail,
            }
        )

    if profile.require_strict_merge_mode:
        strict_merge_ok = merge_mode == "strict_single_engine" and bool(
            parser_diagnostics.get("injection_disabled", False)
        )
        add_check(
            "strict_merge_mode",
            strict_merge_ok,
            f"merge_mode={merge_mode} injection_disabled={bool(parser_diagnostics.get('injection_disabled', False))}",
        )
        if not strict_merge_ok:
            add_issue(
                "strict_merge_mode_violation",
                "high",
                "",
                f"merge_mode={merge_mode} injection_disabled={bool(parser_diagnostics.get('injection_disabled', False))}",
            )

    if profile.max_supplemental_heading_total is not None:
        passed = supplemental_heading_total <= int(profile.max_supplemental_heading_total)
        add_check(
            "supplemental_heading_total_limit",
            passed,
            f"supplemental_heading_total={supplemental_heading_total} max={int(profile.max_supplemental_heading_total)}",
        )
        if not passed:
            add_issue(
                "supplemental_heading_total_limit",
                "high",
                "",
                f"supplemental_heading_total={supplemental_heading_total} max={int(profile.max_supplemental_heading_total)}",
            )

    if profile.max_table_block_injected_total is not None:
        passed = table_block_injected_total <= int(profile.max_table_block_injected_total)
        add_check(
            "table_block_injected_total_limit",
            passed,
            f"table_block_injected_total={table_block_injected_total} max={int(profile.max_table_block_injected_total)}",
        )
        if not passed:
            add_issue(
                "table_block_injected_total_limit",
                "high",
                "",
                f"table_block_injected_total={table_block_injected_total} max={int(profile.max_table_block_injected_total)}",
            )

    if profile.max_line_injection_count is not None:
        passed = line_injection_count <= int(profile.max_line_injection_count)
        add_check(
            "line_injection_count_limit",
            passed,
            f"line_injection_count={line_injection_count} max={int(profile.max_line_injection_count)}",
        )
        if not passed:
            add_issue(
                "line_injection_count_limit",
                "high",
                "",
                f"line_injection_count={line_injection_count} max={int(profile.max_line_injection_count)}",
            )

    if profile.max_line_reorder_count is not None:
        passed = line_reorder_count <= int(profile.max_line_reorder_count)
        add_check(
            "line_reorder_count_limit",
            passed,
            f"line_reorder_count={line_reorder_count} max={int(profile.max_line_reorder_count)}",
        )
        if not passed:
            add_issue(
                "line_reorder_count_limit",
                "high",
                "",
                f"line_reorder_count={line_reorder_count} max={int(profile.max_line_reorder_count)}",
            )

    if profile.require_order_integrity_ok:
        add_check(
            "order_integrity_ok",
            order_integrity_ok,
            f"order_integrity_ok={order_integrity_ok}",
        )
        if not order_integrity_ok:
            add_issue(
                "order_integrity_violation",
                "high",
                "",
                f"order_integrity_ok={order_integrity_ok}",
            )

    add_check(
        "min_sections_count",
        sections_count >= profile.min_sections_count,
        f"sections={sections_count} min={profile.min_sections_count}",
    )
    if sections_count < profile.min_sections_count:
        add_issue("section_count_too_low", "high", "", f"sections={sections_count} min={profile.min_sections_count}")

    add_check(
        "min_requirements_count",
        requirements_count >= profile.min_requirements_count,
        f"requirements={requirements_count} min={profile.min_requirements_count}",
    )
    if requirements_count < profile.min_requirements_count:
        add_issue(
            "requirement_count_too_low",
            "high",
            "",
            f"requirements={requirements_count} min={profile.min_requirements_count}",
        )

    if profile.expected_sections_count is not None and profile.max_section_count_drift is not None:
        drift = abs(sections_count - profile.expected_sections_count)
        passed = drift <= profile.max_section_count_drift
        add_check(
            "sections_count_drift",
            passed,
            f"sections={sections_count} expected={profile.expected_sections_count} drift={drift}",
        )
        if not passed:
            add_issue(
                "section_count_drift",
                "medium",
                "",
                f"sections={sections_count} expected={profile.expected_sections_count} drift={drift}",
            )

    if profile.expected_requirements_count is not None and profile.max_requirement_count_drift is not None:
        drift = abs(requirements_count - profile.expected_requirements_count)
        passed = drift <= profile.max_requirement_count_drift
        add_check(
            "requirements_count_drift",
            passed,
            f"requirements={requirements_count} expected={profile.expected_requirements_count} drift={drift}",
        )
        if not passed:
            add_issue(
                "requirement_count_drift",
                "medium",
                "",
                f"requirements={requirements_count} expected={profile.expected_requirements_count} drift={drift}",
            )

    add_check(
        "section_consistency_issue_limit",
        section_consistency_issues <= profile.max_section_consistency_issues,
        f"consistency_issues={section_consistency_issues} max={profile.max_section_consistency_issues}",
    )
    if section_consistency_issues > profile.max_section_consistency_issues:
        add_issue(
            "section_consistency_issue_limit",
            "medium",
            "",
            f"consistency_issues={section_consistency_issues} max={profile.max_section_consistency_issues}",
        )

    missing_required: list[str] = []
    for raw_code in profile.required_scoped_codes:
        scoped_code = _normalize_scoped_code(raw_code)
        if scoped_code and scoped_code not in scoped_keys:
            missing_required.append(scoped_code)
    add_check(
        "required_codes_present",
        len(missing_required) == 0,
        f"missing_required={len(missing_required)}",
    )
    for scoped_code in missing_required:
        add_issue("required_code_missing", "high", scoped_code, "required scoped code missing")

    present_forbidden: list[str] = []
    for raw_code in profile.forbidden_scoped_codes:
        scoped_code = _normalize_scoped_code(raw_code)
        if scoped_code and scoped_code in scoped_keys:
            present_forbidden.append(scoped_code)
    add_check(
        "forbidden_codes_absent",
        len(present_forbidden) == 0,
        f"forbidden_present={len(present_forbidden)}",
    )
    for scoped_code in present_forbidden:
        add_issue("forbidden_code_present", "high", scoped_code, "forbidden scoped code present")

    missing_appendix_scopes = [
        _normalize_scope(scope)
        for scope in profile.required_appendix_scopes
        if _normalize_scope(scope) and _normalize_scope(scope) not in appendix_scopes
    ]
    add_check(
        "required_appendix_scopes_present",
        len(missing_appendix_scopes) == 0,
        f"missing_appendix_scopes={len(missing_appendix_scopes)}",
    )
    for scope in missing_appendix_scopes:
        add_issue("appendix_scope_missing", "high", f"{scope}:*", "required appendix scope missing")

    add_check(
        "appendix_marker_count_min",
        appendix_marker_count >= profile.min_appendix_marker_count,
        f"appendix_markers={appendix_marker_count} min={profile.min_appendix_marker_count}",
    )
    if appendix_marker_count < profile.min_appendix_marker_count:
        add_issue(
            "appendix_marker_count_low",
            "medium",
            "",
            f"appendix_markers={appendix_marker_count} min={profile.min_appendix_marker_count}",
        )

    title_mismatch: list[str] = []
    for raw_code, needle in profile.required_title_contains.items():
        scoped_code = _normalize_scoped_code(raw_code)
        row = scoped_index.get(scoped_code)
        if not row:
            title_mismatch.append(scoped_code)
            add_issue("required_title_missing_code", "high", scoped_code, f"title check missing code; need contains '{needle}'")
            continue
        title = str(row.get("section_title") or "")
        if needle not in title:
            title_mismatch.append(scoped_code)
            add_issue(
                "required_title_mismatch",
                "high",
                scoped_code,
                f"title '{title}' does not contain '{needle}'",
            )
    add_check("required_title_contains", len(title_mismatch) == 0, f"title_mismatch={len(title_mismatch)}")

    requirement_text_mismatch: list[str] = []
    for raw_code, needle in profile.required_requirement_text_contains.items():
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        req_text = _lookup_requirement_text_for_scoped_code(
            scoped_code,
            requirement_text_by_scoped_code,
            tree_child_codes_by_parent,
        )
        if _normalize_match_text(needle) not in _normalize_match_text(req_text):
            requirement_text_mismatch.append(scoped_code)
            add_issue(
                "required_requirement_text_mismatch",
                "high",
                scoped_code,
                f"requirement text does not contain '{needle}'",
            )
    add_check(
        "required_requirement_text_contains",
        len(requirement_text_mismatch) == 0,
        f"requirement_text_mismatch={len(requirement_text_mismatch)}",
    )

    requirement_order_mismatch: list[str] = []
    for raw_code, snippets in profile.required_requirement_ordered_snippets.items():
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        req_text = _lookup_requirement_text_for_scoped_code(
            scoped_code,
            requirement_text_by_scoped_code,
            tree_child_codes_by_parent,
        )
        if not req_text:
            requirement_order_mismatch.append(scoped_code)
            add_issue(
                "required_requirement_order_missing_code",
                "high",
                scoped_code,
                "requirement text missing for ordered snippet check",
            )
            continue
        ordered = [str(item).strip() for item in (snippets or []) if str(item).strip()]
        if not ordered:
            continue
        req_text_norm = _normalize_match_text(req_text)
        cursor = -1
        failed = False
        for snippet in ordered:
            snippet_norm = _normalize_match_text(snippet)
            pos = req_text_norm.find(snippet_norm, cursor + 1)
            if pos < 0:
                requirement_order_mismatch.append(scoped_code)
                add_issue(
                    "required_requirement_order_missing_snippet",
                    "high",
                    scoped_code,
                    f"ordered snippet not found: '{snippet}'",
                )
                failed = True
                break
            if pos < cursor:
                requirement_order_mismatch.append(scoped_code)
                add_issue(
                    "required_requirement_order_violation",
                    "high",
                    scoped_code,
                    f"snippet order violation at '{snippet}'",
                )
                failed = True
                break
            cursor = pos
        if failed:
            continue
    add_check(
        "required_requirement_ordered_snippets",
        len(requirement_order_mismatch) == 0,
        f"requirement_order_mismatch={len(requirement_order_mismatch)}",
    )

    table_row_mismatch: list[str] = []
    for raw_code, snippets in profile.required_table_row_snippets.items():
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        req_text = requirement_text_by_scoped_code.get(scoped_code, "")
        req_text_norm = _normalize_match_text(req_text)
        ordered = [str(item).strip() for item in (snippets or []) if str(item).strip()]
        if not ordered:
            continue
        for snippet in ordered:
            if _normalize_match_text(snippet) not in req_text_norm:
                table_row_mismatch.append(scoped_code)
                add_issue(
                    "required_table_row_snippet_missing",
                    "high",
                    scoped_code,
                    f"table row snippet not found: '{snippet}'",
                )
                break
    add_check(
        "required_table_row_snippets",
        len(table_row_mismatch) == 0,
        f"table_row_mismatch={len(table_row_mismatch)}",
    )

    visual_asset_mismatch: list[str] = []
    for raw_code in profile.required_visual_asset_codes:
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        if visual_asset_counts_by_scoped_code.get(scoped_code, 0) > 0:
            continue
        visual_asset_mismatch.append(scoped_code)
        add_issue(
            "required_visual_asset_missing",
            "high",
            scoped_code,
            "preview visual asset not found",
        )
    add_check(
        "required_visual_asset_codes",
        len(visual_asset_mismatch) == 0,
        f"visual_asset_mismatch={len(visual_asset_mismatch)}",
    )

    tree_child_mismatch: list[str] = []
    for raw_parent, child_codes in profile.required_tree_children.items():
        parent_key = str(raw_parent or "").strip()
        expected_codes = [str(code).strip() for code in (child_codes or []) if str(code).strip()]
        if not parent_key or not expected_codes:
            continue
        actual_codes = tree_child_codes_by_parent.get(parent_key, [])
        if actual_codes != expected_codes:
            tree_child_mismatch.append(parent_key)
            add_issue(
                "required_tree_children_mismatch",
                "high",
                parent_key,
                f"expected children {expected_codes}, actual {actual_codes}",
            )
    add_check(
        "required_tree_children",
        len(tree_child_mismatch) == 0,
        f"tree_child_mismatch={len(tree_child_mismatch)}",
    )

    single_requirement_mismatch: list[str] = []
    for raw_code in profile.required_single_requirement_codes:
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        row_count = len(requirement_rows_by_scoped_code.get(scoped_code, []))
        if row_count != 1:
            single_requirement_mismatch.append(scoped_code)
            add_issue(
                "required_single_requirement_code_mismatch",
                "high",
                scoped_code,
                f"required single requirement row, actual={row_count}",
            )
    add_check(
        "required_single_requirement_codes",
        len(single_requirement_mismatch) == 0,
        f"single_requirement_mismatch={len(single_requirement_mismatch)}",
    )

    duplicate_title_mismatch: list[str] = []
    for raw_code, required_titles in profile.required_duplicate_code_titles.items():
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        existing_titles = requirement_titles_by_scoped_code.get(scoped_code, set())
        missing_titles: list[str] = []
        for title in [str(item).strip() for item in (required_titles or []) if str(item).strip()]:
            if _normalize_match_text(title) not in existing_titles:
                missing_titles.append(title)
        if missing_titles:
            duplicate_title_mismatch.append(scoped_code)
            add_issue(
                "required_duplicate_code_title_missing",
                "high",
                scoped_code,
                f"missing duplicate titles: {missing_titles}",
            )
    add_check(
        "required_duplicate_code_titles",
        len(duplicate_title_mismatch) == 0,
        f"duplicate_title_mismatch={len(duplicate_title_mismatch)}",
    )

    forbidden_cross_section_hits: list[str] = []
    for rule in profile.forbidden_cross_section_snippets:
        if not isinstance(rule, dict):
            continue
        snippet = str(rule.get("snippet") or "").strip()
        source_code = _normalize_scoped_code(str(rule.get("must_be_in") or ""))
        forbidden_codes = [
            _normalize_scoped_code(str(item))
            for item in (rule.get("must_not_be_in") or [])
            if _normalize_scoped_code(str(item))
        ]
        if not snippet or not source_code or not forbidden_codes:
            continue
        snippet_norm = _normalize_match_text(snippet)
        source_text = _normalize_match_text(requirement_text_by_scoped_code.get(source_code, ""))
        if snippet_norm and source_text and snippet_norm not in source_text:
            forbidden_cross_section_hits.append(source_code)
            add_issue(
                "forbidden_cross_section_source_missing",
                "high",
                source_code,
                f"source does not contain snippet: '{snippet}'",
            )
            continue
        for target_code in forbidden_codes:
            target_text = _normalize_match_text(requirement_text_by_scoped_code.get(target_code, ""))
            if snippet_norm and target_text and snippet_norm in target_text:
                forbidden_cross_section_hits.append(target_code)
                add_issue(
                    "forbidden_cross_section_snippet_present",
                    "high",
                    target_code,
                    f"forbidden snippet contamination from {source_code}: '{snippet}'",
                )
    add_check(
        "forbidden_cross_section_snippets",
        len(forbidden_cross_section_hits) == 0,
        f"forbidden_cross_section_hits={len(forbidden_cross_section_hits)}",
    )

    forbidden_requirement_text_hits: list[str] = []
    for raw_code, needle in profile.forbidden_requirement_text_contains.items():
        scoped_code = _normalize_scoped_code(raw_code)
        if not scoped_code:
            continue
        req_text = requirement_text_by_scoped_code.get(scoped_code, "")
        if _normalize_match_text(needle) and _normalize_match_text(needle) in _normalize_match_text(req_text):
            forbidden_requirement_text_hits.append(scoped_code)
            add_issue(
                "forbidden_requirement_text_present",
                "high",
                scoped_code,
                f"requirement text contains forbidden snippet '{needle}'",
            )
    add_check(
        "forbidden_requirement_text_absent",
        len(forbidden_requirement_text_hits) == 0,
        f"forbidden_requirement_text_hits={len(forbidden_requirement_text_hits)}",
    )

    add_check(
        "list_style_heading_limit",
        list_style_heading_count <= profile.max_list_style_heading_count,
        f"list_style_headings={list_style_heading_count} max={profile.max_list_style_heading_count}",
    )
    if list_style_heading_count > profile.max_list_style_heading_count:
        add_issue(
            "list_style_heading_limit",
            "medium",
            "",
            f"list_style_headings={list_style_heading_count} max={profile.max_list_style_heading_count}",
        )

    add_check(
        "leading_punct_heading_limit",
        leading_punct_heading_count <= profile.max_leading_punct_heading_count,
        f"leading_punct_headings={leading_punct_heading_count} max={profile.max_leading_punct_heading_count}",
    )
    if leading_punct_heading_count > profile.max_leading_punct_heading_count:
        add_issue(
            "leading_punct_heading_limit",
            "medium",
            "",
            f"leading_punct_headings={leading_punct_heading_count} max={profile.max_leading_punct_heading_count}",
        )

    add_check(
        "short_deep_heading_limit",
        short_deep_heading_count <= profile.max_short_deep_heading_count,
        f"short_deep_headings={short_deep_heading_count} max={profile.max_short_deep_heading_count}",
    )
    if short_deep_heading_count > profile.max_short_deep_heading_count:
        add_issue(
            "short_deep_heading_limit",
            "medium",
            "",
            f"short_deep_headings={short_deep_heading_count} max={profile.max_short_deep_heading_count}",
        )

    checks_total = len(checks)
    checks_passed = sum(1 for _, ok, _ in checks if ok)
    score_pct = round((checks_passed / checks_total) * 100, 2) if checks_total else 0.0
    doc_passed = checks_passed == checks_total

    doc_row: dict[str, Any] = {
        "pdf_name": pdf_path.name,
        "profile": profile.name,
        "sections_count": sections_count,
        "requirements_count": requirements_count,
        "appendix_scope_count": len(appendix_scopes),
        "appendix_marker_count": appendix_marker_count,
        "section_consistency_issues": section_consistency_issues,
        "list_style_heading_count": list_style_heading_count,
        "leading_punct_heading_count": leading_punct_heading_count,
        "short_deep_heading_count": short_deep_heading_count,
        "merge_mode": merge_mode,
        "supplemental_heading_total": supplemental_heading_total,
        "table_block_injected_total": table_block_injected_total,
        "line_injection_count": line_injection_count,
        "line_reorder_count": line_reorder_count,
        "order_integrity_ok": int(order_integrity_ok),
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "score_pct": score_pct,
        "passed": int(doc_passed),
    }
    for name, passed, detail in checks:
        doc_row[f"check_{name}"] = int(bool(passed))
        doc_row[f"detail_{name}"] = detail

    return {
        "doc_row": doc_row,
        "issue_rows": issues,
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "passed": doc_passed,
    }


def _load_profiles(payload: list[dict[str, Any]], defaults: dict[str, Any]) -> list[FidelityProfile]:
    profiles: list[FidelityProfile] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        profiles.append(
            FidelityProfile(
                name=str(item.get("name", "")).strip(),
                file_name_regex=str(item.get("file_name_regex", "")).strip(),
                min_sections_count=_as_int(item.get("min_sections_count"), defaults.get("min_sections_count", 0)),
                min_requirements_count=_as_int(
                    item.get("min_requirements_count"),
                    defaults.get("min_requirements_count", 0),
                ),
                expected_sections_count=_as_optional_int(item.get("expected_sections_count")),
                max_section_count_drift=_as_optional_int(item.get("max_section_count_drift")),
                expected_requirements_count=_as_optional_int(item.get("expected_requirements_count")),
                max_requirement_count_drift=_as_optional_int(item.get("max_requirement_count_drift")),
                max_section_consistency_issues=_as_int(
                    item.get("max_section_consistency_issues"),
                    defaults.get("max_section_consistency_issues", 999),
                ),
                required_scoped_codes=[str(code).strip() for code in item.get("required_scoped_codes", []) if str(code).strip()],
                forbidden_scoped_codes=[
                    str(code).strip() for code in item.get("forbidden_scoped_codes", []) if str(code).strip()
                ],
                required_appendix_scopes=[
                    str(scope).strip() for scope in item.get("required_appendix_scopes", []) if str(scope).strip()
                ],
                min_appendix_marker_count=_as_int(
                    item.get("min_appendix_marker_count"),
                    defaults.get("min_appendix_marker_count", 0),
                ),
                required_title_contains={
                    str(key).strip(): str(value)
                    for key, value in (item.get("required_title_contains", {}) or {}).items()
                    if str(key).strip()
                },
                required_requirement_text_contains={
                    str(key).strip(): str(value)
                    for key, value in (item.get("required_requirement_text_contains", {}) or {}).items()
                    if str(key).strip()
                },
                required_requirement_ordered_snippets={
                    str(key).strip(): [str(snippet).strip() for snippet in (value or []) if str(snippet).strip()]
                    for key, value in (item.get("required_requirement_ordered_snippets", {}) or {}).items()
                    if str(key).strip() and isinstance(value, list)
                },
                required_table_row_snippets={
                    str(key).strip(): [str(snippet).strip() for snippet in (value or []) if str(snippet).strip()]
                    for key, value in (item.get("required_table_row_snippets", {}) or {}).items()
                    if str(key).strip() and isinstance(value, list)
                },
                required_visual_asset_codes=[
                    str(code).strip() for code in (item.get("required_visual_asset_codes", []) or []) if str(code).strip()
                ],
                required_tree_children={
                    str(key).strip(): [str(code).strip() for code in (value or []) if str(code).strip()]
                    for key, value in (item.get("required_tree_children", {}) or {}).items()
                    if str(key).strip() and isinstance(value, list)
                },
                forbidden_cross_section_snippets=[
                    value for value in (item.get("forbidden_cross_section_snippets", []) or []) if isinstance(value, dict)
                ],
                required_single_requirement_codes=[
                    str(code).strip() for code in (item.get("required_single_requirement_codes", []) or []) if str(code).strip()
                ],
                required_duplicate_code_titles={
                    str(key).strip(): [str(title).strip() for title in (value or []) if str(title).strip()]
                    for key, value in (item.get("required_duplicate_code_titles", {}) or {}).items()
                    if str(key).strip() and isinstance(value, list)
                },
                forbidden_requirement_text_contains={
                    str(key).strip(): str(value)
                    for key, value in (item.get("forbidden_requirement_text_contains", {}) or {}).items()
                    if str(key).strip()
                },
                max_list_style_heading_count=_as_int(
                    item.get("max_list_style_heading_count"),
                    defaults.get("max_list_style_heading_count", 0),
                ),
                max_leading_punct_heading_count=_as_int(
                    item.get("max_leading_punct_heading_count"),
                    defaults.get("max_leading_punct_heading_count", 0),
                ),
                max_short_deep_heading_count=_as_int(
                    item.get("max_short_deep_heading_count"),
                    defaults.get("max_short_deep_heading_count", 0),
                ),
                require_strict_merge_mode=bool(
                    item.get("require_strict_merge_mode", defaults.get("require_strict_merge_mode", False))
                ),
                max_supplemental_heading_total=_as_optional_int(
                    item.get("max_supplemental_heading_total")
                    if "max_supplemental_heading_total" in item
                    else defaults.get("max_supplemental_heading_total")
                ),
                max_table_block_injected_total=_as_optional_int(
                    item.get("max_table_block_injected_total")
                    if "max_table_block_injected_total" in item
                    else defaults.get("max_table_block_injected_total")
                ),
                max_line_injection_count=_as_optional_int(
                    item.get("max_line_injection_count")
                    if "max_line_injection_count" in item
                    else defaults.get("max_line_injection_count")
                ),
                max_line_reorder_count=_as_optional_int(
                    item.get("max_line_reorder_count")
                    if "max_line_reorder_count" in item
                    else defaults.get("max_line_reorder_count")
                ),
                require_order_integrity_ok=bool(
                    item.get("require_order_integrity_ok", defaults.get("require_order_integrity_ok", False))
                ),
            )
        )
    return [profile for profile in profiles if profile.name and profile.file_name_regex]


def _match_profile(profiles: list[FidelityProfile], file_name: str) -> FidelityProfile | None:
    for profile in profiles:
        if profile.file_name_regex and re.search(profile.file_name_regex, file_name, re.I):
            return profile
    return None


def _normalize_scope(scope: str) -> str:
    token = str(scope or "").strip().upper()
    if token in {"MAIN", "ROOT", "NONE", "-"}:
        return ""
    return token


def _normalize_match_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\s+", "", value)
    return value


def _requirement_title_from_row(row: dict[str, Any]) -> str:
    title = str(row.get("section_title") or "").strip()
    if title:
        return title
    text = str(row.get("requirement_text") or "").strip()
    if not text:
        return ""
    first_line = text.splitlines()[0].strip()
    code = str(row.get("section_code") or "").strip()
    if code and first_line.startswith(code):
        first_line = first_line[len(code):].strip(" .、:：)\t")
    return first_line[:120]


def _collect_tree_child_codes(sections: list[dict[str, Any]]) -> dict[str, list[str]]:
    child_codes_by_parent: dict[str, list[str]] = {}
    for row in sections:
        parent_key = _normalize_scoped_code(str(row.get("structural_parent_key") or ""))
        code = str(row.get("section_code") or "").strip()
        if not parent_key or not code:
            continue
        child_codes_by_parent.setdefault(parent_key, []).append(code)
    return child_codes_by_parent


def _lookup_requirement_text_for_scoped_code(
    scoped_code: str,
    requirement_text_by_scoped_code: dict[str, str],
    tree_child_codes_by_parent: dict[str, list[str]],
) -> str:
    direct = str(requirement_text_by_scoped_code.get(scoped_code) or "").strip()
    if direct:
        return direct

    descendant_texts: list[str] = []
    seen_descendants: set[str] = set()
    stack = [scoped_code]
    while stack:
        parent_key = stack.pop()
        parent_scope, _ = _split_scope_code(parent_key)
        for child_code in tree_child_codes_by_parent.get(parent_key, []):
            child_scoped_code = _join_scope_code(parent_scope, child_code)
            if not child_scoped_code or child_scoped_code in seen_descendants:
                continue
            seen_descendants.add(child_scoped_code)
            stack.append(child_scoped_code)
            child_text = str(requirement_text_by_scoped_code.get(child_scoped_code) or "").strip()
            if child_text and child_text not in descendant_texts:
                descendant_texts.append(child_text)
    return "\n".join(descendant_texts).strip()


def _normalize_scoped_code(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    if "#" in text:
        text = text.split("#", 1)[0].strip()
        if not text:
            return ""
    if ":" not in text:
        scope = ""
        code = text
    else:
        left, right = text.split(":", 1)
        scope = _normalize_scope(left)
        code = right.strip()
    if not code:
        return ""
    return _join_scope_code(scope, code)


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


def _section_level(code: str) -> int:
    token = str(code or "").strip()
    return token.count(".") + 1 if token else 0


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _as_int(value: Any, default: int) -> int:
    text = str(value or "").strip()
    if not text:
        return int(default)
    try:
        return int(float(text))
    except ValueError:
        return int(default)


def _as_optional_int(value: Any) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"Config not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


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


def _render_summary_md(
    summary: dict[str, Any],
    doc_rows: list[dict[str, Any]],
    issue_rows: list[dict[str, Any]],
    skipped_docs: list[str],
) -> str:
    lines = [
        "# Fidelity Benchmark",
        "",
        f"- PDF dir: `{summary.get('pdf_dir', '')}`",
        f"- Target docs: `{summary.get('target_docs', 0)}`",
        f"- Checks: `{summary.get('checks_passed', 0)}/{summary.get('checks_total', 0)}`",
        f"- Score: `{summary.get('score_pct', 0.0):.2f}%`",
        f"- Failed docs: `{summary.get('failed_docs', 0)}`",
        f"- Overall pass: `{summary.get('overall_pass', False)}`",
        "",
        "## Document Scores",
    ]
    for row in sorted(doc_rows, key=lambda item: (float(item.get("score_pct", 0.0)), item.get("pdf_name", ""))):
        lines.append(
            f"- `{row.get('pdf_name', '')}` ({row.get('profile', '')}): "
            f"`{row.get('score_pct', 0.0)}%`, checks `{row.get('checks_passed', 0)}/{row.get('checks_total', 0)}`"
        )

    lines.append("")
    lines.append("## Top Issues")
    if not issue_rows:
        lines.append("- None")
    else:
        for row in issue_rows[:40]:
            scoped = _join_scope_code(str(row.get("appendix_scope") or ""), str(row.get("section_code") or ""))
            lines.append(
                f"- `{row.get('pdf_name', '')}` | `{row.get('issue_type', '')}` | "
                f"`{scoped}` | {row.get('detail', '')}"
            )

    lines.append("")
    lines.append("## Missing Profiles")
    missing_profiles = summary.get("missing_profiles", [])
    if not missing_profiles:
        lines.append("- None")
    else:
        for name in missing_profiles:
            lines.append(f"- `{name}`")

    lines.append("")
    lines.append("## Skipped Docs")
    if not skipped_docs:
        lines.append("- None")
    else:
        for name in skipped_docs:
            lines.append(f"- `{name}`")

    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main()
