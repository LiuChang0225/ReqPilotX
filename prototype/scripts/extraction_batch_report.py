#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
WORKTREE_ROOT = PROTOTYPE_DIR.parent
DEFAULT_BATCH_CONFIG = PROTOTYPE_DIR / "eval" / "extraction_batches.json"
DEFAULT_TAXONOMY_CONFIG = PROTOTYPE_DIR / "eval" / "extraction_issue_taxonomy.json"
DEFAULT_PATTERN_LIBRARY_CONFIG = PROTOTYPE_DIR / "eval" / "extraction_patterns.json"
UUID_PREFIX_RE = re.compile(r"^[0-9a-f]{32}_", re.I)

if str(PROTOTYPE_DIR) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE_DIR))

from scripts import extraction_diagnose


@dataclass(frozen=True)
class ResolvedDocument:
    doc_key: str
    profile: str
    path: Path
    logical_name: str
    source_root: str
    section_split: bool = False
    gate_tags: tuple[str, ...] = ()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run extraction regression as a reproducible batch with manifest + taxonomy rollup."
    )
    parser.add_argument(
        "--batch",
        default="legacy_benchmark_core",
        help="Batch id from extraction_batches.json.",
    )
    parser.add_argument(
        "--batch-config",
        type=Path,
        default=DEFAULT_BATCH_CONFIG,
        help="Batch manifest JSON.",
    )
    parser.add_argument(
        "--taxonomy-config",
        type=Path,
        default=DEFAULT_TAXONOMY_CONFIG,
        help="Issue taxonomy JSON.",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("eval/extraction_batches"),
        help="Output root relative to prototype/ unless absolute.",
    )
    parser.add_argument(
        "--run-id",
        default=datetime.now().strftime("batch_%Y%m%d_%H%M%S"),
        help="Run id used under out-root.",
    )
    parser.add_argument(
        "--shared-repo-root",
        type=Path,
        default=None,
        help="Optional override for the shared repo checkout root.",
    )
    parser.add_argument(
        "--keep-staged-dir",
        action="store_true",
        help="Keep the staged docs directory instead of cleaning it up.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    batch_config_path = _resolve_path(args.batch_config)
    taxonomy_path = _resolve_path(args.taxonomy_config)
    batch_config = _load_json(batch_config_path)
    taxonomy = _load_json(taxonomy_path)
    batch = _require_batch(batch_config, args.batch)

    shared_repo_root = resolve_shared_repo_root(args.shared_repo_root)
    python_executable = preferred_python_executable(shared_repo_root)
    candidate_roots = resolve_candidate_roots(batch_config, shared_repo_root)
    resolved_docs, missing_doc_keys = resolve_batch_documents(batch, candidate_roots)

    out_root = _resolve_output_root(args.out_root)
    run_dir = (out_root / args.run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    staged_dir = run_dir / "staged_docs"
    staged_manifest = stage_documents(resolved_docs, staged_dir)

    if missing_doc_keys:
        summary = {
            "run_id": args.run_id,
            "batch_id": batch["id"],
            "description": batch.get("description", ""),
            "shared_repo_root": str(shared_repo_root),
            "resolved_docs": staged_manifest,
            "missing_doc_keys": missing_doc_keys,
            "gates": {
                "batch-report": {"status": "FAIL", "detail": f"missing_docs={missing_doc_keys}"},
                "regression-suite": {"status": "FAIL", "detail": "skipped because required docs are missing"},
                "oil-pump-benchmark": {"status": "FAIL", "detail": "skipped because required docs are missing"},
            },
        }
        _write_json(run_dir / "batch_summary.json", summary)
        (run_dir / "batch_summary.md").write_text(_render_failure_md(summary), encoding="utf-8")
        print(f"[batch] missing docs: {', '.join(missing_doc_keys)}")
        return 1

    gate_expectations = batch.get("gate_expectations", {}) if isinstance(batch, dict) else {}
    suite_result = run_regression_suite(
        staged_dir=staged_dir,
        run_dir=run_dir,
        gate_expectations=gate_expectations,
        exclude_name_regex=batch.get("exclude_name_regex", []),
        python_executable=python_executable,
    )
    suite_dir = Path(str(suite_result["suite_dir"]))
    suite_summary = dict(suite_result["suite_summary"])
    quality_summary = dict(suite_result["quality_summary"])
    fidelity_summary = dict(suite_result["fidelity_summary"])
    suite_exit_codes = dict(suite_result["exit_codes"])
    section_split_results = run_section_split_targets(
        resolved_docs,
        staged_dir,
        run_dir,
        python_executable=python_executable,
    )
    quality_doc_rows = _read_csv_rows(suite_dir / "quality_gate" / "structural_quality_docs.csv")
    quality_issue_rows = _read_csv_rows(suite_dir / "quality_gate" / "structural_quality_issues.csv")
    fidelity_doc_rows = _read_csv_rows(suite_dir / "fidelity" / "fidelity_docs.csv")
    fidelity_issue_rows = _read_csv_rows(suite_dir / "fidelity" / "fidelity_issues.csv")

    section_issue_rows = build_section_split_issue_rows(section_split_results)
    issue_rollup_rows, issue_summary = build_issue_rollup(
        quality_issue_rows=quality_issue_rows,
        fidelity_issue_rows=fidelity_issue_rows,
        section_issue_rows=section_issue_rows,
        taxonomy=taxonomy,
    )
    document_rollup_rows = build_document_rollup(
        resolved_docs=resolved_docs,
        quality_doc_rows=quality_doc_rows,
        fidelity_doc_rows=fidelity_doc_rows,
        section_split_results=section_split_results,
    )
    quality_defaults, quality_profiles = extraction_diagnose._load_quality_profiles(
        extraction_diagnose.DEFAULT_QUALITY_PROFILE_CONFIG
    )
    fidelity_profiles = extraction_diagnose._load_fidelity_profiles(
        extraction_diagnose.DEFAULT_FIDELITY_PROFILE_CONFIG
    )
    pattern_library = _load_json(DEFAULT_PATTERN_LIBRARY_CONFIG)
    diagnosis_result = extraction_diagnose.run_batch_extraction_audit(
        pdf_paths=[staged_dir / doc.logical_name for doc in resolved_docs],
        out_dir=run_dir,
        batch_config=batch_config,
        issue_taxonomy=taxonomy,
        pattern_library=pattern_library,
        quality_defaults=quality_defaults,
        quality_profiles=quality_profiles,
        fidelity_profiles=fidelity_profiles,
        batch_id=batch["id"],
        document_manifests=staged_manifest,
    )
    _attach_diagnosis_rollup(document_rollup_rows, diagnosis_result.get("documents", []))

    oil_pump_row = next(
        (row for row in document_rollup_rows if "oil-pump-benchmark" in str(row.get("gate_tags") or "")),
        None,
    )
    required_profiles = sorted({doc.profile for doc in resolved_docs if doc.profile})
    matched_profiles = sorted(set(str(name) for name in fidelity_summary.get("matched_profiles", [])))
    missing_required_profiles = sorted(set(required_profiles) - set(matched_profiles))
    section_split_pass = all(bool(item.get("passed")) for item in section_split_results)
    regression_pass = (
        bool(quality_summary.get("gate_pass"))
        and bool(fidelity_summary.get("overall_pass"))
        and not missing_required_profiles
        and section_split_pass
    )
    oil_pump_pass = bool(
        oil_pump_row
        and oil_pump_row.get("quality_pass")
        and oil_pump_row.get("fidelity_pass")
    )

    gates = {
        "batch-report": {
            "status": "PASS",
            "detail": f"docs={len(resolved_docs)} issues={len(issue_rollup_rows)}",
        },
        "regression-suite": {
            "status": "PASS" if regression_pass else "FAIL",
            "detail": (
                f"quality_pass={bool(quality_summary.get('gate_pass'))} "
                f"fidelity_pass={bool(fidelity_summary.get('overall_pass'))} "
                f"section_split_pass={section_split_pass} "
                f"quality_exit_code={suite_exit_codes.get('quality', 0)} "
                f"fidelity_exit_code={suite_exit_codes.get('fidelity', 0)} "
                f"missing_required_profiles={missing_required_profiles}"
            ),
        },
        "oil-pump-benchmark": {
            "status": "PASS" if oil_pump_pass else "FAIL",
            "detail": (
                "not resolved"
                if oil_pump_row is None
                else (
                    f"quality_pass={oil_pump_row.get('quality_pass')} "
                    f"fidelity_pass={oil_pump_row.get('fidelity_pass')} "
                    f"pdf={oil_pump_row.get('pdf_name')}"
                )
            ),
        },
    }

    batch_summary = {
        "run_id": args.run_id,
        "batch_id": batch["id"],
        "description": batch.get("description", ""),
        "shared_repo_root": str(shared_repo_root),
        "python_executable": str(python_executable),
        "staged_dir": str(staged_dir),
        "resolved_docs": staged_manifest,
        "required_profiles": required_profiles,
        "matched_profiles": matched_profiles,
        "missing_required_profiles": missing_required_profiles,
        "suite_summary": suite_summary,
        "quality_summary": quality_summary,
        "fidelity_summary": fidelity_summary,
        "section_split_summary": summarize_section_split(section_split_results),
        "issue_summary": issue_summary,
        "diagnosis_summary": diagnosis_result.get("summary", {}),
        "diagnosis_paths": diagnosis_result.get("paths", {}),
        "gates": gates,
    }

    _write_json(run_dir / "batch_summary.json", batch_summary)
    _write_csv(run_dir / "document_rollup.csv", document_rollup_rows)
    _write_csv(run_dir / "issue_rollup.csv", issue_rollup_rows)
    (run_dir / "batch_summary.md").write_text(
        _render_batch_md(batch_summary, document_rollup_rows),
        encoding="utf-8",
    )

    if not args.keep_staged_dir:
        shutil.rmtree(staged_dir, ignore_errors=True)

    print(f"[batch] id: {batch['id']}")
    print(f"[batch] run: {args.run_id}")
    print(f"[batch] docs: {len(resolved_docs)}")
    print(f"[batch] python: {python_executable}")
    print(f"[batch] regression-suite: {gates['regression-suite']['status']}")
    print(f"[batch] oil-pump-benchmark: {gates['oil-pump-benchmark']['status']}")
    print(f"[batch] summary json: {run_dir / 'batch_summary.json'}")
    print(f"[batch] summary md: {run_dir / 'batch_summary.md'}")
    return 0 if all(gate["status"] == "PASS" for gate in gates.values()) else 1


def resolve_shared_repo_root(override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=str(WORKTREE_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (WORKTREE_ROOT / common_dir).resolve()
    return common_dir.parent.resolve()


def preferred_python_executable(shared_repo_root: Path) -> Path:
    venv_python = shared_repo_root / "prototype" / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python
    return Path(sys.executable)


def resolve_candidate_roots(config: dict[str, Any], shared_repo_root: Path) -> list[dict[str, str | Path]]:
    roots: list[dict[str, str | Path]] = []
    for entry in config.get("candidate_roots", []):
        root_kind = str(entry.get("root") or "worktree").strip()
        relative_path = Path(str(entry.get("path") or "")).expanduser()
        name = str(entry.get("name") or relative_path.as_posix())
        optional = bool(entry.get("optional", False))
        if root_kind == "shared_repo":
            resolved = (shared_repo_root / relative_path).resolve()
        elif root_kind == "worktree":
            resolved = (WORKTREE_ROOT / relative_path).resolve()
        elif root_kind == "home":
            resolved = (Path.home() / relative_path).resolve()
        else:
            raise SystemExit(f"Unsupported candidate root kind: {root_kind}")
        if not resolved.exists():
            if optional:
                continue
            raise SystemExit(f"Candidate root not found: {resolved}")
        roots.append({"name": name, "path": resolved})
    if not roots:
        raise SystemExit("No candidate roots resolved for extraction batch.")
    return roots


def resolve_batch_documents(
    batch: dict[str, Any],
    candidate_roots: list[dict[str, str | Path]],
) -> tuple[list[ResolvedDocument], list[str]]:
    resolved_docs: list[ResolvedDocument] = []
    missing_doc_keys: list[str] = []
    documents = batch.get("documents", []) if isinstance(batch, dict) else []
    for doc in documents:
        doc_key = str(doc.get("doc_key") or "").strip()
        if not doc_key:
            continue
        match_regex = str(doc.get("match_regex") or "").strip()
        if not match_regex:
            raise SystemExit(f"Document {doc_key} is missing match_regex.")
        pattern = re.compile(match_regex, re.I)
        matches: list[tuple[int, str, str, Path, str, str]] = []
        for root_index, root_entry in enumerate(candidate_roots):
            root_path = Path(str(root_entry["path"]))
            root_name = str(root_entry["name"])
            for path in sorted(root_path.glob("*.pdf")):
                logical_name = normalize_logical_name(path.name)
                if pattern.search(logical_name):
                    matches.append(
                        (
                            root_index,
                            logical_name.lower(),
                            path.name.lower(),
                            path.resolve(),
                            logical_name,
                            root_name,
                        )
                    )
        if not matches:
            if bool(doc.get("required", True)):
                missing_doc_keys.append(doc_key)
            continue
        _, _, _, chosen_path, logical_name, source_root = sorted(matches)[0]
        resolved_docs.append(
            ResolvedDocument(
                doc_key=doc_key,
                profile=str(doc.get("profile") or "").strip(),
                path=chosen_path,
                logical_name=logical_name,
                source_root=source_root,
                section_split=bool(doc.get("section_split", False)),
                gate_tags=tuple(str(tag) for tag in doc.get("gate_tags", []) or []),
            )
        )
    return resolved_docs, missing_doc_keys


def stage_documents(resolved_docs: list[ResolvedDocument], staged_dir: Path) -> list[dict[str, Any]]:
    shutil.rmtree(staged_dir, ignore_errors=True)
    staged_dir.mkdir(parents=True, exist_ok=True)
    manifest_rows: list[dict[str, Any]] = []
    for doc in resolved_docs:
        staged_path = staged_dir / doc.logical_name
        _link_or_copy(doc.path, staged_path)
        manifest_rows.append(
            {
                "doc_key": doc.doc_key,
                "profile": doc.profile,
                "pdf_name": doc.logical_name,
                "source_path": str(doc.path),
                "staged_path": str(staged_path),
                "source_root": doc.source_root,
                "section_split": doc.section_split,
                "gate_tags": list(doc.gate_tags),
            }
        )
    _write_json(staged_dir.parent / "resolved_documents.json", manifest_rows)
    return manifest_rows


def run_regression_suite(
    *,
    staged_dir: Path,
    run_dir: Path,
    gate_expectations: dict[str, Any],
    exclude_name_regex: list[str],
    python_executable: Path,
) -> dict[str, Any]:
    env = _build_env()
    suite_dir = run_dir / "regression_suite"
    quality_out = suite_dir / "quality_gate"
    fidelity_out = suite_dir / "fidelity"
    suite_dir.mkdir(parents=True, exist_ok=True)

    exclude_patterns = list(exclude_name_regex or [])
    default_pattern = "PRINT_21033420"
    if default_pattern not in exclude_patterns:
        exclude_patterns.append(default_pattern)

    quality_cmd = [
        str(python_executable),
        str(PROTOTYPE_DIR / "scripts" / "structural_quality_gate.py"),
        str(staged_dir),
        "--profile-config",
        str(PROTOTYPE_DIR / "eval" / "quality_gate_profiles.json"),
        "--out-dir",
        str(quality_out),
        "--fail-below-score-pct",
        str(float(gate_expectations.get("min_quality_score_pct", 95.0))),
        "--fail-on-doc-score-below-pct",
        str(float(gate_expectations.get("min_quality_doc_score_pct", 70.0))),
        "--max-high-severity-issues",
        str(int(gate_expectations.get("max_quality_high_severity_issues", 0))),
    ]
    fidelity_cmd = [
        str(python_executable),
        str(PROTOTYPE_DIR / "scripts" / "fidelity_benchmark.py"),
        str(staged_dir),
        "--profile-config",
        str(PROTOTYPE_DIR / "eval" / "fidelity_profiles.json"),
        "--out-dir",
        str(fidelity_out),
        "--allow-missing-profiles",
    ]
    for pattern in exclude_patterns:
        quality_cmd.extend(["--exclude-name-regex", str(pattern)])
        fidelity_cmd.extend(["--exclude-name-regex", str(pattern)])

    quality_result = subprocess.run(quality_cmd, cwd=str(WORKTREE_ROOT), check=False, env=env)
    fidelity_result = subprocess.run(fidelity_cmd, cwd=str(WORKTREE_ROOT), check=False, env=env)

    quality_summary_path = quality_out / "structural_quality_summary.json"
    fidelity_summary_path = fidelity_out / "fidelity_summary.json"
    quality_summary = _load_json(quality_summary_path) if quality_summary_path.exists() else {}
    fidelity_summary = _load_json(fidelity_summary_path) if fidelity_summary_path.exists() else {}

    suite_payload = {
        "run_id": "regression_suite",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "pdf_dir": str(staged_dir),
        "exclude_name_regex": exclude_patterns,
        "paths": {
            "quality_summary_json": str(quality_summary_path),
            "fidelity_summary_json": str(fidelity_summary_path),
            "quality_summary_md": str(quality_out / "structural_quality_summary.md"),
            "fidelity_summary_md": str(fidelity_out / "fidelity_summary.md"),
        },
        "quality_gate": {
            "docs": quality_summary.get("docs", 0),
            "checks_passed": quality_summary.get("checks_passed", 0),
            "checks_total": quality_summary.get("checks_total", 0),
            "gate_score_pct": quality_summary.get("gate_score_pct", 0.0),
            "high_severity_issues": quality_summary.get("high_severity_issues", 0),
            "gate_pass": quality_summary.get("gate_pass", False),
        },
        "fidelity_gate": {
            "target_docs": fidelity_summary.get("target_docs", 0),
            "checks_passed": fidelity_summary.get("checks_passed", 0),
            "checks_total": fidelity_summary.get("checks_total", 0),
            "score_pct": fidelity_summary.get("score_pct", 0.0),
            "failed_docs": fidelity_summary.get("failed_docs", 0),
            "overall_pass": fidelity_summary.get("overall_pass", False),
        },
        "exit_codes": {
            "quality": int(quality_result.returncode),
            "fidelity": int(fidelity_result.returncode),
        },
    }
    _write_json(suite_dir / "regression_suite_summary.json", suite_payload)
    (suite_dir / "regression_suite_summary.md").write_text(
        _render_regression_suite_md(suite_payload),
        encoding="utf-8",
    )
    return {
        "suite_dir": str(suite_dir),
        "suite_summary": suite_payload,
        "quality_summary": quality_summary,
        "fidelity_summary": fidelity_summary,
        "exit_codes": suite_payload["exit_codes"],
    }


def run_section_split_targets(
    resolved_docs: list[ResolvedDocument],
    staged_dir: Path,
    run_dir: Path,
    *,
    python_executable: Path,
) -> list[dict[str, Any]]:
    env = _build_env()
    out_dir = run_dir / "section_split"
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for doc in resolved_docs:
        if not doc.section_split:
            continue
        json_out = out_dir / f"{doc.doc_key}.json"
        csv_out = out_dir / f"{doc.doc_key}.csv"
        cmd = [
            str(python_executable),
            str(PROTOTYPE_DIR / "scripts" / "section_split_regression.py"),
            str(staged_dir / doc.logical_name),
            "--json-out",
            str(json_out),
            "--csv-out",
            str(csv_out),
        ]
        subprocess.run(cmd, cwd=str(WORKTREE_ROOT), check=True, env=env)
        payload = _load_json(json_out)
        results.append(
            {
                "doc_key": doc.doc_key,
                "profile": doc.profile,
                "pdf_name": doc.logical_name,
                "passed": bool(payload.get("summary", {}).get("passed")),
                "score_pct": _to_float(payload.get("summary", {}).get("score_pct")),
                "checks_passed": _to_int(payload.get("summary", {}).get("passed_checks")),
                "checks_total": _to_int(payload.get("summary", {}).get("total_checks")),
                "checks": payload.get("checks", []),
                "json_path": str(json_out),
                "csv_path": str(csv_out),
            }
        )
    _write_json(out_dir / "section_split_summary.json", summarize_section_split(results))
    return results


def summarize_section_split(results: list[dict[str, Any]]) -> dict[str, Any]:
    checks_total = sum(_to_int(item.get("checks_total")) for item in results)
    checks_passed = sum(_to_int(item.get("checks_passed")) for item in results)
    return {
        "targets": len(results),
        "targets_passed": sum(1 for item in results if bool(item.get("passed"))),
        "checks_passed": checks_passed,
        "checks_total": checks_total,
        "score_pct": round((checks_passed / checks_total) * 100, 2) if checks_total else 100.0,
        "results": [
            {
                "doc_key": item.get("doc_key"),
                "pdf_name": item.get("pdf_name"),
                "passed": item.get("passed"),
                "score_pct": item.get("score_pct"),
            }
            for item in results
        ],
    }


def build_section_split_issue_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for item in results:
        for check in item.get("checks", []) or []:
            if bool(check.get("passed")):
                continue
            issues.append(
                {
                    "gate": "section_split",
                    "pdf_name": str(item.get("pdf_name") or ""),
                    "profile": str(item.get("profile") or ""),
                    "issue_type": "section_split_failed",
                    "severity": "high",
                    "appendix_scope": "",
                    "section_code": "",
                    "detail": f"{check.get('name')}: {check.get('detail')}",
                }
            )
    return issues


def build_issue_rollup(
    *,
    quality_issue_rows: list[dict[str, str]],
    fidelity_issue_rows: list[dict[str, str]],
    section_issue_rows: list[dict[str, str]],
    taxonomy: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enriched_rows: list[dict[str, Any]] = []
    sources = [
        ("quality_gate", quality_issue_rows),
        ("fidelity", fidelity_issue_rows),
        ("section_split", section_issue_rows),
    ]
    for gate_name, rows in sources:
        for row in rows:
            issue_type = str(row.get("issue_type") or "").strip()
            category_key, category_label = classify_issue(issue_type, taxonomy)
            enriched_rows.append(
                {
                    "gate": gate_name,
                    "pdf_name": normalize_logical_name(str(row.get("pdf_name") or "")),
                    "profile": str(row.get("profile") or ""),
                    "category": category_key,
                    "category_label": category_label,
                    "issue_type": issue_type,
                    "severity": str(row.get("severity") or "info").lower(),
                    "appendix_scope": str(row.get("appendix_scope") or ""),
                    "section_code": str(row.get("section_code") or ""),
                    "detail": str(row.get("detail") or ""),
                }
            )
    enriched_rows.sort(
        key=lambda row: (
            str(row.get("severity") or ""),
            str(row.get("category") or ""),
            str(row.get("issue_type") or ""),
            str(row.get("pdf_name") or ""),
        )
    )
    category_counts: dict[str, int] = {}
    severity_counts: dict[str, int] = {}
    gate_counts: dict[str, int] = {}
    for row in enriched_rows:
        category = str(row.get("category") or "")
        severity = str(row.get("severity") or "")
        gate_name = str(row.get("gate") or "")
        category_counts[category] = category_counts.get(category, 0) + 1
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        gate_counts[gate_name] = gate_counts.get(gate_name, 0) + 1
    summary = {
        "total_issues": len(enriched_rows),
        "category_counts": dict(sorted(category_counts.items())),
        "severity_counts": dict(sorted(severity_counts.items())),
        "gate_counts": dict(sorted(gate_counts.items())),
        "unmapped_issue_types": sorted(
            {
                str(row.get("issue_type") or "")
                for row in enriched_rows
                if str(row.get("category") or "") == str(taxonomy.get("default_category", "unmapped"))
            }
        ),
    }
    return enriched_rows, summary


def build_document_rollup(
    *,
    resolved_docs: list[ResolvedDocument],
    quality_doc_rows: list[dict[str, str]],
    fidelity_doc_rows: list[dict[str, str]],
    section_split_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    quality_by_name = {
        normalize_logical_name(str(row.get("pdf_name") or "")): row
        for row in quality_doc_rows
    }
    fidelity_by_name = {
        normalize_logical_name(str(row.get("pdf_name") or "")): row
        for row in fidelity_doc_rows
    }
    section_by_name = {
        normalize_logical_name(str(item.get("pdf_name") or "")): item
        for item in section_split_results
    }
    rows: list[dict[str, Any]] = []
    for doc in resolved_docs:
        quality = quality_by_name.get(doc.logical_name, {})
        fidelity = fidelity_by_name.get(doc.logical_name, {})
        section = section_by_name.get(doc.logical_name)
        quality_pass = bool(
            quality and _to_int(quality.get("checks_passed")) == _to_int(quality.get("checks_total"))
        )
        fidelity_pass = bool(fidelity and _to_boolish(fidelity.get("passed")))
        rows.append(
            {
                "doc_key": doc.doc_key,
                "profile": doc.profile,
                "pdf_name": doc.logical_name,
                "source_path": str(doc.path),
                "source_root": doc.source_root,
                "gate_tags": "|".join(doc.gate_tags),
                "quality_checks_passed": _to_int(quality.get("checks_passed")),
                "quality_checks_total": _to_int(quality.get("checks_total")),
                "quality_score_pct": _to_float(quality.get("score_pct")),
                "quality_pass": quality_pass,
                "fidelity_checks_passed": _to_int(fidelity.get("checks_passed")),
                "fidelity_checks_total": _to_int(fidelity.get("checks_total")),
                "fidelity_score_pct": _to_float(fidelity.get("score_pct")),
                "fidelity_pass": fidelity_pass,
                "section_split_target": bool(section is not None),
                "section_split_pass": bool(section.get("passed")) if section else True,
                "section_split_score_pct": _to_float(section.get("score_pct")) if section else 100.0,
            }
        )
    return rows


def _attach_diagnosis_rollup(
    document_rows: list[dict[str, Any]],
    diagnosis_docs: list[dict[str, Any]],
) -> None:
    by_name = {
        normalize_logical_name(str(item.get("pdf_name") or "")): item
        for item in diagnosis_docs
    }
    for row in document_rows:
        diagnosis = by_name.get(normalize_logical_name(str(row.get("pdf_name") or "")), {})
        findings = diagnosis.get("findings", []) or []
        layers = sorted({str(item.get("suspected_layer") or "") for item in findings if str(item.get("suspected_layer") or "")})
        row["diagnosis_finding_count"] = len(findings)
        row["diagnosis_pattern_candidate_count"] = len(diagnosis.get("pattern_candidates", []) or [])
        row["diagnosis_needs_human_review"] = bool(diagnosis.get("status", {}).get("needs_human_review", False))
        row["diagnosis_layers"] = "|".join(layers)


def classify_issue(issue_type: str, taxonomy: dict[str, Any]) -> tuple[str, str]:
    categories = taxonomy.get("categories", {}) if isinstance(taxonomy, dict) else {}
    default_category = str(taxonomy.get("default_category", "unmapped"))
    mapping = taxonomy.get("issue_types", {}) if isinstance(taxonomy, dict) else {}
    category_key = str(mapping.get(issue_type) or default_category)
    category_label = str(categories.get(category_key) or category_key)
    return category_key, category_label


def normalize_logical_name(name: str) -> str:
    cleaned = UUID_PREFIX_RE.sub("", Path(name).name)
    return cleaned.strip()


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROTOTYPE_DIR) if not current else f"{str(PROTOTYPE_DIR)}:{current}"
    return env


def _require_batch(config: dict[str, Any], batch_id: str) -> dict[str, Any]:
    for batch in config.get("batches", []):
        if str(batch.get("id") or "") == batch_id:
            return batch
    raise SystemExit(f"Batch not found: {batch_id}")


def _resolve_output_root(path: Path) -> Path:
    return path.expanduser().resolve() if path.is_absolute() else (PROTOTYPE_DIR / path).resolve()


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve() if path.is_absolute() else (WORKTREE_ROOT / path).resolve()


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as fp:
        reader = csv.DictReader(fp)
        return [dict(row) for row in reader]


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def _link_or_copy(src: Path, dst: Path) -> None:
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        dst.symlink_to(src)
    except OSError:
        shutil.copy2(src, dst)


def _render_failure_md(summary: dict[str, Any]) -> str:
    lines = [
        "# Extraction Batch Summary",
        "",
        f"- batch_id: `{summary.get('batch_id', '')}`",
        f"- run_id: `{summary.get('run_id', '')}`",
        f"- missing_doc_keys: `{summary.get('missing_doc_keys', [])}`",
        "",
        "## Gates",
    ]
    for gate_name, gate in (summary.get("gates", {}) or {}).items():
        lines.append(f"- {gate_name}: `{gate.get('status', '')}` | {gate.get('detail', '')}")
    return "\n".join(lines) + "\n"


def _render_batch_md(summary: dict[str, Any], document_rows: list[dict[str, Any]]) -> str:
    lines = [
        "# Extraction Batch Summary",
        "",
        f"- batch_id: `{summary.get('batch_id', '')}`",
        f"- run_id: `{summary.get('run_id', '')}`",
        f"- description: `{summary.get('description', '')}`",
        f"- shared_repo_root: `{summary.get('shared_repo_root', '')}`",
        "",
        "## Gates",
    ]
    for gate_name, gate in (summary.get("gates", {}) or {}).items():
        lines.append(f"- {gate_name}: `{gate.get('status', '')}` | {gate.get('detail', '')}")
    lines.extend(
        [
            "",
            "## Issue Rollup",
            f"- total_issues: `{summary.get('issue_summary', {}).get('total_issues', 0)}`",
            f"- category_counts: `{summary.get('issue_summary', {}).get('category_counts', {})}`",
            f"- severity_counts: `{summary.get('issue_summary', {}).get('severity_counts', {})}`",
            "",
            "## Diagnosis",
            f"- findings: `{summary.get('diagnosis_summary', {}).get('total_findings', 0)}`",
            f"- pattern_candidates: `{summary.get('diagnosis_summary', {}).get('pattern_candidate_count', 0)}`",
            f"- suspected_layer_counts: `{summary.get('diagnosis_summary', {}).get('suspected_layer_counts', {})}`",
            f"- diagnosis_summary: `{summary.get('diagnosis_paths', {}).get('summary_md', '')}`",
            "",
            "## Section Split",
            f"- summary: `{summary.get('section_split_summary', {})}`",
            "",
            "## Documents",
        ]
    )
    for row in document_rows:
        lines.append(
            "- "
            f"`{row.get('doc_key', '')}` -> `{row.get('pdf_name', '')}` | "
            f"quality `{row.get('quality_score_pct', 0.0)}%` | "
            f"fidelity `{row.get('fidelity_score_pct', 0.0)}%` | "
            f"section_split `{row.get('section_split_pass', True)}` | "
            f"diagnosis `{row.get('diagnosis_finding_count', 0)}` findings"
        )
    return "\n".join(lines) + "\n"


def _render_regression_suite_md(payload: dict[str, Any]) -> str:
    quality = payload.get("quality_gate", {})
    fidelity = payload.get("fidelity_gate", {})
    exit_codes = payload.get("exit_codes", {})
    lines = [
        "# Regression Suite Summary",
        "",
        f"- run_id: `{payload.get('run_id', '')}`",
        f"- timestamp: `{payload.get('timestamp', '')}`",
        f"- pdf_dir: `{payload.get('pdf_dir', '')}`",
        f"- exclude_name_regex: `{payload.get('exclude_name_regex', [])}`",
        f"- exit_codes: `{exit_codes}`",
        "",
        "## Structural Quality Gate",
        f"- docs: `{quality.get('docs', 0)}`",
        f"- checks: `{quality.get('checks_passed', 0)}/{quality.get('checks_total', 0)}`",
        f"- gate_score_pct: `{quality.get('gate_score_pct', 0.0)}%`",
        f"- high_severity_issues: `{quality.get('high_severity_issues', 0)}`",
        f"- gate_pass: `{quality.get('gate_pass', False)}`",
        "",
        "## Fidelity Benchmark",
        f"- target_docs: `{fidelity.get('target_docs', 0)}`",
        f"- checks: `{fidelity.get('checks_passed', 0)}/{fidelity.get('checks_total', 0)}`",
        f"- score_pct: `{fidelity.get('score_pct', 0.0)}%`",
        f"- failed_docs: `{fidelity.get('failed_docs', 0)}`",
        f"- overall_pass: `{fidelity.get('overall_pass', False)}`",
        "",
        "## Artifacts",
        f"- quality summary: `{payload.get('paths', {}).get('quality_summary_md', '')}`",
        f"- fidelity summary: `{payload.get('paths', {}).get('fidelity_summary_md', '')}`",
    ]
    return "\n".join(lines) + "\n"


def _to_boolish(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "pass"}


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
    raise SystemExit(main())
