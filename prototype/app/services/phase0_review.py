from __future__ import annotations

import csv
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOTYPE_DIR = Path(__file__).resolve().parents[2]
DEFAULT_PHASE0_DIR = PROTOTYPE_DIR / "eval" / "phase0"
REPORT_SCRIPT = PROTOTYPE_DIR / "scripts" / "phase0_baseline_report.py"


@dataclass(frozen=True)
class Phase0Paths:
    phase0_dir: Path
    page_csv: Path
    item_csv: Path
    priority_csv: Path
    report_md: Path

ITEM_UPDATABLE_FIELDS = [
    "is_true_requirement",
    "is_split_correct",
    "is_type_correct",
    "gold_requirement_text",
    "gold_requirement_type",
    "error_tags",
    "annotator",
    "notes",
]
PAGE_UPDATABLE_FIELDS = [
    "is_page_readable",
    "gold_total_requirements",
    "gold_noise_lines_pct",
    "annotator",
    "notes",
]


def load_review_dataset(
    top_pages: int = 30, include_done: bool = False, phase0_dir: str | Path | None = None
) -> dict[str, Any]:
    paths = _resolve_phase0_paths(phase0_dir)
    _ensure_phase0_exists(paths)
    page_rows = _read_csv(paths.page_csv)
    item_rows = _read_csv(paths.item_csv)
    priority_rows = _read_csv(paths.priority_csv) if paths.priority_csv.exists() else []

    predicted_count_by_sample: dict[str, int] = {}
    for row in item_rows:
        if row.get("source_type", "") != "predicted":
            continue
        sample_id = row.get("sample_id", "")
        if not sample_id:
            continue
        predicted_count_by_sample[sample_id] = predicted_count_by_sample.get(sample_id, 0) + 1

    selected_sample_ids: list[str] = []
    if priority_rows:
        for row in priority_rows:
            sample_id = row.get("sample_id", "")
            if not sample_id:
                continue
            if predicted_count_by_sample.get(sample_id, 0) <= 0:
                continue
            selected_sample_ids.append(sample_id)
            if len(selected_sample_ids) >= max(1, top_pages):
                break
    if not selected_sample_ids:
        for row in page_rows:
            sample_id = row.get("sample_id", "")
            if predicted_count_by_sample.get(sample_id, 0) <= 0:
                continue
            selected_sample_ids.append(sample_id)
            if len(selected_sample_ids) >= max(1, top_pages):
                break

    page_map = {row["sample_id"]: row for row in page_rows}
    items_by_sample: dict[str, list[dict[str, str]]] = {sample_id: [] for sample_id in selected_sample_ids}
    total_selected_items = 0
    labeled_items = 0

    for row in item_rows:
        sample_id = row.get("sample_id", "")
        if sample_id not in items_by_sample:
            continue
        done = _is_done_item(row)
        total_selected_items += 1
        if done:
            labeled_items += 1
        if include_done or not done:
            items_by_sample[sample_id].append(row)

    pages_out: list[dict[str, Any]] = []
    for sample_id in selected_sample_ids:
        page_row = page_map.get(sample_id)
        if not page_row:
            continue
        items = items_by_sample.get(sample_id, [])
        if not include_done and not items:
            continue
        pages_out.append(
            {
                "sample_id": sample_id,
                "file_name": page_row.get("file_name", ""),
                "page_no": _to_int(page_row.get("page_no")),
                "priority_score": _priority_score(sample_id, priority_rows),
                "cleaned_text_preview": page_row.get("cleaned_text_preview", ""),
                "page_label": {
                    "is_page_readable": page_row.get("is_page_readable", ""),
                    "gold_total_requirements": page_row.get("gold_total_requirements", ""),
                    "gold_noise_lines_pct": page_row.get("gold_noise_lines_pct", ""),
                },
                "items": [_shape_item(item) for item in items],
            }
        )

    return {
        "meta": {
            "phase0_dir": str(paths.phase0_dir),
            "top_pages": top_pages,
            "include_done": include_done,
            "selected_pages": len(pages_out),
            "total_selected_items": total_selected_items,
            "labeled_selected_items": labeled_items,
            "remaining_selected_items": max(0, total_selected_items - labeled_items),
            "progress_pct": round((labeled_items / total_selected_items) * 100, 2)
            if total_selected_items
            else 0.0,
        },
        "pages": pages_out,
    }


def update_item_labels(updates: list[dict[str, Any]], phase0_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _resolve_phase0_paths(phase0_dir)
    _ensure_phase0_exists(paths)
    rows = _read_csv(paths.item_csv)
    row_map = {row.get("item_id", ""): row for row in rows}
    changed = 0
    missing = 0
    for update in updates:
        item_id = str(update.get("item_id", "")).strip()
        if not item_id or item_id not in row_map:
            missing += 1
            continue
        row = row_map[item_id]
        touched = False
        for field in ITEM_UPDATABLE_FIELDS:
            if field not in update:
                continue
            value = "" if update[field] is None else str(update[field]).strip()
            if row.get(field, "") != value:
                row[field] = value
                touched = True
        if touched:
            changed += 1
    _write_csv(paths.item_csv, rows)
    return {"changed": changed, "missing": missing}


def update_page_labels(updates: list[dict[str, Any]], phase0_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _resolve_phase0_paths(phase0_dir)
    _ensure_phase0_exists(paths)
    rows = _read_csv(paths.page_csv)
    row_map = {row.get("sample_id", ""): row for row in rows}
    changed = 0
    missing = 0
    for update in updates:
        sample_id = str(update.get("sample_id", "")).strip()
        if not sample_id or sample_id not in row_map:
            missing += 1
            continue
        row = row_map[sample_id]
        touched = False
        for field in PAGE_UPDATABLE_FIELDS:
            if field not in update:
                continue
            value = "" if update[field] is None else str(update[field]).strip()
            if row.get(field, "") != value:
                row[field] = value
                touched = True
        if touched:
            changed += 1
    _write_csv(paths.page_csv, rows)
    return {"changed": changed, "missing": missing}


def refresh_baseline_report(phase0_dir: str | Path | None = None) -> dict[str, Any]:
    paths = _resolve_phase0_paths(phase0_dir)
    _ensure_phase0_exists(paths)
    if not REPORT_SCRIPT.exists():
        raise FileNotFoundError(f"Missing script: {REPORT_SCRIPT}")

    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(PROTOTYPE_DIR) if not current_pythonpath else f"{str(PROTOTYPE_DIR)}:{current_pythonpath}"
    )

    proc = subprocess.run(
        [sys.executable, str(REPORT_SCRIPT), "--phase0-dir", str(paths.phase0_dir)],
        cwd=str(PROTOTYPE_DIR),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    return {
        "report_path": str(paths.report_md),
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def _shape_item(row: dict[str, str]) -> dict[str, Any]:
    return {
        "item_id": row.get("item_id", ""),
        "source_type": row.get("source_type", ""),
        "auto_requirement_text": row.get("auto_requirement_text", ""),
        "auto_requirement_type": row.get("auto_requirement_type", ""),
        "auto_priority": row.get("auto_priority", ""),
        "is_true_requirement": row.get("is_true_requirement", ""),
        "is_split_correct": row.get("is_split_correct", ""),
        "is_type_correct": row.get("is_type_correct", ""),
        "gold_requirement_text": row.get("gold_requirement_text", ""),
        "gold_requirement_type": row.get("gold_requirement_type", ""),
        "error_tags": row.get("error_tags", ""),
        "annotator": row.get("annotator", ""),
        "notes": row.get("notes", ""),
        "is_done": _is_done_item(row),
    }


def _is_done_item(row: dict[str, str]) -> bool:
    return _is_binary(row.get("is_true_requirement", "")) and _is_binary(
        row.get("is_split_correct", "")
    ) and _is_binary(row.get("is_type_correct", ""))


def _is_binary(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text in {"0", "1", "true", "false", "y", "n", "yes", "no"}


def _priority_score(sample_id: str, priority_rows: list[dict[str, str]]) -> float:
    for row in priority_rows:
        if row.get("sample_id", "") == sample_id:
            return _to_float(row.get("priority_score"))
    return 0.0


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


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _resolve_phase0_paths(phase0_dir: str | Path | None) -> Phase0Paths:
    if phase0_dir is None or not str(phase0_dir).strip():
        resolved_dir = DEFAULT_PHASE0_DIR.resolve()
    else:
        raw_dir = Path(str(phase0_dir).strip()).expanduser()
        resolved_dir = (raw_dir if raw_dir.is_absolute() else PROTOTYPE_DIR / raw_dir).resolve()

    try:
        resolved_dir.relative_to(PROTOTYPE_DIR)
    except ValueError as exc:
        raise FileNotFoundError("phase0_dir must be inside prototype directory.") from exc

    return Phase0Paths(
        phase0_dir=resolved_dir,
        page_csv=resolved_dir / "phase0_page_annotation_template.csv",
        item_csv=resolved_dir / "phase0_item_annotation_template.csv",
        priority_csv=resolved_dir / "phase0_labeling_priority.csv",
        report_md=resolved_dir / "phase0_baseline_report.md",
    )


def _ensure_phase0_exists(paths: Phase0Paths) -> None:
    if not paths.page_csv.exists() or not paths.item_csv.exists():
        raise FileNotFoundError(
            "Phase-0 annotation files are missing. Run scripts/phase0_prepare.py first."
        )
