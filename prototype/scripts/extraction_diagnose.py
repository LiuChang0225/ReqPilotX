#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from app.services.extraction_preview import build_extraction_preview
from app.services.parser import parse_document_with_diagnostics
from scripts import fidelity_benchmark
from scripts import section_split_regression
from scripts import structural_quality_gate


DEFAULT_BATCH_CONFIG = PROJECT_DIR / "eval" / "extraction_batches.json"
DEFAULT_ISSUE_TAXONOMY = PROJECT_DIR / "eval" / "extraction_issue_taxonomy.json"
DEFAULT_PATTERN_LIBRARY = PROJECT_DIR / "eval" / "extraction_patterns.json"
DEFAULT_QUALITY_PROFILE_CONFIG = PROJECT_DIR / "eval" / "quality_gate_profiles.json"
DEFAULT_FIDELITY_PROFILE_CONFIG = PROJECT_DIR / "eval" / "fidelity_profiles.json"
DEFAULT_OUT_DIR = PROJECT_DIR / "eval" / "extraction_diagnose" / "latest"

LOCAL_CLAUSE_RE = re.compile(
    r"^\s*(\d+(?:\.\d+){0,5})(?:\s*[)）】、:：.\-]+\s*|\s+)(.{1,160})$"
)
HEADING_SKELETON_RE = re.compile(
    r"^\s*(\d+(?:\.\d+){0,5})(?:\s*[)）】、:：.\-]+\s*|\s+)(.{1,160})$"
)
APPENDIX_HEADING_RE = re.compile(r"^\s*(?:附件|附录|appendix|annex|attachment)\s*([A-Za-z0-9_-]+)\b", re.I)
MAX_HEADING_LINES_PER_PAGE = 12

PATTERN_HINTS: dict[str, dict[str, Any]] = {
    "local_clause_heading_not_promoted": {
        "category": "heading_recovery",
        "suspected_layer": "preview",
        "recommended_fix_direction": (
            "Preserve local clause anchors as canonical sections; inspect extractor clause classification and "
            "preview local-clause promotion heuristics together."
        ),
    },
    "raw_scoped_code_namespace_collision": {
        "category": "scope_and_namespace",
        "suspected_layer": "extractor",
        "recommended_fix_direction": (
            "Tighten appendix scope / section instance generation before relying on duplicate scoped codes."
        ),
    },
    "complex_table_clause_binding_drift": {
        "category": "table_and_clause_binding",
        "suspected_layer": "preview",
        "recommended_fix_direction": (
            "Stabilize complex-table rewrite ordering, requirement binding, and dedupe semantics without sample hacks."
        ),
    },
    "profile_unmatched": {
        "category": "heading_recovery",
        "suspected_layer": "gate",
        "recommended_fix_direction": (
            "Add or refresh benchmark profiles only after confirming the current sample is a reusable regression target."
        ),
    },
    "parser_low_confidence_hotspot": {
        "category": "table_and_clause_binding",
        "suspected_layer": "parser",
        "recommended_fix_direction": (
            "Inspect page merge selection and parser confidence signals before changing downstream section logic."
        ),
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Single-document extraction diagnosis and pattern capture.")
    parser.add_argument(
        "input_path",
        type=Path,
        help="A PDF file or directory containing PDFs.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Directory for diagnosis artifacts.",
    )
    parser.add_argument(
        "--batch-config",
        type=Path,
        default=DEFAULT_BATCH_CONFIG,
        help="Extraction batch manifest used for doc metadata matching.",
    )
    parser.add_argument(
        "--issue-taxonomy",
        type=Path,
        default=DEFAULT_ISSUE_TAXONOMY,
        help="Issue taxonomy used to classify gate failures.",
    )
    parser.add_argument(
        "--pattern-library",
        type=Path,
        default=DEFAULT_PATTERN_LIBRARY,
        help="Pattern library JSON for candidate enrichment.",
    )
    parser.add_argument(
        "--quality-profile-config",
        type=Path,
        default=DEFAULT_QUALITY_PROFILE_CONFIG,
        help="Structural quality profile config.",
    )
    parser.add_argument(
        "--fidelity-profile-config",
        type=Path,
        default=DEFAULT_FIDELITY_PROFILE_CONFIG,
        help="Fidelity profile config.",
    )
    parser.add_argument(
        "--batch-id",
        default="",
        help="Optional batch id for metadata matching. Empty means search all configured batch documents.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.input_path.expanduser().resolve()
    out_dir = _resolve_path(args.out_dir)
    taxonomy = _load_json(_resolve_path(args.issue_taxonomy))
    pattern_library = _load_json(_resolve_path(args.pattern_library))
    batch_config = _load_json(_resolve_path(args.batch_config))
    quality_defaults, quality_profiles = _load_quality_profiles(_resolve_path(args.quality_profile_config))
    fidelity_profiles = _load_fidelity_profiles(_resolve_path(args.fidelity_profile_config))

    pdf_paths = _collect_pdf_paths(input_path)
    if not pdf_paths:
        raise SystemExit(f"No PDF files found for diagnosis: {input_path}")

    results = run_batch_extraction_audit(
        pdf_paths=pdf_paths,
        out_dir=out_dir,
        batch_config=batch_config,
        issue_taxonomy=taxonomy,
        pattern_library=pattern_library,
        quality_defaults=quality_defaults,
        quality_profiles=quality_profiles,
        fidelity_profiles=fidelity_profiles,
        batch_id=args.batch_id or None,
    )

    print(f"[diagnose] docs: {results['summary']['docs']}")
    print(f"[diagnose] findings: {results['summary']['total_findings']}")
    print(f"[diagnose] pattern candidates: {results['summary']['pattern_candidate_count']}")
    print(f"[diagnose] summary json: {out_dir / 'diagnosis_summary.json'}")
    print(f"[diagnose] summary md: {out_dir / 'diagnosis_summary.md'}")
    return 0


def run_batch_extraction_audit(
    *,
    pdf_paths: list[Path],
    out_dir: Path,
    batch_config: dict[str, Any],
    issue_taxonomy: dict[str, Any],
    pattern_library: dict[str, Any],
    quality_defaults: dict[str, Any],
    quality_profiles: list[structural_quality_gate.Profile],
    fidelity_profiles: list[fidelity_benchmark.FidelityProfile],
    batch_id: str | None = None,
    document_manifests: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    per_doc_dir = out_dir / "per_doc"
    per_doc_dir.mkdir(parents=True, exist_ok=True)

    doc_results: list[dict[str, Any]] = []
    pattern_candidates: list[dict[str, Any]] = []
    findings_total = 0

    manifest_by_name = {
        _normalize_name(str(item.get("pdf_name") or "")): dict(item)
        for item in (document_manifests or [])
    }

    for pdf_path in pdf_paths:
        logical_name = _normalize_name(pdf_path.name)
        manifest = manifest_by_name.get(logical_name)
        batch_match = match_batch_document(
            logical_name,
            batch_config=batch_config,
            batch_id=batch_id,
        )
        doc_context = {
            "doc_key": str((manifest or {}).get("doc_key") or (batch_match or {}).get("doc_key") or pdf_path.stem),
            "profile_hint": str((manifest or {}).get("profile") or (batch_match or {}).get("profile") or ""),
            "gate_tags": list((manifest or {}).get("gate_tags") or (batch_match or {}).get("gate_tags") or []),
            "section_split": bool((manifest or {}).get("section_split") or (batch_match or {}).get("section_split")),
            "batch_id": str((batch_match or {}).get("batch_id") or batch_id or ""),
        }
        diagnosis = run_extraction_audit(
            pdf_path=pdf_path,
            doc_context=doc_context,
            issue_taxonomy=issue_taxonomy,
            pattern_library=pattern_library,
            quality_defaults=quality_defaults,
            quality_profiles=quality_profiles,
            fidelity_profiles=fidelity_profiles,
        )
        findings_total += len(diagnosis["findings"])
        pattern_candidates.extend(diagnosis["pattern_candidates"])
        doc_results.append(diagnosis)
        safe_name = _slugify(logical_name)
        (per_doc_dir / f"{safe_name}.diagnosis.json").write_text(
            json.dumps(diagnosis, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    summary = build_diagnosis_summary(doc_results, pattern_candidates)
    summary_path = out_dir / "diagnosis_summary.json"
    patterns_path = out_dir / "pattern_candidates.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    patterns_path.write_text(json.dumps(pattern_candidates, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "diagnosis_summary.md").write_text(
        render_diagnosis_summary_md(summary, doc_results),
        encoding="utf-8",
    )
    return {
        "summary": summary,
        "documents": doc_results,
        "pattern_candidates": pattern_candidates,
        "paths": {
            "summary_json": str(summary_path),
            "summary_md": str(out_dir / "diagnosis_summary.md"),
            "pattern_candidates_json": str(patterns_path),
            "per_doc_dir": str(per_doc_dir),
        },
    }


def run_extraction_audit(
    *,
    pdf_path: Path,
    doc_context: dict[str, Any],
    issue_taxonomy: dict[str, Any],
    pattern_library: dict[str, Any],
    quality_defaults: dict[str, Any],
    quality_profiles: list[structural_quality_gate.Profile],
    fidelity_profiles: list[fidelity_benchmark.FidelityProfile],
) -> dict[str, Any]:
    pdf_path = pdf_path.expanduser().resolve()
    preview = build_extraction_preview(str(pdf_path))
    parsed = parse_document_with_diagnostics(str(pdf_path))
    logical_name = _normalize_name(pdf_path.name)

    quality_profile = structural_quality_gate._match_profile(quality_profiles, logical_name)
    quality_result = structural_quality_gate._evaluate_document(
        pdf_name=logical_name,
        outline=preview.get("section_outline", []) or [],
        requirements=preview.get("complete_requirement_entries", []) or [],
        parser_diagnostics=preview.get("parser_diagnostics", {}) or {},
        section_consistency=preview.get("section_consistency", {}) or {},
        defaults=quality_defaults,
        profile=quality_profile,
    )

    fidelity_profile = fidelity_benchmark._match_profile(fidelity_profiles, logical_name)
    fidelity_result = (
        fidelity_benchmark._evaluate_document(pdf_path, fidelity_profile)
        if fidelity_profile is not None
        else None
    )

    section_split_result = (
        section_split_regression.evaluate_preview(preview, pdf_path=str(pdf_path))
        if bool(doc_context.get("section_split"))
        else None
    )

    issue_rollup, issue_summary = _build_issue_rollup(
        quality_issue_rows=quality_result.get("issue_rows", []),
        fidelity_issue_rows=(fidelity_result or {}).get("issue_rows", []),
        section_split_result=section_split_result,
        taxonomy=issue_taxonomy,
        profile_name=str(fidelity_profile.name if fidelity_profile else ""),
    )

    evidence = build_evidence_pack(
        pdf_path=pdf_path,
        parsed=parsed,
        preview=preview,
        quality_doc_row=quality_result.get("doc_row", {}),
        fidelity_doc_row=(fidelity_result or {}).get("doc_row", {}),
        fidelity_profile=fidelity_profile,
    )
    findings = build_findings(
        doc_key=str(doc_context.get("doc_key") or pdf_path.stem),
        pdf_name=logical_name,
        gate_tags=list(doc_context.get("gate_tags") or []),
        issue_rollup=issue_rollup,
        evidence=evidence,
        pattern_library=pattern_library,
        quality_profile_name=str(quality_profile.name if quality_profile else ""),
        fidelity_profile_name=str(fidelity_profile.name if fidelity_profile else ""),
    )
    pattern_candidates = build_pattern_candidates(
        findings=findings,
        pattern_library=pattern_library,
        doc_key=str(doc_context.get("doc_key") or pdf_path.stem),
        pdf_name=logical_name,
        gate_tags=list(doc_context.get("gate_tags") or []),
    )
    diagnosis_messages = build_extraction_audit_messages(
        pdf_name=logical_name,
        issue_rollup=issue_rollup,
        findings=findings,
        evidence=evidence,
    )

    return {
        "doc_key": str(doc_context.get("doc_key") or pdf_path.stem),
        "pdf_name": logical_name,
        "pdf_path": str(pdf_path),
        "batch_id": str(doc_context.get("batch_id") or ""),
        "gate_tags": list(doc_context.get("gate_tags") or []),
        "section_split_target": bool(doc_context.get("section_split")),
        "profiles": {
            "quality": str(quality_profile.name if quality_profile else ""),
            "fidelity": str(fidelity_profile.name if fidelity_profile else ""),
        },
        "status": {
            "quality_profile_matched": quality_profile is not None,
            "fidelity_profile_matched": fidelity_profile is not None,
            "needs_human_review": any(bool(item.get("needs_human_review")) for item in findings),
        },
        "quality_gate": {
            "doc_row": quality_result.get("doc_row", {}),
            "checks_total": int(quality_result.get("checks_total", 0)),
            "checks_passed": int(quality_result.get("checks_passed", 0)),
        },
        "fidelity_gate": {
            "doc_row": (fidelity_result or {}).get("doc_row", {}),
            "checks_total": int((fidelity_result or {}).get("checks_total", 0)),
            "checks_passed": int((fidelity_result or {}).get("checks_passed", 0)),
            "passed": bool((fidelity_result or {}).get("passed", False)),
        },
        "section_split": section_split_result or {},
        "issue_summary": issue_summary,
        "issue_rollup": issue_rollup,
        "evidence": evidence,
        "findings": findings,
        "pattern_candidates": pattern_candidates,
        "diagnosis_messages": diagnosis_messages,
    }


def build_extraction_audit_messages(
    *,
    pdf_name: str,
    issue_rollup: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> list[dict[str, str]]:
    compact = {
        "pdf_name": pdf_name,
        "issue_rollup": issue_rollup[:12],
        "findings": findings[:8],
        "page_hotspots": evidence.get("page_hotspots", [])[:6],
        "local_clause_summary": evidence.get("local_clause_summary", {}),
        "duplicate_scope_summary": evidence.get("duplicate_scope_summary", {}),
    }
    return [
        {
            "role": "system",
            "content": (
                "You are auditing extraction output. Use only the evidence pack, identify the most likely failing layer, "
                "and recommend a benchmark-safe generic fix."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(compact, ensure_ascii=False, indent=2),
        },
    ]


def build_evidence_pack(
    *,
    pdf_path: Path,
    parsed: dict[str, Any],
    preview: dict[str, Any],
    quality_doc_row: dict[str, Any],
    fidelity_doc_row: dict[str, Any],
    fidelity_profile: fidelity_benchmark.FidelityProfile | None,
) -> dict[str, Any]:
    page_texts = _build_page_texts(parsed.get("segments", []) or [])
    heading_skeleton = _build_heading_skeleton(page_texts)
    local_clause_summary = _build_local_clause_summary(
        requirements=preview.get("requirements", []) or [],
        sections=preview.get("sections", []) or [],
    )
    duplicate_scope_summary = _build_duplicate_scope_summary(
        outline=preview.get("section_outline", []) or [],
        sections=preview.get("sections", []) or [],
        requirements=preview.get("requirements", []) or [],
        section_consistency=preview.get("section_consistency", {}) or {},
    )
    baseline_delta = _build_baseline_delta(
        preview_summary=preview.get("summary", {}) or {},
        quality_doc_row=quality_doc_row,
        fidelity_doc_row=fidelity_doc_row,
        fidelity_profile=fidelity_profile,
    )
    page_hotspots = _build_page_hotspots(
        parser_diagnostics=preview.get("parser_diagnostics", {}) or {},
        requirements=preview.get("requirements", []) or [],
        local_clause_by_page=local_clause_summary.get("page_counts", {}),
    )
    return {
        "pdf_path": str(pdf_path),
        "heading_skeleton": heading_skeleton,
        "local_clause_summary": local_clause_summary,
        "duplicate_scope_summary": duplicate_scope_summary,
        "baseline_delta": baseline_delta,
        "page_hotspots": page_hotspots,
        "parser_diagnostics": {
            "merge_mode": str((preview.get("parser_diagnostics") or {}).get("merge_mode") or ""),
            "line_reorder_count": int((preview.get("parser_diagnostics") or {}).get("line_reorder_count") or 0),
            "low_confidence_pages": list((preview.get("parser_diagnostics") or {}).get("low_confidence_pages") or []),
            "selected_engine_pages": dict((preview.get("parser_diagnostics") or {}).get("selected_engine_pages") or {}),
        },
        "expected_structure_summary": _build_expected_structure_summary(local_clause_summary, duplicate_scope_summary),
    }


def build_findings(
    *,
    doc_key: str,
    pdf_name: str,
    gate_tags: list[str],
    issue_rollup: list[dict[str, Any]],
    evidence: dict[str, Any],
    pattern_library: dict[str, Any],
    quality_profile_name: str,
    fidelity_profile_name: str,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    pattern_index = _pattern_index(pattern_library)
    local_clause_summary = evidence.get("local_clause_summary", {}) or {}
    duplicate_scope_summary = evidence.get("duplicate_scope_summary", {}) or {}
    baseline_delta = evidence.get("baseline_delta", {}) or {}
    parser_diag = evidence.get("parser_diagnostics", {}) or {}

    for parent in local_clause_summary.get("parent_findings", []) or []:
        if int(parent.get("missing_section_anchor_count", 0)) <= 0:
            continue
        expected_nodes = ", ".join(parent.get("expected_direct_children", [])[:10]) or "local clauses"
        finding = _pattern_finding(
            pattern_index=pattern_index,
            pattern_id="local_clause_heading_not_promoted",
            issue_type="local_clause_section_missing",
            impacted_scope=str(parent.get("appendix_scope") or ""),
            impacted_section=str(parent.get("parent_section_code") or ""),
            expected_structure_summary=(
                f"{parent.get('parent_section_code') or 'parent'} should expose local section anchors such as {expected_nodes}"
            ),
            evidence_refs=[
                f"local_clause_summary.parent_findings[{parent.get('parent_section_code') or 'unknown'}]",
                "expected_structure_summary",
            ],
            confidence=0.93 if int(parent.get("missing_section_anchor_count", 0)) >= 4 else 0.78,
            needs_human_review=False,
            details={
                "missing_section_anchor_count": int(parent.get("missing_section_anchor_count", 0)),
                "remote_section_match_count": int(parent.get("remote_section_match_count", 0)),
                "sample_missing_codes": list(parent.get("sample_missing_codes", [])[:8]),
            },
        )
        findings.append(finding)

    for duplicate in duplicate_scope_summary.get("duplicates", []) or []:
        raw_count = int(duplicate.get("raw_duplicate_count", 0))
        if raw_count <= 1:
            continue
        disambiguated = int(duplicate.get("section_instance_key_count", 0)) >= raw_count
        hinted_pattern = "raw_scoped_code_namespace_collision"
        finding = _pattern_finding(
            pattern_index=pattern_index,
            pattern_id=hinted_pattern,
            issue_type="raw_duplicate_scoped_code",
            impacted_scope=str(duplicate.get("appendix_scope") or ""),
            impacted_section=str(duplicate.get("section_code") or ""),
            expected_structure_summary=(
                "Duplicate scoped codes should either collapse upstream or remain explicitly instance-safe before gating."
            ),
            evidence_refs=[
                f"duplicate_scope_summary.duplicates[{duplicate.get('scoped_code') or 'unknown'}]",
            ],
            confidence=0.82 if disambiguated else 0.9,
            needs_human_review=disambiguated,
            suspected_layer="gate" if disambiguated else None,
            details={
                "raw_duplicate_count": raw_count,
                "section_instance_key_count": int(duplicate.get("section_instance_key_count", 0)),
                "requirement_row_count": int(duplicate.get("requirement_row_count", 0)),
            },
        )
        findings.append(finding)

    if baseline_delta.get("requirements_drift") is not None and abs(int(baseline_delta.get("requirements_drift") or 0)) > 0:
        if baseline_delta.get("complex_table_signal") or baseline_delta.get("table_row_issue_count", 0):
            finding = _pattern_finding(
                pattern_index=pattern_index,
                pattern_id="complex_table_clause_binding_drift",
                issue_type="complex_table_clause_binding_drift",
                impacted_scope="",
                impacted_section=str(baseline_delta.get("hotspot_section_code") or ""),
                expected_structure_summary="Complex-table pages should keep stable section ownership while preserving row coverage.",
                evidence_refs=["baseline_delta", "page_hotspots"],
                confidence=0.76,
                needs_human_review=False,
                details={
                    "requirements_drift": int(baseline_delta.get("requirements_drift") or 0),
                    "table_row_issue_count": int(baseline_delta.get("table_row_issue_count") or 0),
                    "hotspot_section_code": str(baseline_delta.get("hotspot_section_code") or ""),
                },
            )
            findings.append(finding)

    if not quality_profile_name or not fidelity_profile_name:
        findings.append(
            _pattern_finding(
                pattern_index=pattern_index,
                pattern_id="profile_unmatched",
                issue_type="profile_unmatched",
                impacted_scope="",
                impacted_section="",
                expected_structure_summary="Capture diagnosis evidence first, then decide whether the sample should join a reusable benchmark profile.",
                evidence_refs=["issue_summary", "profiles"],
                confidence=0.66,
                needs_human_review=True,
                details={
                    "quality_profile": quality_profile_name,
                    "fidelity_profile": fidelity_profile_name,
                },
            )
        )

    low_conf_pages = list(parser_diag.get("low_confidence_pages") or [])
    if low_conf_pages:
        findings.append(
            _pattern_finding(
                pattern_index=pattern_index,
                pattern_id="parser_low_confidence_hotspot",
                issue_type="parser_low_confidence_hotspot",
                impacted_scope="",
                impacted_section="",
                expected_structure_summary="Low-confidence pages should be stabilized before downstream clause binding is tuned.",
                evidence_refs=["parser_diagnostics.low_confidence_pages", "page_hotspots"],
                confidence=0.62,
                needs_human_review=True,
                details={"low_confidence_pages": low_conf_pages[:12]},
            )
        )

    enriched_gate_findings = _gate_issue_findings(issue_rollup, pattern_index)
    findings.extend(enriched_gate_findings)

    findings.sort(
        key=lambda item: (
            int(bool(item.get("needs_human_review"))),
            -float(item.get("confidence") or 0.0),
            str(item.get("issue_type") or ""),
        )
    )
    return _dedupe_findings(findings)


def build_pattern_candidates(
    *,
    findings: list[dict[str, Any]],
    pattern_library: dict[str, Any],
    doc_key: str,
    pdf_name: str,
    gate_tags: list[str],
) -> list[dict[str, Any]]:
    pattern_index = _pattern_index(pattern_library)
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        pattern_id = str(finding.get("pattern_id") or finding.get("issue_type") or "").strip()
        if not pattern_id:
            continue
        pattern_meta = pattern_index.get(pattern_id, {})
        candidates.append(
            {
                "pattern_id": pattern_id,
                "pattern_name": str(pattern_meta.get("name") or finding.get("issue_type") or pattern_id),
                "category": str(pattern_meta.get("category") or finding.get("category") or "unmapped"),
                "doc_key": doc_key,
                "pdf_name": pdf_name,
                "issue_type": str(finding.get("issue_type") or ""),
                "suspected_layer": str(finding.get("suspected_layer") or ""),
                "recommended_fix_direction": str(finding.get("recommended_fix_direction") or ""),
                "benchmark_impact": list(gate_tags),
                "promoted": bool(pattern_meta.get("promoted", False)),
                "validation_status": str(pattern_meta.get("validation_status") or "candidate"),
            }
        )
    return _dedupe_pattern_candidates(candidates)


def build_diagnosis_summary(doc_results: list[dict[str, Any]], pattern_candidates: list[dict[str, Any]]) -> dict[str, Any]:
    suspected_layer_counts: dict[str, int] = {}
    finding_type_counts: dict[str, int] = {}
    docs_requiring_human_review = 0
    for result in doc_results:
        if bool(result.get("status", {}).get("needs_human_review")):
            docs_requiring_human_review += 1
        for finding in result.get("findings", []) or []:
            issue_type = str(finding.get("issue_type") or "")
            finding_type_counts[issue_type] = finding_type_counts.get(issue_type, 0) + 1
            layer = str(finding.get("suspected_layer") or "")
            if layer:
                suspected_layer_counts[layer] = suspected_layer_counts.get(layer, 0) + 1
    return {
        "docs": len(doc_results),
        "total_findings": sum(len(result.get("findings", []) or []) for result in doc_results),
        "pattern_candidate_count": len(pattern_candidates),
        "docs_requiring_human_review": docs_requiring_human_review,
        "finding_type_counts": dict(sorted(finding_type_counts.items())),
        "suspected_layer_counts": dict(sorted(suspected_layer_counts.items())),
        "documents": [
            {
                "doc_key": result.get("doc_key"),
                "pdf_name": result.get("pdf_name"),
                "profiles": result.get("profiles"),
                "finding_count": len(result.get("findings", []) or []),
                "pattern_candidate_count": len(result.get("pattern_candidates", []) or []),
                "needs_human_review": result.get("status", {}).get("needs_human_review", False),
            }
            for result in doc_results
        ],
    }


def render_diagnosis_summary_md(summary: dict[str, Any], doc_results: list[dict[str, Any]]) -> str:
    lines = [
        "# Extraction Diagnosis Summary",
        "",
        f"- docs: `{summary.get('docs', 0)}`",
        f"- findings: `{summary.get('total_findings', 0)}`",
        f"- pattern_candidates: `{summary.get('pattern_candidate_count', 0)}`",
        f"- docs_requiring_human_review: `{summary.get('docs_requiring_human_review', 0)}`",
        f"- suspected_layer_counts: `{summary.get('suspected_layer_counts', {})}`",
        "",
        "## Documents",
    ]
    for result in doc_results:
        lines.append(
            "- "
            f"`{result.get('doc_key', '')}` -> `{result.get('pdf_name', '')}` | "
            f"profiles `{result.get('profiles', {})}` | "
            f"findings `{len(result.get('findings', []) or [])}` | "
            f"pattern_candidates `{len(result.get('pattern_candidates', []) or [])}`"
        )
        for finding in (result.get("findings", []) or [])[:3]:
            lines.append(
                f"  - `{finding.get('issue_type', '')}` [{finding.get('suspected_layer', '')}] "
                f"{finding.get('expected_structure_summary', '')}"
            )
    return "\n".join(lines) + "\n"


def match_batch_document(
    logical_name: str,
    *,
    batch_config: dict[str, Any],
    batch_id: str | None,
) -> dict[str, Any] | None:
    candidates = batch_config.get("batches", []) if isinstance(batch_config, dict) else []
    for batch in candidates:
        if batch_id and str(batch.get("id") or "") != str(batch_id):
            continue
        for doc in batch.get("documents", []) or []:
            match_regex = str(doc.get("match_regex") or "").strip()
            if not match_regex:
                continue
            if re.search(match_regex, logical_name, re.I):
                payload = dict(doc)
                payload["batch_id"] = str(batch.get("id") or "")
                return payload
    return None


def _build_issue_rollup(
    *,
    quality_issue_rows: list[dict[str, Any]],
    fidelity_issue_rows: list[dict[str, Any]],
    section_split_result: dict[str, Any] | None,
    taxonomy: dict[str, Any],
    profile_name: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for gate_name, source_rows in (("quality_gate", quality_issue_rows), ("fidelity", fidelity_issue_rows)):
        for row in source_rows or []:
            issue_type = str(row.get("issue_type") or "")
            category_key = str((taxonomy.get("issue_types") or {}).get(issue_type) or taxonomy.get("default_category", "unmapped"))
            category_label = str((taxonomy.get("categories") or {}).get(category_key) or category_key)
            rows.append(
                {
                    "gate": gate_name,
                    "pdf_name": _normalize_name(str(row.get("pdf_name") or "")),
                    "profile": str(row.get("profile") or profile_name),
                    "category": category_key,
                    "category_label": category_label,
                    "issue_type": issue_type,
                    "severity": str(row.get("severity") or "info"),
                    "appendix_scope": str(row.get("appendix_scope") or ""),
                    "section_code": str(row.get("section_code") or ""),
                    "detail": str(row.get("detail") or ""),
                }
            )
    if section_split_result:
        for check in section_split_result.get("checks", []) or []:
            if bool(check.get("passed")):
                continue
            issue_type = "section_split_failed"
            category_key = str((taxonomy.get("issue_types") or {}).get(issue_type) or taxonomy.get("default_category", "unmapped"))
            category_label = str((taxonomy.get("categories") or {}).get(category_key) or category_key)
            rows.append(
                {
                    "gate": "section_split",
                    "pdf_name": _normalize_name(str(section_split_result.get("pdf_path") or "")),
                    "profile": profile_name,
                    "category": category_key,
                    "category_label": category_label,
                    "issue_type": issue_type,
                    "severity": "high",
                    "appendix_scope": "",
                    "section_code": "",
                    "detail": f"{check.get('name')}: {check.get('detail')}",
                }
            )
    rows.sort(key=lambda item: (str(item.get("severity")), str(item.get("issue_type")), str(item.get("section_code"))))
    summary = {
        "total_issues": len(rows),
        "issue_types": sorted({str(row.get("issue_type") or "") for row in rows}),
    }
    return rows, summary


def _build_page_texts(segments: list[dict[str, Any]]) -> dict[int, list[str]]:
    by_page: dict[int, list[str]] = defaultdict(list)
    for segment in segments or []:
        page = _to_int(segment.get("source_page"))
        text = str(segment.get("text") or "").strip()
        if page <= 0 or not text:
            continue
        for line in text.splitlines():
            clean = line.strip()
            if clean:
                by_page[page].append(clean)
    return dict(sorted(by_page.items()))


def _build_heading_skeleton(page_texts: dict[int, list[str]]) -> list[dict[str, Any]]:
    skeleton: list[dict[str, Any]] = []
    for page, lines in page_texts.items():
        page_hits = 0
        for line in lines:
            appendix_match = APPENDIX_HEADING_RE.match(line)
            if appendix_match:
                skeleton.append(
                    {
                        "page": page,
                        "kind": "appendix",
                        "code": appendix_match.group(1).upper(),
                        "title": line[:160],
                    }
                )
                page_hits += 1
            heading_match = HEADING_SKELETON_RE.match(line)
            if not heading_match:
                continue
            code = str(heading_match.group(1) or "").strip()
            title = str(heading_match.group(2) or "").strip()
            if not code or not title:
                continue
            skeleton.append(
                {
                    "page": page,
                    "kind": "heading",
                    "code": code,
                    "title": title[:160],
                }
            )
            page_hits += 1
            if page_hits >= MAX_HEADING_LINES_PER_PAGE:
                break
    return skeleton


def _build_local_clause_summary(
    *,
    requirements: list[dict[str, Any]],
    sections: list[dict[str, Any]],
) -> dict[str, Any]:
    section_hits: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in sections:
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        code = str(row.get("section_code") or "").strip()
        if not code:
            continue
        section_hits[(scope, code)].append(row)

    page_counts: dict[int, int] = defaultdict(int)
    parent_buckets: dict[tuple[str, str], dict[str, Any]] = {}

    for row in requirements:
        text = str(row.get("requirement_text") or "").strip()
        match = LOCAL_CLAUSE_RE.match(text)
        if not match:
            continue
        clause_code = str(match.group(1) or "").strip()
        section_code = str(row.get("section_code") or "").strip()
        if not clause_code or clause_code == section_code:
            continue
        scope = _normalize_scope(str(row.get("appendix_scope") or ""))
        page = _to_int(row.get("source_page"))
        page_end = _to_int(row.get("source_page_end")) or page
        overlap_found = False
        remote_found = False
        for section in section_hits.get((scope, clause_code), []):
            start = _to_int(section.get("source_page_start"))
            end = _to_int(section.get("source_page_end")) or start
            if _ranges_overlap(page, page_end, start, end):
                overlap_found = True
            else:
                remote_found = True
        parent_key = (scope, section_code)
        bucket = parent_buckets.setdefault(
            parent_key,
            {
                "appendix_scope": scope,
                "parent_section_code": section_code,
                "requirement_count": 0,
                "missing_section_anchor_count": 0,
                "remote_section_match_count": 0,
                "sample_missing_codes": [],
                "expected_direct_children": set(),
            },
        )
        bucket["requirement_count"] += 1
        root_code = clause_code.split(".")[0]
        bucket["expected_direct_children"].add(root_code)
        if not overlap_found:
            page_counts[page] += 1
            bucket["missing_section_anchor_count"] += 1
            if len(bucket["sample_missing_codes"]) < 8:
                bucket["sample_missing_codes"].append(clause_code)
        if remote_found and not overlap_found:
            bucket["remote_section_match_count"] += 1

    parent_findings: list[dict[str, Any]] = []
    for bucket in parent_buckets.values():
        bucket["expected_direct_children"] = sorted(bucket["expected_direct_children"], key=_sort_code_key)
        if bucket["missing_section_anchor_count"] <= 0:
            continue
        parent_findings.append(bucket)
    parent_findings.sort(
        key=lambda item: (
            -int(item.get("missing_section_anchor_count", 0)),
            str(item.get("parent_section_code") or ""),
        )
    )

    return {
        "parent_findings": parent_findings,
        "page_counts": {str(page): count for page, count in sorted(page_counts.items())},
    }


def _build_duplicate_scope_summary(
    *,
    outline: list[dict[str, Any]],
    sections: list[dict[str, Any]],
    requirements: list[dict[str, Any]],
    section_consistency: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_counts: Counter[str] = Counter()
    instance_keys: dict[str, set[str]] = defaultdict(set)
    requirement_counts: Counter[str] = Counter()
    ignored_scoped_codes: set[str] = set()

    for item in (section_consistency or {}).get("issues", []) or []:
        if str(item.get("issue_type") or "").strip() != "source_faithful_duplicate_code":
            continue
        scoped_code = _join_scope_code(str(item.get("appendix_scope") or ""), str(item.get("section_code") or ""))
        if scoped_code:
            ignored_scoped_codes.add(scoped_code)

    for row in outline:
        scoped_code = _join_scope_code(str(row.get("appendix_scope") or ""), str(row.get("section_code") or ""))
        if scoped_code:
            raw_counts[scoped_code] += 1
    for row in sections:
        scoped_code = _join_scope_code(str(row.get("appendix_scope") or ""), str(row.get("section_code") or ""))
        if scoped_code:
            instance_keys[scoped_code].add(str(row.get("section_instance_key") or row.get("section_key") or ""))
    for row in requirements:
        scoped_code = _join_scope_code(str(row.get("appendix_scope") or ""), str(row.get("section_code") or ""))
        if scoped_code:
            requirement_counts[scoped_code] += 1

    duplicates: list[dict[str, Any]] = []
    for scoped_code, count in sorted(raw_counts.items()):
        if count <= 1:
            continue
        if scoped_code in ignored_scoped_codes:
            continue
        scope, code = _split_scope_code(scoped_code)
        duplicates.append(
            {
                "scoped_code": scoped_code,
                "appendix_scope": scope,
                "section_code": code,
                "raw_duplicate_count": count,
                "section_instance_key_count": len({key for key in instance_keys.get(scoped_code, set()) if key}),
                "requirement_row_count": int(requirement_counts.get(scoped_code, 0)),
            }
        )
    return {"duplicates": duplicates}


def _build_baseline_delta(
    *,
    preview_summary: dict[str, Any],
    quality_doc_row: dict[str, Any],
    fidelity_doc_row: dict[str, Any],
    fidelity_profile: fidelity_benchmark.FidelityProfile | None,
) -> dict[str, Any]:
    requirements_count = int(preview_summary.get("requirements_count") or 0)
    sections_count = int(preview_summary.get("sections_count") or 0)
    requirements_drift = None
    sections_drift = None
    if fidelity_profile is not None and fidelity_profile.expected_requirements_count is not None:
        requirements_drift = requirements_count - int(fidelity_profile.expected_requirements_count)
    if fidelity_profile is not None and fidelity_profile.expected_sections_count is not None:
        sections_drift = sections_count - int(fidelity_profile.expected_sections_count)

    hotspot_section_code = ""
    table_row_issue_count = 0
    for key, value in fidelity_doc_row.items():
        if str(key).startswith("check_required_table_row_snippets") and int(float(value or 0)) == 0:
            table_row_issue_count += 1
    if int(quality_doc_row.get("duplicate_code_count") or 0) > 0:
        hotspot_section_code = "duplicate_scoped_codes"

    return {
        "requirements_count": requirements_count,
        "sections_count": sections_count,
        "requirements_drift": requirements_drift,
        "sections_drift": sections_drift,
        "table_row_issue_count": table_row_issue_count,
        "complex_table_signal": int(preview_summary.get("complex_table_zone_count") or 0) > 0,
        "hotspot_section_code": hotspot_section_code,
    }


def _build_page_hotspots(
    *,
    parser_diagnostics: dict[str, Any],
    requirements: list[dict[str, Any]],
    local_clause_by_page: dict[str, int],
) -> list[dict[str, Any]]:
    req_counts: Counter[int] = Counter()
    for row in requirements:
        page = _to_int(row.get("source_page"))
        if page > 0:
            req_counts[page] += 1
    hotspots: list[dict[str, Any]] = []
    low_conf_pages = {int(page) for page in (parser_diagnostics.get("low_confidence_pages") or []) if int(page) > 0}
    for detail in parser_diagnostics.get("page_details", []) or []:
        page = _to_int(detail.get("page_no"))
        if page <= 0:
            continue
        local_clause_count = int(local_clause_by_page.get(str(page), 0))
        req_count = int(req_counts.get(page, 0))
        if req_count == 0 and local_clause_count == 0 and page not in low_conf_pages:
            continue
        hotspots.append(
            {
                "page": page,
                "requirement_count": req_count,
                "missing_local_clause_count": local_clause_count,
                "low_confidence": page in low_conf_pages,
                "selected_engine": str(detail.get("selected_engine") or ""),
                "quality_score": float(detail.get("quality_score") or 0.0),
                "table_count": int(detail.get("table_count") or 0),
                "enum_signal": int(detail.get("enum_signal") or 0),
            }
        )
    hotspots.sort(
        key=lambda item: (
            int(not bool(item.get("low_confidence"))),
            -int(item.get("missing_local_clause_count", 0)),
            -int(item.get("requirement_count", 0)),
        )
    )
    return hotspots[:8]


def _build_expected_structure_summary(
    local_clause_summary: dict[str, Any],
    duplicate_scope_summary: dict[str, Any],
) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for parent in local_clause_summary.get("parent_findings", []) or []:
        children = ", ".join(parent.get("expected_direct_children", [])[:10])
        summary.append(
            {
                "appendix_scope": str(parent.get("appendix_scope") or ""),
                "parent_section_code": str(parent.get("parent_section_code") or ""),
                "expected_children": list(parent.get("expected_direct_children", [])[:12]),
                "summary": f"{parent.get('parent_section_code') or 'parent'} should expose local anchors: {children}",
            }
        )
    for duplicate in duplicate_scope_summary.get("duplicates", []) or []:
        summary.append(
            {
                "appendix_scope": str(duplicate.get("appendix_scope") or ""),
                "parent_section_code": str(duplicate.get("section_code") or ""),
                "expected_children": [],
                "summary": (
                    f"{duplicate.get('scoped_code') or ''} should stop colliding at raw outline level or become explicitly instance-safe."
                ),
            }
        )
    return summary[:12]


def _gate_issue_findings(
    issue_rollup: list[dict[str, Any]],
    pattern_index: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for row in issue_rollup:
        issue_type = str(row.get("issue_type") or "")
        if issue_type not in {"required_code_missing", "required_title_mismatch", "requirement_count_drift", "duplicate_section_code"}:
            continue
        pattern_id = ""
        suspected_layer = "extractor"
        expected_summary = ""
        if issue_type == "duplicate_section_code":
            pattern_id = "raw_scoped_code_namespace_collision"
            suspected_layer = "extractor"
            expected_summary = "Duplicate scoped codes should map to an intentional namespace or collapse upstream."
        elif issue_type == "requirement_count_drift":
            pattern_id = "complex_table_clause_binding_drift"
            suspected_layer = "preview"
            expected_summary = "Requirement counts should return to the benchmark band without losing section anchors."
        elif issue_type in {"required_code_missing", "required_title_mismatch"}:
            pattern_id = "local_clause_heading_not_promoted"
            suspected_layer = "extractor"
            expected_summary = "Missing or drifted section anchors should be restored through generic heading recovery."
        finding = _pattern_finding(
            pattern_index=pattern_index,
            pattern_id=pattern_id,
            issue_type=issue_type,
            impacted_scope=str(row.get("appendix_scope") or ""),
            impacted_section=str(row.get("section_code") or ""),
            expected_structure_summary=expected_summary,
            evidence_refs=[f"issue_rollup[{issue_type}]"],
            confidence=0.58,
            needs_human_review=False,
            suspected_layer=suspected_layer,
            details={"detail": str(row.get("detail") or "")},
        )
        findings.append(finding)
    return findings


def _pattern_finding(
    *,
    pattern_index: dict[str, dict[str, Any]],
    pattern_id: str,
    issue_type: str,
    impacted_scope: str,
    impacted_section: str,
    expected_structure_summary: str,
    evidence_refs: list[str],
    confidence: float,
    needs_human_review: bool,
    details: dict[str, Any],
    suspected_layer: str | None = None,
) -> dict[str, Any]:
    meta = pattern_index.get(pattern_id, {})
    hint = PATTERN_HINTS.get(pattern_id, {})
    return {
        "pattern_id": pattern_id,
        "category": str(meta.get("category") or hint.get("category") or "unmapped"),
        "issue_type": issue_type,
        "impacted_scope": impacted_scope,
        "impacted_section": impacted_section,
        "expected_structure_summary": expected_structure_summary,
        "suspected_layer": str(suspected_layer or hint.get("suspected_layer") or meta.get("root_cause_layer") or ""),
        "recommended_fix_direction": str(
            hint.get("recommended_fix_direction")
            or meta.get("validated_fix_strategy")
            or "Use the evidence pack to confirm a benchmark-safe generic fix."
        ),
        "evidence_refs": evidence_refs,
        "confidence": round(float(confidence), 2),
        "needs_human_review": bool(needs_human_review),
        "details": details,
    }


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for finding in findings:
        key = (
            str(finding.get("pattern_id") or ""),
            str(finding.get("issue_type") or ""),
            str(finding.get("impacted_scope") or ""),
            str(finding.get("impacted_section") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def _dedupe_pattern_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in candidates:
        key = (str(item.get("pattern_id") or ""), str(item.get("doc_key") or ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _pattern_index(pattern_library: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item.get("pattern_id") or ""): dict(item)
        for item in (pattern_library.get("patterns", []) if isinstance(pattern_library, dict) else [])
        if str(item.get("pattern_id") or "").strip()
    }


def _collect_pdf_paths(input_path: Path) -> list[Path]:
    if input_path.is_file():
        return [input_path] if input_path.suffix.lower() == ".pdf" else []
    if input_path.is_dir():
        return sorted(path.resolve() for path in input_path.glob("*.pdf"))
    return []


def _load_quality_profiles(path: Path) -> tuple[dict[str, Any], list[structural_quality_gate.Profile]]:
    payload = structural_quality_gate._load_profile_config(path)
    defaults = payload.get("defaults", {}) if isinstance(payload, dict) else {}
    profiles = structural_quality_gate._load_profiles(payload.get("profiles", []) if isinstance(payload, dict) else [])
    return defaults, profiles


def _load_fidelity_profiles(path: Path) -> list[fidelity_benchmark.FidelityProfile]:
    payload = _load_json(path)
    defaults = payload.get("defaults", {}) if isinstance(payload, dict) else {}
    return fidelity_benchmark._load_profiles(payload.get("profiles", []), defaults) if isinstance(payload, dict) else []


def _resolve_path(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    cwd_candidate = (Path.cwd() / expanded).resolve()
    parts = expanded.parts
    if cwd_candidate.exists():
        return cwd_candidate
    if parts and parts[0] == "prototype" and (Path.cwd() / "prototype").exists():
        return cwd_candidate
    return (PROJECT_DIR / expanded).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalize_name(value: str) -> str:
    name = Path(value).name
    return re.sub(r"^[0-9a-f]{32}_", "", name, flags=re.I).strip()


def _normalize_scope(scope: str) -> str:
    return str(scope or "").strip().upper()


def _join_scope_code(scope: str, code: str) -> str:
    clean_scope = _normalize_scope(scope)
    clean_code = str(code or "").strip()
    if not clean_code:
        return ""
    return f"{clean_scope}:{clean_code}" if clean_scope else clean_code


def _split_scope_code(scoped_code: str) -> tuple[str, str]:
    value = str(scoped_code or "").strip()
    if ":" not in value:
        return "", value
    scope, code = value.split(":", 1)
    return scope.strip().upper(), code.strip()


def _to_int(value: Any) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return int(float(text))
    except ValueError:
        return 0


def _slugify(value: str) -> str:
    lowered = _normalize_name(value).lower()
    lowered = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", lowered)
    lowered = lowered.strip("-")
    return lowered or "document"


def _sort_code_key(code: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in str(code or "").split("."):
        try:
            parts.append(int(token))
        except ValueError:
            parts.append(9999)
    return tuple(parts)


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    if a_start <= 0 or b_start <= 0:
        return False
    return not (a_end < b_start or b_end < a_start)


if __name__ == "__main__":
    raise SystemExit(main())
