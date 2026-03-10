from __future__ import annotations

import csv
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
PROTOTYPE_DIR = APP_DIR.parent
ITERATIONS_DIR = PROTOTYPE_DIR / "eval" / "iterations"
BOARD_CSV = ITERATIONS_DIR / "phase0_iteration_board.csv"
REGRESSION_DIR = PROTOTYPE_DIR / "eval" / "regression_suite"
PROGRESS_DIR = PROTOTYPE_DIR / "eval" / "progress"
TRACKER_PATH = PROGRESS_DIR / "progress_tracker.json"

ALLOWED_PLAN_STATUS = {"TODO", "IN_PROGRESS", "BLOCKED", "DONE"}
ALLOWED_EVENT_STATUS = {"DONE", "INFO", "RISK", "BLOCKED"}
ALLOWED_EVENT_CATEGORY = {"manual", "iteration", "quality", "review", "infra", "note"}


def load_progress_snapshot(limit_rounds: int = 60, limit_events: int = 200) -> dict[str, Any]:
    tracker = _load_tracker()
    iterations = _load_iteration_board(limit_rounds=limit_rounds)
    regression_runs = _load_regression_runs(limit_runs=limit_rounds)
    plan_items = tracker.get("plan", [])
    manual_events = tracker.get("events", [])
    latest_timestamp = max(
        [
            tracker.get("updated_at", ""),
            iterations[-1]["timestamp"] if iterations else "",
            regression_runs[-1]["timestamp"] if regression_runs else "",
            manual_events[0]["time"] if manual_events else "",
        ]
    )
    regression_latest = regression_runs[-1] if regression_runs else {}
    return {
        "generated_at": _now_iso(),
        "latest_activity_at": latest_timestamp,
        "tracker": {
            "project_name": tracker.get("project_name", ""),
            "north_star": tracker.get("north_star", ""),
            "updated_at": tracker.get("updated_at", ""),
        },
        "kpis": _build_kpis(iterations, plan_items, regression_runs),
        "plan": plan_items,
        "timeline": _build_timeline(
            iterations,
            manual_events,
            regression_runs,
            limit_events=limit_events,
        ),
        "charts": {
            "split_trend": iterations,
            "volume_trend": iterations,
            "regression_trend": regression_runs,
        },
        "regression": {
            "latest": regression_latest,
            "runs": regression_runs,
            "total_runs": len(regression_runs),
            "pass_runs": len([row for row in regression_runs if bool(row.get("overall_pass"))]),
        },
        "next_actions": _build_next_actions(plan_items),
    }


def add_progress_event(
    *,
    title: str,
    detail: str = "",
    category: str = "manual",
    status: str = "DONE",
    event_time: str | None = None,
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Event title cannot be empty.")
    tracker = _load_tracker()
    normalized_category = category if category in ALLOWED_EVENT_CATEGORY else "manual"
    normalized_status = status if status in ALLOWED_EVENT_STATUS else "INFO"
    event = {
        "id": f"evt-{uuid.uuid4().hex[:10]}",
        "time": _normalize_time(event_time) if event_time else _now_iso(),
        "category": normalized_category,
        "status": normalized_status,
        "title": clean_title,
        "detail": detail.strip(),
        "source": "manual",
    }
    events = list(tracker.get("events", []))
    events.append(event)
    events = _sort_events_desc(events)[:800]
    tracker["events"] = events
    tracker["updated_at"] = _now_iso()
    _save_tracker(tracker)
    return event


def replace_plan_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    tracker = _load_tracker()
    normalized = _normalize_plan_items(items)
    tracker["plan"] = normalized
    tracker["updated_at"] = _now_iso()
    _save_tracker(tracker)
    return {"count": len(normalized), "updated_at": tracker["updated_at"], "items": normalized}


def _load_tracker() -> dict[str, Any]:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    if not TRACKER_PATH.exists():
        tracker = _default_tracker()
        _save_tracker(tracker)
        return tracker
    try:
        tracker = json.loads(TRACKER_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        tracker = _default_tracker()
        _save_tracker(tracker)
        return tracker
    return _sanitize_tracker(tracker)


def _save_tracker(payload: dict[str, Any]) -> None:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    TRACKER_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _sanitize_tracker(payload: dict[str, Any]) -> dict[str, Any]:
    project_name = str(payload.get("project_name") or "SOR Requirement Extraction Program")
    north_star = str(
        payload.get("north_star")
        or "Stabilize section-level extraction with split accuracy above 90% on text PDFs."
    )
    raw_plan = payload.get("plan", [])
    safe_plan = _normalize_plan_items(raw_plan) if isinstance(raw_plan, list) else []
    raw_events = payload.get("events", [])
    safe_events: list[dict[str, Any]] = []
    if isinstance(raw_events, list):
        for event in raw_events:
            if not isinstance(event, dict):
                continue
            title = str(event.get("title") or "").strip()
            if not title:
                continue
            safe_events.append(
                {
                    "id": str(event.get("id") or f"evt-{uuid.uuid4().hex[:10]}"),
                    "time": _normalize_time(str(event.get("time") or _now_iso())),
                    "category": str(event.get("category") or "manual")
                    if str(event.get("category") or "manual") in ALLOWED_EVENT_CATEGORY
                    else "manual",
                    "status": str(event.get("status") or "INFO")
                    if str(event.get("status") or "INFO") in ALLOWED_EVENT_STATUS
                    else "INFO",
                    "title": title,
                    "detail": str(event.get("detail") or "").strip(),
                    "source": "manual",
                }
            )
    tracker = {
        "project_name": project_name,
        "north_star": north_star,
        "updated_at": _normalize_time(str(payload.get("updated_at") or _now_iso())),
        "plan": safe_plan,
        "events": _sort_events_desc(safe_events),
    }
    return tracker


def _default_tracker() -> dict[str, Any]:
    now = _now_iso()
    return {
        "project_name": "SOR Requirement Extraction Program",
        "north_star": "Stabilize section-level extraction with split accuracy above 90% on text PDFs.",
        "updated_at": now,
        "plan": [
            {
                "id": "plan-parse-noise",
                "title": "Harden parse layer for header/footer/watermark noise removal",
                "owner": "AI + Engineer",
                "status": "IN_PROGRESS",
                "progress_pct": 85,
                "target_date": "",
                "notes": "Track mixed watermark fragments and repeated metadata lines.",
            },
            {
                "id": "plan-split-hierarchy",
                "title": "Keep chapter hierarchy extraction stable at section/sub-section level",
                "owner": "AI + Engineer",
                "status": "IN_PROGRESS",
                "progress_pct": 80,
                "target_date": "",
                "notes": "Prevent false sub-sections triggered by table numeric values.",
            },
            {
                "id": "plan-review-loop",
                "title": "Close labeling-review loop with measurable gate per round",
                "owner": "Engineer",
                "status": "TODO",
                "progress_pct": 35,
                "target_date": "",
                "notes": "Use phase0 UI labels to prioritize split errors.",
            },
        ],
        "events": [
            {
                "id": "evt-bootstrap",
                "time": now,
                "category": "note",
                "status": "INFO",
                "title": "Progress tracker initialized",
                "detail": "Auto timeline from phase0 iteration board is enabled.",
                "source": "manual",
            }
        ],
    }


def _load_iteration_board(limit_rounds: int = 60) -> list[dict[str, Any]]:
    if not BOARD_CSV.exists():
        return []
    rows: list[dict[str, Any]] = []
    with BOARD_CSV.open("r", newline="", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            round_id = str(row.get("round_id") or "").strip()
            if not round_id:
                continue
            timestamp = str(row.get("timestamp") or "").strip()
            out_dir = str(row.get("out_dir") or "").strip()
            quality = _read_quality_summary(round_id=round_id, out_dir=out_dir)
            rows.append(
                {
                    "round_id": round_id,
                    "timestamp": timestamp,
                    "out_dir": out_dir,
                    "auto_predicted_items": _coerce_int(row.get("auto_predicted_items"), default=0),
                    "parse_coverage_pct": _coerce_float(row.get("parse_coverage_pct"), default=0.0),
                    "pages_with_items": str(row.get("pages_with_items") or "").strip(),
                    "pages_with_items_pct": _parse_ratio_pct(str(row.get("pages_with_items") or "").strip()),
                    "split_accuracy_pct": _coerce_float(row.get("split_accuracy_pct"), default=0.0),
                    "type_accuracy_pct": _coerce_float(row.get("type_accuracy_pct"), default=0.0),
                    "labeled_predicted_items": _coerce_int(row.get("labeled_predicted_items"), default=0),
                    "quality_gate_score_pct": _coerce_float(quality.get("gate_score_pct"), default=0.0),
                    "quality_checks_passed": _coerce_int(quality.get("checks_passed"), default=0),
                    "quality_checks_total": _coerce_int(quality.get("checks_total"), default=0),
                    "quality_docs": _coerce_int(quality.get("docs"), default=0),
                }
            )
    rows.sort(key=lambda item: _sort_key(item.get("timestamp", "")))
    if limit_rounds > 0:
        rows = rows[-limit_rounds:]
    return rows


def _load_regression_runs(limit_runs: int = 60) -> list[dict[str, Any]]:
    if not REGRESSION_DIR.exists():
        return []
    rows: list[dict[str, Any]] = []
    for run_dir in sorted(REGRESSION_DIR.iterdir()):
        if not run_dir.is_dir():
            continue
        summary_path = run_dir / "regression_suite_summary.json"
        if not summary_path.exists():
            continue
        try:
            payload = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        quality = payload.get("quality_gate", {}) if isinstance(payload.get("quality_gate"), dict) else {}
        fidelity = payload.get("fidelity_gate", {}) if isinstance(payload.get("fidelity_gate"), dict) else {}
        quality_checks_passed = _coerce_int(quality.get("checks_passed"), default=0)
        quality_checks_total = _coerce_int(quality.get("checks_total"), default=0)
        quality_high_severity_issues = _coerce_int(quality.get("high_severity_issues"), default=0)
        if "gate_pass" in quality:
            quality_pass = bool(quality.get("gate_pass"))
        else:
            quality_pass = (
                quality_checks_total > 0
                and quality_checks_passed == quality_checks_total
                and quality_high_severity_issues <= 0
            )
        fidelity_checks_passed = _coerce_int(fidelity.get("checks_passed"), default=0)
        fidelity_checks_total = _coerce_int(fidelity.get("checks_total"), default=0)
        if "overall_pass" in fidelity:
            fidelity_pass = bool(fidelity.get("overall_pass"))
        else:
            fidelity_pass = fidelity_checks_total > 0 and fidelity_checks_passed == fidelity_checks_total
        timestamp = str(payload.get("timestamp") or "").strip() or datetime.fromtimestamp(
            summary_path.stat().st_mtime
        ).isoformat(timespec="seconds")
        rows.append(
            {
                "run_id": str(payload.get("run_id") or run_dir.name),
                "timestamp": timestamp,
                "quality_gate_score_pct": _coerce_float(quality.get("gate_score_pct"), default=0.0),
                "quality_checks_passed": quality_checks_passed,
                "quality_checks_total": quality_checks_total,
                "quality_high_severity_issues": quality_high_severity_issues,
                "quality_gate_pass": quality_pass,
                "fidelity_score_pct": _coerce_float(fidelity.get("score_pct"), default=0.0),
                "fidelity_checks_passed": fidelity_checks_passed,
                "fidelity_checks_total": fidelity_checks_total,
                "fidelity_target_docs": _coerce_int(fidelity.get("target_docs"), default=0),
                "fidelity_failed_docs": _coerce_int(fidelity.get("failed_docs"), default=0),
                "fidelity_overall_pass": fidelity_pass,
                "overall_pass": bool(quality_pass and fidelity_pass),
                "summary_json": str(summary_path),
            }
        )
    rows.sort(key=lambda item: _sort_key(item.get("timestamp", "")))
    if limit_runs > 0:
        rows = rows[-limit_runs:]
    return rows


def _read_quality_summary(round_id: str, out_dir: str) -> dict[str, Any]:
    candidates: list[Path] = []
    if out_dir:
        candidates.append(Path(out_dir) / "quality_gate" / "structural_quality_summary.json")
    candidates.append(ITERATIONS_DIR / round_id / "quality_gate" / "structural_quality_summary.json")
    for path in candidates:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
    return {}


def _build_kpis(
    iterations: list[dict[str, Any]],
    plan_items: list[dict[str, Any]],
    regression_runs: list[dict[str, Any]],
) -> dict[str, Any]:
    latest_regression = regression_runs[-1] if regression_runs else {}
    regression_pass_runs = len([row for row in regression_runs if bool(row.get("overall_pass"))])
    regression_total_runs = len(regression_runs)
    regression_pass_rate = (
        round((regression_pass_runs / regression_total_runs) * 100.0, 2)
        if regression_total_runs > 0
        else 0.0
    )
    total_rounds = len(iterations)
    if not total_rounds:
        return {
            "total_rounds": 0,
            "latest_round_id": "",
            "latest_split_accuracy_pct": 0.0,
            "best_split_accuracy_pct": 0.0,
            "split_improvement_pct": 0.0,
            "latest_parse_coverage_pct": 0.0,
            "latest_quality_gate_score_pct": 0.0,
            "text_only_rounds": 0,
            "regression_total_runs": regression_total_runs,
            "regression_pass_runs": regression_pass_runs,
            "regression_pass_rate_pct": regression_pass_rate,
            "latest_regression_run_id": str(latest_regression.get("run_id") or ""),
            "latest_regression_quality_gate_score_pct": _coerce_float(
                latest_regression.get("quality_gate_score_pct"),
                default=0.0,
            ),
            "latest_regression_fidelity_score_pct": _coerce_float(
                latest_regression.get("fidelity_score_pct"),
                default=0.0,
            ),
            "latest_regression_overall_pass": bool(latest_regression.get("overall_pass", False)),
            "plan_total": len(plan_items),
            "plan_done": len([item for item in plan_items if item.get("status") == "DONE"]),
            "plan_in_progress": len([item for item in plan_items if item.get("status") == "IN_PROGRESS"]),
            "plan_overall_progress_pct": _average_progress(plan_items),
            "overall_progress_index_pct": 0.0,
        }

    latest = iterations[-1]
    first = iterations[0]
    best = max(iterations, key=lambda item: item.get("split_accuracy_pct", 0.0))
    split_improvement = round(
        _coerce_float(latest.get("split_accuracy_pct"), default=0.0)
        - _coerce_float(first.get("split_accuracy_pct"), default=0.0),
        2,
    )
    text_only_rounds = len([item for item in iterations if "text_only" in str(item.get("round_id", ""))])
    plan_progress = _average_progress(plan_items)
    split_progress = min(100.0, max(0.0, _coerce_float(latest.get("split_accuracy_pct"), default=0.0)))
    parse_progress = min(100.0, max(0.0, _coerce_float(latest.get("parse_coverage_pct"), default=0.0)))
    overall_index = round(split_progress * 0.6 + parse_progress * 0.2 + plan_progress * 0.2, 2)
    return {
        "total_rounds": total_rounds,
        "latest_round_id": latest.get("round_id", ""),
        "latest_split_accuracy_pct": _coerce_float(latest.get("split_accuracy_pct"), default=0.0),
        "best_split_accuracy_pct": _coerce_float(best.get("split_accuracy_pct"), default=0.0),
        "best_split_round_id": best.get("round_id", ""),
        "split_improvement_pct": split_improvement,
        "latest_parse_coverage_pct": _coerce_float(latest.get("parse_coverage_pct"), default=0.0),
        "latest_quality_gate_score_pct": _coerce_float(latest.get("quality_gate_score_pct"), default=0.0),
        "text_only_rounds": text_only_rounds,
        "regression_total_runs": regression_total_runs,
        "regression_pass_runs": regression_pass_runs,
        "regression_pass_rate_pct": regression_pass_rate,
        "latest_regression_run_id": str(latest_regression.get("run_id") or ""),
        "latest_regression_quality_gate_score_pct": _coerce_float(
            latest_regression.get("quality_gate_score_pct"),
            default=0.0,
        ),
        "latest_regression_fidelity_score_pct": _coerce_float(
            latest_regression.get("fidelity_score_pct"),
            default=0.0,
        ),
        "latest_regression_overall_pass": bool(latest_regression.get("overall_pass", False)),
        "plan_total": len(plan_items),
        "plan_done": len([item for item in plan_items if item.get("status") == "DONE"]),
        "plan_in_progress": len([item for item in plan_items if item.get("status") == "IN_PROGRESS"]),
        "plan_overall_progress_pct": plan_progress,
        "overall_progress_index_pct": overall_index,
    }


def _build_timeline(
    iterations: list[dict[str, Any]],
    manual_events: list[dict[str, Any]],
    regression_runs: list[dict[str, Any]],
    *,
    limit_events: int,
) -> list[dict[str, Any]]:
    auto_events: list[dict[str, Any]] = []
    for row in iterations:
        auto_events.append(
            {
                "id": f"auto-{row.get('round_id', '')}",
                "time": row.get("timestamp", ""),
                "category": "iteration",
                "status": "DONE",
                "title": f"{row.get('round_id', '')} completed",
                "detail": (
                    f"split={row.get('split_accuracy_pct', 0.0)}%, "
                    f"parse={row.get('parse_coverage_pct', 0.0)}%, "
                    f"pages={row.get('pages_with_items', '')}"
                ),
                "source": "auto",
            }
        )
    regression_events: list[dict[str, Any]] = []
    for row in regression_runs:
        status = "DONE" if bool(row.get("overall_pass")) else "RISK"
        regression_events.append(
            {
                "id": f"auto-reg-{row.get('run_id', '')}",
                "time": row.get("timestamp", ""),
                "category": "quality",
                "status": status,
                "title": f"{row.get('run_id', '')} regression",
                "detail": (
                    f"quality={row.get('quality_gate_score_pct', 0.0)}%, "
                    f"fidelity={row.get('fidelity_score_pct', 0.0)}%, "
                    f"pass={bool(row.get('overall_pass'))}"
                ),
                "source": "auto",
            }
        )
    merged = auto_events + regression_events + list(manual_events)
    merged = _sort_events_desc(merged)
    if limit_events > 0:
        merged = merged[:limit_events]
    return merged


def _build_next_actions(plan_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    next_actions = [
        {
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "owner": item.get("owner", ""),
            "status": item.get("status", "TODO"),
            "progress_pct": _coerce_int(item.get("progress_pct"), default=0, lower=0, upper=100),
            "target_date": item.get("target_date", ""),
            "notes": item.get("notes", ""),
        }
        for item in plan_items
        if item.get("status") in {"TODO", "IN_PROGRESS", "BLOCKED"}
    ]
    next_actions.sort(key=lambda item: (item.get("status") != "IN_PROGRESS", -item.get("progress_pct", 0)))
    return next_actions[:8]


def _sort_events_desc(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        events,
        key=lambda item: _sort_key(str(item.get("time", ""))),
        reverse=True,
    )


def _normalize_plan_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("title", "")).strip()
        if not title:
            continue
        plan_id = str(raw.get("id") or f"plan-{idx}")
        status = str(raw.get("status") or "TODO").strip().upper()
        if status not in ALLOWED_PLAN_STATUS:
            status = "TODO"
        progress_pct = _coerce_int(raw.get("progress_pct"), default=0, lower=0, upper=100)
        owner = str(raw.get("owner") or "").strip()
        target_date = str(raw.get("target_date") or "").strip()
        notes = str(raw.get("notes") or "").strip()
        normalized.append(
            {
                "id": plan_id,
                "title": title,
                "owner": owner,
                "status": status,
                "progress_pct": progress_pct,
                "target_date": target_date,
                "notes": notes,
            }
        )
    return normalized


def _sort_key(value: str) -> tuple[int, str]:
    stamp = _parse_datetime(value)
    if stamp is None:
        return (0, value)
    return (1, stamp.isoformat())


def _parse_datetime(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    candidates = [value]
    if value.endswith("Z"):
        candidates.append(value[:-1] + "+00:00")
    for candidate in candidates:
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _normalize_time(raw: str) -> str:
    parsed = _parse_datetime(raw)
    if parsed is None:
        return raw.strip() or _now_iso()
    return parsed.isoformat(timespec="seconds")


def _coerce_int(value: Any, *, default: int, lower: int | None = None, upper: int | None = None) -> int:
    try:
        number = int(float(str(value).strip()))
    except (TypeError, ValueError):
        number = default
    if lower is not None:
        number = max(lower, number)
    if upper is not None:
        number = min(upper, number)
    return number


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return round(float(str(value).strip()), 2)
    except (TypeError, ValueError):
        return default


def _parse_ratio_pct(text: str) -> float:
    value = text.strip()
    if not value:
        return 0.0
    match = re.match(r"^\s*(\d+)\s*/\s*(\d+)\s*$", value)
    if not match:
        return 0.0
    numerator = int(match.group(1))
    denominator = int(match.group(2))
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100.0, 2)


def _average_progress(plan_items: list[dict[str, Any]]) -> float:
    if not plan_items:
        return 0.0
    values = [
        _coerce_int(item.get("progress_pct"), default=0, lower=0, upper=100)
        for item in plan_items
    ]
    return round(sum(values) / len(values), 2)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
