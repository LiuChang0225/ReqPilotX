from __future__ import annotations

import argparse
import json
import unittest
from datetime import datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description="Run table asset regression tests and write a summary artifact.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("eval/table_assets/latest"),
        help="Directory for regression summary artifacts.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    out_dir = (project_dir / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(project_dir / "tests"),
        pattern="test_table_assets.py",
        top_level_dir=str(project_dir),
    )
    result = unittest.TestResult()
    suite.run(result)

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        # This command is a gate, so an unexecuted required regression is not
        # equivalent to a passing regression.
        "overall_pass": not result.failures and not result.errors and not result.skipped,
        "failure_tests": [str(test) for test, _ in result.failures],
        "error_tests": [str(test) for test, _ in result.errors],
        "skipped_tests": [f"{str(test)} :: {reason}" for test, reason in result.skipped],
    }
    summary_json = out_dir / "table_asset_regression_summary.json"
    summary_md = out_dir / "table_asset_regression_summary.md"
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    summary_md.write_text(_render_md(summary), encoding="utf-8")

    print(f"[table-gate] tests: {summary['tests_run']}")
    print(f"[table-gate] failures: {summary['failures']}, errors: {summary['errors']}, skipped: {summary['skipped']}")
    print(f"[table-gate] summary json: {summary_json}")
    print(f"[table-gate] summary md: {summary_md}")

    exit_code = _gate_exit_code(summary)
    if exit_code:
        raise SystemExit(exit_code)


def _gate_exit_code(summary: dict) -> int:
    if int(summary.get("failures", 0)) or int(summary.get("errors", 0)):
        return 1
    if int(summary.get("skipped", 0)):
        return 2
    return 0


def _render_md(summary: dict) -> str:
    lines = [
        "# Table Asset Regression Summary",
        "",
        f"- generated_at: `{summary.get('generated_at', '')}`",
        f"- tests_run: `{summary.get('tests_run', 0)}`",
        f"- failures: `{summary.get('failures', 0)}`",
        f"- errors: `{summary.get('errors', 0)}`",
        f"- skipped: `{summary.get('skipped', 0)}`",
        f"- overall_pass: `{summary.get('overall_pass', False)}`",
        "",
    ]
    if summary.get("failure_tests"):
        lines.append("## Failure Tests")
        lines.extend(f"- `{item}`" for item in summary["failure_tests"])
        lines.append("")
    if summary.get("error_tests"):
        lines.append("## Error Tests")
        lines.extend(f"- `{item}`" for item in summary["error_tests"])
        lines.append("")
    if summary.get("skipped_tests"):
        lines.append("## Skipped Tests")
        lines.extend(f"- `{item}`" for item in summary["skipped_tests"][:20])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


if __name__ == "__main__":
    main()
