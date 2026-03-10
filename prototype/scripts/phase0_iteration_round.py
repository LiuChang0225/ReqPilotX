from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


METRIC_PATTERNS = {
    "auto_predicted_items": re.compile(r"Auto predicted items:\s*`?(\d+)`?"),
    "parse_coverage_pct": re.compile(r"Parse coverage.*:\s*`?([0-9.]+)%`?"),
    "pages_with_items": re.compile(r"Pages with extracted items:\s*`?(\d+/\d+)`?"),
    "split_accuracy_pct": re.compile(r"Split accuracy:\s*`?([0-9.]+)%`?"),
    "type_accuracy_pct": re.compile(r"Type accuracy:\s*`?([0-9.]+)%`?"),
    "labeled_predicted_items": re.compile(r"Labeled predicted items:\s*`?(\d+)`?"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one Phase0 optimization iteration round.")
    parser.add_argument("pdf_dir", type=Path, help="Directory with source PDFs.")
    parser.add_argument("--round-id", type=str, required=True, help="Round id, e.g. r01.")
    parser.add_argument("--base-label-dir", type=Path, default=Path("eval/phase0"), help="Base labeled dir.")
    parser.add_argument("--out-root", type=Path, default=Path("eval/iterations"), help="Iteration output root.")
    parser.add_argument("--target-pages", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--rebase-min-score", type=float, default=35.0)
    parser.add_argument("--split-transfer-min-sim", type=float, default=0.92)
    parser.add_argument("--allow-span-change-for-split", action="store_true")
    parser.add_argument("--run-structural-gate", action="store_true")
    parser.add_argument(
        "--gate-profile-config",
        type=Path,
        default=Path("eval/quality_gate_profiles.json"),
        help="Profile JSON for structural quality gate.",
    )
    parser.add_argument("--build-split-queue", action="store_true")
    parser.add_argument("--split-queue-size", type=int, default=80)
    parser.add_argument("--split-target-labeled", type=int, default=60)
    parser.add_argument(
        "--exclude-name-regex",
        action="append",
        default=[],
        help="Exclude PDFs whose file name matches this regex. Can be used multiple times.",
    )
    args = parser.parse_args()

    project_dir = Path(__file__).resolve().parents[1]
    pdf_dir = args.pdf_dir.expanduser().resolve()
    out_root = (project_dir / args.out_root).resolve()
    out_dir = (out_root / args.round_id).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    prepare_cmd = [
        sys.executable,
        str(project_dir / "scripts" / "phase0_prepare.py"),
        str(pdf_dir),
        "--target-pages",
        str(args.target_pages),
        "--seed",
        str(args.seed),
        "--out-dir",
        str(out_dir),
    ]
    for pattern in args.exclude_name_regex:
        prepare_cmd.extend(["--exclude-name-regex", pattern])
    _run(prepare_cmd, cwd=project_dir)

    base_item_csv = (project_dir / args.base_label_dir / "phase0_item_annotation_template.csv").resolve()
    if base_item_csv.exists():
        rebase_cmd = [
            sys.executable,
            str(project_dir / "scripts" / "phase0_rebase_labels.py"),
            "--old-item-csv",
            str(base_item_csv),
            "--new-item-csv",
            str(out_dir / "phase0_item_annotation_template.csv"),
            "--focus-csv",
            str(out_dir / "phase0_relabel_focus.csv"),
            "--split-recheck-csv",
            str(out_dir / "phase0_split_recheck.csv"),
            "--min-score",
            str(args.rebase_min_score),
            "--split-transfer-min-sim",
            str(args.split_transfer_min_sim),
        ]
        if args.allow_span_change_for_split:
            rebase_cmd.append("--allow-span-change-for-split")
        _run(rebase_cmd, cwd=project_dir)

    _run(
        [
            sys.executable,
            str(project_dir / "scripts" / "phase0_baseline_report.py"),
            "--phase0-dir",
            str(out_dir),
        ],
        cwd=project_dir,
    )

    if args.run_structural_gate:
        gate_cmd = [
            sys.executable,
            str(project_dir / "scripts" / "structural_quality_gate.py"),
            str(pdf_dir),
            "--phase0-dir",
            str(out_dir),
            "--profile-config",
            str(project_dir / args.gate_profile_config),
            "--out-dir",
            str(out_dir / "quality_gate"),
        ]
        for pattern in args.exclude_name_regex:
            gate_cmd.extend(["--exclude-name-regex", pattern])
        _run(gate_cmd, cwd=project_dir)

    if args.build_split_queue:
        queue_cmd = [
            sys.executable,
            str(project_dir / "scripts" / "phase0_split_active_queue.py"),
            "--phase0-dir",
            str(out_dir),
            "--queue-size",
            str(args.split_queue_size),
            "--target-labeled",
            str(args.split_target_labeled),
        ]
        if args.run_structural_gate:
            queue_cmd.extend(["--quality-gate-dir", str(out_dir / "quality_gate")])
        _run(queue_cmd, cwd=project_dir)

    report_path = out_dir / "phase0_baseline_report.md"
    metrics = _parse_metrics(report_path)
    metrics_row = {
        "round_id": args.round_id,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "out_dir": str(out_dir),
        **metrics,
    }
    board_csv = out_root / "phase0_iteration_board.csv"
    _append_csv(board_csv, metrics_row)

    print("[round] completed")
    for key, value in metrics_row.items():
        print(f"[round] {key}: {value}")
    print(f"[round] board: {board_csv}")


def _run(cmd: list[str], cwd: Path) -> None:
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(cwd) if not current_pythonpath else f"{str(cwd)}:{current_pythonpath}"
    subprocess.run(cmd, cwd=str(cwd), check=True, env=env)


def _parse_metrics(report_path: Path) -> dict[str, Any]:
    text = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
    values: dict[str, Any] = {}
    for key, pattern in METRIC_PATTERNS.items():
        match = pattern.search(text)
        values[key] = match.group(1) if match else ""
    return values


def _append_csv(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(row.keys()))
        if not exists:
            writer.writeheader()
        writer.writerow(row)


if __name__ == "__main__":
    main()
