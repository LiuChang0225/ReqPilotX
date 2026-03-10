from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run structural gate + fidelity benchmark in one command."
    )
    parser.add_argument("pdf_dir", type=Path, help="Directory containing PDF files.")
    parser.add_argument(
        "--run-id",
        type=str,
        default=datetime.now().strftime("r%Y%m%d_%H%M%S"),
        help="Run id used to create output folder.",
    )
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("eval/regression_suite"),
        help="Output root for suite reports.",
    )
    parser.add_argument(
        "--quality-profile-config",
        type=Path,
        default=Path("eval/quality_gate_profiles.json"),
        help="Profile JSON for structural quality gate.",
    )
    parser.add_argument(
        "--fidelity-profile-config",
        type=Path,
        default=Path("eval/fidelity_profiles.json"),
        help="Profile JSON for fidelity benchmark.",
    )
    parser.add_argument(
        "--exclude-name-regex",
        action="append",
        default=[],
        help="Exclude PDFs whose name matches regex. Can be repeated.",
    )
    parser.add_argument(
        "--no-default-text-only-exclude",
        action="store_true",
        help="Do not auto-exclude default scan-like doc pattern.",
    )
    parser.add_argument(
        "--min-quality-score-pct",
        type=float,
        default=95.0,
        help="Hard gate: fail if structural gate score is below this threshold.",
    )
    parser.add_argument(
        "--min-quality-doc-score-pct",
        type=float,
        default=70.0,
        help="Hard gate: fail if any document structural score is below this threshold.",
    )
    parser.add_argument(
        "--max-quality-high-severity-issues",
        type=int,
        default=0,
        help="Hard gate: fail if high-severity structural issues exceed this value.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    pdf_dir = args.pdf_dir.expanduser().resolve()
    out_root = (project_dir / args.out_root).resolve()
    run_dir = (out_root / args.run_id).resolve()
    quality_out = run_dir / "quality_gate"
    fidelity_out = run_dir / "fidelity"
    run_dir.mkdir(parents=True, exist_ok=True)

    exclude_patterns = list(args.exclude_name_regex or [])
    if not args.no_default_text_only_exclude:
        default_pattern = "PRINT_21033420"
        if default_pattern not in exclude_patterns:
            exclude_patterns.append(default_pattern)

    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(project_dir)
        if not current_pythonpath
        else f"{str(project_dir)}:{current_pythonpath}"
    )

    structural_cmd = [
        sys.executable,
        str(project_dir / "scripts" / "structural_quality_gate.py"),
        str(pdf_dir),
        "--profile-config",
        str((project_dir / args.quality_profile_config).resolve()),
        "--out-dir",
        str(quality_out),
        "--fail-below-score-pct",
        str(float(args.min_quality_score_pct)),
        "--fail-on-doc-score-below-pct",
        str(float(args.min_quality_doc_score_pct)),
        "--max-high-severity-issues",
        str(int(args.max_quality_high_severity_issues)),
    ]
    fidelity_cmd = [
        sys.executable,
        str(project_dir / "scripts" / "fidelity_benchmark.py"),
        str(pdf_dir),
        "--profile-config",
        str((project_dir / args.fidelity_profile_config).resolve()),
        "--out-dir",
        str(fidelity_out),
        "--allow-missing-profiles",
    ]
    for pattern in exclude_patterns:
        structural_cmd.extend(["--exclude-name-regex", pattern])
        fidelity_cmd.extend(["--exclude-name-regex", pattern])

    _run(structural_cmd, project_dir, env)
    _run(fidelity_cmd, project_dir, env)

    quality_summary_path = quality_out / "structural_quality_summary.json"
    fidelity_summary_path = fidelity_out / "fidelity_summary.json"
    quality_summary = _load_json(quality_summary_path)
    fidelity_summary = _load_json(fidelity_summary_path)

    suite_payload = {
        "run_id": args.run_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "pdf_dir": str(pdf_dir),
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
            "gate_pass": quality_summary.get("gate_pass", True),
        },
        "fidelity_gate": {
            "target_docs": fidelity_summary.get("target_docs", 0),
            "checks_passed": fidelity_summary.get("checks_passed", 0),
            "checks_total": fidelity_summary.get("checks_total", 0),
            "score_pct": fidelity_summary.get("score_pct", 0.0),
            "failed_docs": fidelity_summary.get("failed_docs", 0),
            "overall_pass": fidelity_summary.get("overall_pass", False),
        },
    }

    suite_json = run_dir / "regression_suite_summary.json"
    suite_md = run_dir / "regression_suite_summary.md"
    suite_json.write_text(
        json.dumps(suite_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    suite_md.write_text(_render_md(suite_payload), encoding="utf-8")

    print(f"[suite] run id: {args.run_id}")
    print(
        "[suite] quality gate: "
        f"{suite_payload['quality_gate']['checks_passed']}/"
        f"{suite_payload['quality_gate']['checks_total']} "
        f"({suite_payload['quality_gate']['gate_score_pct']:.2f}%)"
    )
    print(
        "[suite] fidelity gate: "
        f"{suite_payload['fidelity_gate']['checks_passed']}/"
        f"{suite_payload['fidelity_gate']['checks_total']} "
        f"({suite_payload['fidelity_gate']['score_pct']:.2f}%)"
    )
    print(f"[suite] summary json: {suite_json}")
    print(f"[suite] summary md: {suite_md}")


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, env=env)


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"summary not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _render_md(payload: dict[str, Any]) -> str:
    quality = payload.get("quality_gate", {})
    fidelity = payload.get("fidelity_gate", {})
    lines = [
        "# Regression Suite Summary",
        "",
        f"- run_id: `{payload.get('run_id', '')}`",
        f"- timestamp: `{payload.get('timestamp', '')}`",
        f"- pdf_dir: `{payload.get('pdf_dir', '')}`",
        f"- exclude_name_regex: `{payload.get('exclude_name_regex', [])}`",
        "",
        "## Structural Quality Gate",
        f"- docs: `{quality.get('docs', 0)}`",
        f"- checks: `{quality.get('checks_passed', 0)}/{quality.get('checks_total', 0)}`",
        f"- gate_score_pct: `{quality.get('gate_score_pct', 0.0)}%`",
        f"- high_severity_issues: `{quality.get('high_severity_issues', 0)}`",
        f"- gate_pass: `{quality.get('gate_pass', True)}`",
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


if __name__ == "__main__":
    main()
