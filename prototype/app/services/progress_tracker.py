from __future__ import annotations

import csv
import fnmatch
import json
import os
import re
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
PROTOTYPE_DIR = APP_DIR.parent
REPO_ROOT = PROTOTYPE_DIR.parent
ITERATIONS_DIR = PROTOTYPE_DIR / "eval" / "iterations"
BOARD_CSV = ITERATIONS_DIR / "phase0_iteration_board.csv"
REGRESSION_DIR = PROTOTYPE_DIR / "eval" / "regression_suite"
PROGRESS_DIR = PROTOTYPE_DIR / "eval" / "progress"
TRACKER_BOOTSTRAP_PATH = PROGRESS_DIR / "progress_tracker.json"
TRACK_REGISTRY_PATH = PROGRESS_DIR / "track_registry.json"
RUNTIME_DIR_ENV = "REQPILOT_PROGRESS_RUNTIME_DIR"
RUNTIME_DIR_NAME = "reqpilotx-progress"
TRACKER_FILE_NAME = "progress_tracker.json"

ALLOWED_PLAN_STATUS = {"TODO", "IN_PROGRESS", "BLOCKED", "DONE"}
ALLOWED_EVENT_STATUS = {"DONE", "INFO", "RISK", "BLOCKED"}
ALLOWED_EVENT_CATEGORY = {"manual", "iteration", "quality", "review", "infra", "note"}
ALLOWED_EVENT_TYPE = {
    "manual",
    "kickoff",
    "checkpoint",
    "risk",
    "gate_pass",
    "gate_fail",
    "handoff",
    "done",
}
ALLOWED_DRIFT_STATUS = {"ON_PLAN", "RISK", "OFF_PLAN"}
ALLOWED_GATE_STATUS = {"PENDING", "PASS", "RISK", "FAIL", "NOT_APPLICABLE"}
TRACK_STATUS_ACTIVE = {"ACTIVE", "IN_PROGRESS"}
TRACK_STATUS_QUEUED = {"QUEUED", "PLANNED"}
GLOBAL_CONTEXT_PATH = "docs/active-context.md"


def load_progress_snapshot(limit_rounds: int = 60, limit_events: int = 200) -> dict[str, Any]:
    tracker = _load_tracker()
    registry = load_track_registry()
    iterations = _load_iteration_board(limit_rounds=limit_rounds)
    regression_runs = _load_regression_runs(limit_runs=limit_rounds)
    plan_items = tracker.get("plan", [])
    manual_events = tracker.get("events", [])
    gates = load_gate_statuses()
    branches = load_branch_snapshots()
    tracks = _build_track_cards(registry, plan_items, gates, branches, manual_events)
    alerts = _build_alerts(tracks, branches, gates)
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
            "runtime_tracker_path": str(_runtime_tracker_path()),
        },
        "registry": {
            "updated_at": registry.get("updated_at", ""),
            "track_count": len(registry.get("tracks", [])),
        },
        "tracks": tracks,
        "branches": branches,
        "gates": gates,
        "alerts": alerts,
        "kpis": _build_kpis(iterations, plan_items, regression_runs, tracks, alerts),
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
        "next_actions": _build_next_actions(plan_items, tracks),
    }


def load_track_registry() -> dict[str, Any]:
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    if not TRACK_REGISTRY_PATH.exists():
        payload = _default_track_registry()
        TRACK_REGISTRY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    try:
        payload = json.loads(TRACK_REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = _default_track_registry()
        TRACK_REGISTRY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return payload
    return _sanitize_track_registry(payload)


def add_progress_event(
    *,
    title: str,
    detail: str = "",
    category: str = "manual",
    status: str = "DONE",
    event_time: str | None = None,
    track_id: str = "",
    branch_name: str = "",
    event_type: str = "manual",
) -> dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Event title cannot be empty.")
    tracker = _load_tracker()
    normalized_category = category if category in ALLOWED_EVENT_CATEGORY else "manual"
    normalized_status = status if status in ALLOWED_EVENT_STATUS else "INFO"
    normalized_event_type = event_type if event_type in ALLOWED_EVENT_TYPE else "manual"
    event = {
        "id": f"evt-{uuid.uuid4().hex[:10]}",
        "time": _normalize_time(event_time) if event_time else _now_iso(),
        "category": normalized_category,
        "status": normalized_status,
        "title": clean_title,
        "detail": detail.strip(),
        "source": "manual",
        "track_id": str(track_id or "").strip(),
        "branch_name": str(branch_name or "").strip(),
        "event_type": normalized_event_type,
    }
    events = list(tracker.get("events", []))
    events.append(event)
    tracker["events"] = _sort_events_desc(events)[:800]
    tracker["updated_at"] = _now_iso()
    _apply_event_to_plan(tracker, event)
    _save_tracker(tracker)
    return event


def replace_plan_items(items: list[dict[str, Any]]) -> dict[str, Any]:
    tracker = _load_tracker()
    normalized = _normalize_plan_items(items)
    tracker["plan"] = normalized
    tracker["updated_at"] = _now_iso()
    _save_tracker(tracker)
    return {"count": len(normalized), "updated_at": tracker["updated_at"], "items": normalized}


def bootstrap_control_tower(*, force: bool = False) -> dict[str, Any]:
    tracker = _load_tracker()
    registry = load_track_registry()
    existing_by_track = {
        str(item.get("track_id") or "").strip(): item
        for item in tracker.get("plan", [])
        if str(item.get("track_id") or "").strip()
    }
    plan_items: list[dict[str, Any]] = []
    for track in registry.get("tracks", []):
        track_id = str(track.get("id") or "").strip()
        if not track_id:
            continue
        if force or track_id not in existing_by_track:
            plan_items.append(_track_to_plan_item(track))
        else:
            merged = dict(existing_by_track[track_id])
            merged.setdefault("branch_name", track.get("branch_name", ""))
            merged.setdefault("worktree_path", track.get("worktree_path", ""))
            merged.setdefault("scope_summary", track.get("scope_summary", ""))
            merged.setdefault("acceptance_gate", ", ".join(track.get("required_gates", [])))
            merged.setdefault("track_id", track_id)
            merged.setdefault("drift_status", "ON_PLAN")
            plan_items.append(_normalize_plan_items([merged])[0])
    tracker["plan"] = plan_items
    tracker.setdefault("gates", [])
    tracker["updated_at"] = _now_iso()
    _save_tracker(tracker)
    return {
        "ok": True,
        "plan_count": len(plan_items),
        "gate_count": len(load_gate_statuses()),
        "runtime_tracker_path": str(_runtime_tracker_path()),
    }


def update_progress_checkpoint(
    *,
    track_id: str,
    progress_pct: int,
    status: str,
    summary: str,
    branch_name: str = "",
) -> dict[str, Any]:
    tracker = _load_tracker()
    track = _get_track(track_id)
    plan_item = _ensure_plan_item_for_track(tracker, track)
    normalized_status = status.strip().upper() if status else "INFO"
    event_status = normalized_status if normalized_status in ALLOWED_EVENT_STATUS else "INFO"
    plan_item["progress_pct"] = _coerce_int(progress_pct, default=plan_item.get("progress_pct", 0), lower=0, upper=100)
    plan_item["branch_name"] = branch_name.strip() or str(plan_item.get("branch_name") or track.get("branch_name") or "")
    plan_item["last_update_at"] = _now_iso()
    plan_item["latest_event_summary"] = summary.strip()
    if event_status == "DONE":
        plan_item["status"] = "DONE"
        plan_item["drift_status"] = "ON_PLAN"
        plan_item["progress_pct"] = max(100, int(plan_item["progress_pct"]))
    elif event_status == "BLOCKED":
        plan_item["status"] = "BLOCKED"
        plan_item["drift_status"] = "RISK"
    else:
        plan_item["status"] = "IN_PROGRESS"
        if event_status == "RISK":
            plan_item["drift_status"] = "RISK"
    tracker["updated_at"] = _now_iso()
    event = {
        "id": f"evt-{uuid.uuid4().hex[:10]}",
        "time": _now_iso(),
        "category": "manual",
        "status": event_status,
        "title": f"{track.get('title', track_id)} checkpoint",
        "detail": summary.strip(),
        "source": "manual",
        "track_id": track_id,
        "branch_name": plan_item["branch_name"],
        "event_type": "checkpoint",
    }
    tracker.setdefault("events", []).append(event)
    tracker["events"] = _sort_events_desc(tracker["events"])[:800]
    _save_tracker(tracker)
    return {"ok": True, "event": event, "item": plan_item}


def update_gate(
    *,
    track_id: str,
    gate_name: str,
    result: str,
    detail: str = "",
) -> dict[str, Any]:
    clean_track_id = track_id.strip()
    clean_gate = gate_name.strip()
    if not clean_track_id:
        raise ValueError("track_id is required.")
    if not clean_gate:
        raise ValueError("gate_name is required.")
    normalized_result = result.strip().upper()
    status = normalized_result if normalized_result in ALLOWED_GATE_STATUS else "PENDING"
    tracker = _load_tracker()
    gates = list(tracker.get("gates", []))
    gate_entry = {
        "track_id": clean_track_id,
        "gate_name": clean_gate,
        "status": status,
        "checked_at": _now_iso(),
        "detail": detail.strip(),
    }
    replaced = False
    for idx, existing in enumerate(gates):
        if existing.get("track_id") == clean_track_id and existing.get("gate_name") == clean_gate:
            gates[idx] = gate_entry
            replaced = True
            break
    if not replaced:
        gates.append(gate_entry)
    tracker["gates"] = gates
    track = _get_track(clean_track_id)
    plan_item = _ensure_plan_item_for_track(tracker, track)
    plan_item["last_update_at"] = gate_entry["checked_at"]
    plan_item["latest_event_summary"] = f"{clean_gate}: {status}"
    if status in {"FAIL", "RISK"}:
        plan_item["drift_status"] = "RISK"
    event_status = "DONE" if status == "PASS" else "RISK" if status in {"FAIL", "RISK"} else "INFO"
    tracker.setdefault("events", []).append(
        {
            "id": f"evt-{uuid.uuid4().hex[:10]}",
            "time": gate_entry["checked_at"],
            "category": "quality",
            "status": event_status,
            "title": f"{track.get('title', clean_track_id)} gate {clean_gate}",
            "detail": detail.strip() or status,
            "source": "manual",
            "track_id": clean_track_id,
            "branch_name": str(plan_item.get("branch_name") or track.get("branch_name") or ""),
            "event_type": "gate_pass" if status == "PASS" else "gate_fail",
        }
    )
    tracker["events"] = _sort_events_desc(tracker["events"])[:800]
    tracker["updated_at"] = _now_iso()
    _save_tracker(tracker)
    return {"ok": True, "gate": gate_entry, "item": plan_item}


def load_branch_snapshots() -> list[dict[str, Any]]:
    registry = load_track_registry()
    tracker = _load_tracker()
    plan_items = tracker.get("plan", [])
    events = tracker.get("events", [])
    branches: list[dict[str, Any]] = []
    for track in registry.get("tracks", []):
        branches.append(_inspect_track_branch(track, plan_items, events))
    branches.sort(key=lambda item: (_coerce_int(item.get("lane_order"), default=999), item.get("track_id", "")))
    return branches


def load_gate_statuses() -> list[dict[str, Any]]:
    registry = load_track_registry()
    tracker = _load_tracker()
    stored = {
        (str(item.get("track_id") or "").strip(), str(item.get("gate_name") or "").strip()): item
        for item in tracker.get("gates", [])
        if isinstance(item, dict)
    }
    gate_order: dict[tuple[str, str], tuple[int, int]] = {}
    gates: list[dict[str, Any]] = []
    for track_index, track in enumerate(registry.get("tracks", [])):
        track_id = str(track.get("id") or "").strip()
        required = track.get("required_gates", [])
        for gate_index, gate_name in enumerate(required):
            key = (track_id, str(gate_name or "").strip())
            gate_order[key] = (track_index, gate_index)
            entry = dict(stored.get(key) or {})
            gates.append(
                {
                    "track_id": track_id,
                    "track_title": str(track.get("title") or track_id),
                    "gate_name": str(gate_name or "").strip(),
                    "status": str(entry.get("status") or "PENDING"),
                    "checked_at": str(entry.get("checked_at") or ""),
                    "detail": str(entry.get("detail") or ""),
                    "branch_name": str(track.get("branch_name") or ""),
                }
            )
    gates.sort(
        key=lambda item: gate_order.get(
            (str(item.get("track_id") or "").strip(), str(item.get("gate_name") or "").strip()),
            (999, 999),
        )
    )
    return gates


def _load_tracker() -> dict[str, Any]:
    tracker_path = _runtime_tracker_path()
    tracker_path.parent.mkdir(parents=True, exist_ok=True)
    if not tracker_path.exists():
        tracker = _load_bootstrap_tracker()
        _save_tracker_to_path(tracker_path, tracker)
        return tracker
    try:
        payload = json.loads(tracker_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        payload = _load_bootstrap_tracker()
        _save_tracker_to_path(tracker_path, payload)
        return payload
    return _sanitize_tracker(payload)


def _load_bootstrap_tracker() -> dict[str, Any]:
    if TRACKER_BOOTSTRAP_PATH.exists():
        try:
            payload = json.loads(TRACKER_BOOTSTRAP_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = _default_tracker()
    else:
        payload = _default_tracker()
    return _sanitize_tracker(payload)


def _save_tracker(payload: dict[str, Any]) -> None:
    _save_tracker_to_path(_runtime_tracker_path(), payload)


def _save_tracker_to_path(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_tracker(payload: dict[str, Any]) -> dict[str, Any]:
    project_name = str(payload.get("project_name") or "ReqPilotX Project Control Tower")
    north_star = str(
        payload.get("north_star")
        or "Keep governance, quality gates, and active tracks aligned while the product matures toward trial run."
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
                    "source": str(event.get("source") or "manual"),
                    "track_id": str(event.get("track_id") or "").strip(),
                    "branch_name": str(event.get("branch_name") or "").strip(),
                    "event_type": str(event.get("event_type") or "manual")
                    if str(event.get("event_type") or "manual") in ALLOWED_EVENT_TYPE
                    else "manual",
                }
            )
    safe_gates: list[dict[str, Any]] = []
    raw_gates = payload.get("gates", [])
    if isinstance(raw_gates, list):
        for gate in raw_gates:
            if not isinstance(gate, dict):
                continue
            track_id = str(gate.get("track_id") or "").strip()
            gate_name = str(gate.get("gate_name") or "").strip()
            if not track_id or not gate_name:
                continue
            status = str(gate.get("status") or "PENDING").strip().upper()
            if status not in ALLOWED_GATE_STATUS:
                status = "PENDING"
            safe_gates.append(
                {
                    "track_id": track_id,
                    "gate_name": gate_name,
                    "status": status,
                    "checked_at": _normalize_time(str(gate.get("checked_at") or _now_iso())),
                    "detail": str(gate.get("detail") or "").strip(),
                }
            )
    return {
        "project_name": project_name,
        "north_star": north_star,
        "updated_at": _normalize_time(str(payload.get("updated_at") or _now_iso())),
        "plan": safe_plan,
        "events": _sort_events_desc(safe_events),
        "gates": safe_gates,
    }


def _default_tracker() -> dict[str, Any]:
    now = _now_iso()
    return {
        "project_name": "ReqPilotX Project Control Tower",
        "north_star": "Keep governance, quality gates, and active tracks aligned while the product matures toward trial run.",
        "updated_at": now,
        "plan": [],
        "events": [
            {
                "id": "evt-bootstrap",
                "time": now,
                "category": "note",
                "status": "INFO",
                "title": "Control tower runtime initialized",
                "detail": "Runtime tracker was seeded from bootstrap state.",
                "source": "manual",
                "track_id": "",
                "branch_name": "",
                "event_type": "manual",
            }
        ],
        "gates": [],
    }


def _default_track_registry() -> dict[str, Any]:
    root = REPO_ROOT
    tracks = [
        {
            "id": "release-foundation",
            "title": "Release Foundation",
            "lane_order": 1,
            "status": "ACTIVE",
            "branch_name": "codex/release-foundation",
            "worktree_path": str(root / "worktrees" / "_active" / "release-foundation"),
            "skill_name": "reqpilot-track-release-foundation",
            "branch_context_doc": "prototype/docs/release-foundation-active-context.md",
            "scope_summary": "P0 stability, Entra auth, feedback skeleton, and trial-run foundation.",
            "non_goals": "Do not tune extraction quality, RAG ranking, or product-package architecture in this track.",
            "allowed_paths": [
                "prototype/app/main.py",
                "prototype/app/db.py",
                "prototype/app/runtime_readiness.py",
                "prototype/app/static/*",
                "prototype/tests/*",
                "prototype/scripts/*",
            ],
            "blocked_paths": [
                "prototype/app/services/parser.py",
                "prototype/app/services/extractor.py",
                "prototype/app/services/extraction_preview.py",
                "prototype/app/services/ai_copilot.py",
                "prototype/app/services/rag.py",
                "prototype/scripts/azure_rag_smoke.py",
            ],
            "required_docs": [
                "AGENTS.md",
                "docs/项目.md",
                "docs/开发执行清单.md",
                "docs/active-context.md",
                "prototype/eval/progress/track_registry.json",
                "prototype/docs/release-foundation-active-context.md",
            ],
            "required_gates": ["p0-smoke", "auth-login", "feedback-roundtrip", "health-readiness"],
            "owner_role": "Coordinator + Backend/Ops",
            "target_date": "",
        },
        {
            "id": "extraction-quality-loop",
            "title": "Extraction Quality Loop",
            "lane_order": 2,
            "status": "ACTIVE",
            "branch_name": "codex/extraction-quality-loop",
            "worktree_path": str(root / "worktrees" / "_active" / "extraction-quality-loop"),
            "skill_name": "reqpilot-track-extraction-quality",
            "branch_context_doc": "prototype/docs/extraction-active-context.md",
            "scope_summary": "Batch extraction regression loop, error taxonomy, and benchmark stability.",
            "non_goals": "Do not change auth, feedback, or RAG evidence logic in this track.",
            "allowed_paths": [
                "prototype/app/services/parser.py",
                "prototype/app/services/extractor.py",
                "prototype/app/services/extraction_preview.py",
                "prototype/scripts/*",
                "prototype/tests/*",
                "prototype/eval/*",
            ],
            "blocked_paths": [
                "prototype/app/main.py",
                "prototype/app/db.py",
                "prototype/app/runtime_readiness.py",
                "prototype/app/services/ai_copilot.py",
                "prototype/app/services/rag.py",
                "prototype/app/static/app.js",
            ],
            "required_docs": [
                "AGENTS.md",
                "docs/项目.md",
                "docs/开发执行清单.md",
                "docs/active-context.md",
                "prototype/eval/progress/track_registry.json",
                "prototype/docs/extraction-active-context.md",
            ],
            "required_gates": ["batch-report", "oil-pump-benchmark", "regression-suite"],
            "owner_role": "AI/Extraction",
            "target_date": "",
        },
        {
            "id": "rag-quality-loop",
            "title": "RAG Quality Loop",
            "lane_order": 3,
            "status": "ACTIVE",
            "branch_name": "codex/rag-quality-loop",
            "worktree_path": str(root / "worktrees" / "_active" / "rag-quality-loop"),
            "skill_name": "reqpilot-track-rag-quality",
            "branch_context_doc": "prototype/docs/rag-quality-active-context.md",
            "scope_summary": "Labeled evidence set, retrieval tuning, and judgment-quality gates.",
            "non_goals": "Do not touch extraction parser internals or Entra auth implementation in this track.",
            "allowed_paths": [
                "prototype/app/services/ai_copilot.py",
                "prototype/app/services/rag.py",
                "prototype/scripts/azure_rag_smoke.py",
                "prototype/tests/*",
                "prototype/eval/*",
            ],
            "blocked_paths": [
                "prototype/app/services/parser.py",
                "prototype/app/services/extractor.py",
                "prototype/app/runtime_readiness.py",
                "prototype/app/main.py",
                "prototype/app/static/app.js",
            ],
            "required_docs": [
                "AGENTS.md",
                "docs/项目.md",
                "docs/开发执行清单.md",
                "docs/active-context.md",
                "prototype/eval/progress/track_registry.json",
                "prototype/docs/rag-quality-active-context.md",
            ],
            "required_gates": ["sample10", "labeled-set", "evidence-metrics"],
            "owner_role": "AI/RAG",
            "target_date": "",
        },
        {
            "id": "product-kb",
            "title": "Product Knowledge Base",
            "lane_order": 4,
            "status": "QUEUED",
            "branch_name": "codex/product-kb",
            "worktree_path": str(root / "worktrees" / "_active" / "product-kb"),
            "skill_name": "reqpilot-track-product-kb",
            "branch_context_doc": "prototype/docs/product-kb-active-context.md",
            "scope_summary": "Dual-scope knowledge architecture, package binding, and admin curation.",
            "non_goals": "Do not replace the entire upload flow or Angular shell in this track.",
            "allowed_paths": [
                "prototype/app/db.py",
                "prototype/app/main.py",
                "prototype/app/services/ai_copilot.py",
                "prototype/app/static/*",
                "prototype/tests/*",
            ],
            "blocked_paths": [
                "prototype/app/services/parser.py",
                "prototype/app/services/extractor.py",
                "prototype/app/runtime_readiness.py",
            ],
            "required_docs": [
                "AGENTS.md",
                "docs/项目.md",
                "docs/开发执行清单.md",
                "docs/active-context.md",
                "prototype/eval/progress/track_registry.json",
                "prototype/docs/product-kb-active-context.md",
            ],
            "required_gates": ["package-binding", "dual-scope-retrieval", "admin-curation"],
            "owner_role": "Backend/Product",
            "target_date": "",
        },
        {
            "id": "angular-feasibility",
            "title": "Angular Feasibility",
            "lane_order": 5,
            "status": "QUEUED",
            "branch_name": "codex/angular-feasibility",
            "worktree_path": str(root / "worktrees" / "_active" / "angular-feasibility"),
            "skill_name": "reqpilot-track-angular-feasibility",
            "branch_context_doc": "prototype/docs/angular-feasibility-active-context.md",
            "scope_summary": "Angular shell, auth integration feasibility, and migration report.",
            "non_goals": "Do not replace the current workspace UI or change production runtime architecture in this track.",
            "allowed_paths": [
                "prototype/frontend/angular-shell/*",
                "prototype/tests/*",
                "prototype/scripts/*",
            ],
            "blocked_paths": [
                "prototype/app/static/index.html",
                "prototype/app/static/app.js",
                "prototype/app/static/styles.css",
                "prototype/app/static/home.*",
                "prototype/app/static/progress-dashboard.*",
            ],
            "required_docs": [
                "AGENTS.md",
                "docs/项目.md",
                "docs/开发执行清单.md",
                "docs/active-context.md",
                "prototype/eval/progress/track_registry.json",
                "prototype/docs/angular-feasibility-active-context.md",
            ],
            "required_gates": ["shell-build", "msal-login", "migration-report"],
            "owner_role": "Frontend/Spike",
            "target_date": "",
        },
    ]
    return _sanitize_track_registry({"updated_at": _now_iso(), "tracks": tracks})


def _sanitize_track_registry(payload: dict[str, Any]) -> dict[str, Any]:
    raw_tracks = payload.get("tracks", [])
    safe_tracks: list[dict[str, Any]] = []
    if isinstance(raw_tracks, list):
        for idx, track in enumerate(raw_tracks, start=1):
            if not isinstance(track, dict):
                continue
            track_id = str(track.get("id") or "").strip()
            title = str(track.get("title") or track_id).strip()
            if not track_id or not title:
                continue
            safe_tracks.append(
                {
                    "id": track_id,
                    "title": title,
                    "lane_order": _coerce_int(track.get("lane_order"), default=idx, lower=1),
                    "status": str(track.get("status") or "QUEUED").strip().upper(),
                    "branch_name": str(track.get("branch_name") or "").strip(),
                    "worktree_path": str(track.get("worktree_path") or "").strip(),
                    "skill_name": str(track.get("skill_name") or "").strip(),
                    "branch_context_doc": str(track.get("branch_context_doc") or "").strip(),
                    "scope_summary": str(track.get("scope_summary") or "").strip(),
                    "non_goals": str(track.get("non_goals") or "").strip(),
                    "allowed_paths": _normalize_path_list(track.get("allowed_paths")),
                    "blocked_paths": _normalize_path_list(track.get("blocked_paths")),
                    "required_docs": _normalize_path_list(track.get("required_docs")),
                    "required_gates": [str(item).strip() for item in track.get("required_gates", []) if str(item).strip()],
                    "owner_role": str(track.get("owner_role") or "").strip(),
                    "target_date": str(track.get("target_date") or "").strip(),
                }
            )
    safe_tracks.sort(key=lambda item: (item["lane_order"], item["id"]))
    return {
        "updated_at": _normalize_time(str(payload.get("updated_at") or _now_iso())),
        "tracks": safe_tracks,
    }


def _normalize_path_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


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
    tracks: list[dict[str, Any]],
    alerts: list[dict[str, Any]],
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
    active_tracks = [track for track in tracks if str(track.get("track_status") or "") in TRACK_STATUS_ACTIVE]
    queued_tracks = [track for track in tracks if str(track.get("track_status") or "") in TRACK_STATUS_QUEUED]
    risk_tracks = [track for track in tracks if str(track.get("drift_status") or "") == "RISK"]
    off_plan_tracks = [track for track in tracks if str(track.get("drift_status") or "") == "OFF_PLAN"]
    latest_gate_passes = len([track for track in tracks if bool(track.get("all_required_gates_passed"))])
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
            "active_tracks": len(active_tracks),
            "queued_tracks": len(queued_tracks),
            "risk_tracks": len(risk_tracks),
            "off_plan_tracks": len(off_plan_tracks),
            "latest_gate_passed_tracks": latest_gate_passes,
            "alert_count": len(alerts),
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
    track_progress = _average_progress(
        [{"progress_pct": _coerce_int(track.get("progress_pct"), default=0, lower=0, upper=100)} for track in tracks]
    )
    overall_index = round(split_progress * 0.4 + parse_progress * 0.15 + plan_progress * 0.2 + track_progress * 0.25, 2)
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
        "active_tracks": len(active_tracks),
        "queued_tracks": len(queued_tracks),
        "risk_tracks": len(risk_tracks),
        "off_plan_tracks": len(off_plan_tracks),
        "latest_gate_passed_tracks": latest_gate_passes,
        "alert_count": len(alerts),
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
                "track_id": "",
                "branch_name": "",
                "event_type": "manual",
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
                "track_id": "",
                "branch_name": "",
                "event_type": "manual",
            }
        )
    merged = auto_events + regression_events + list(manual_events)
    merged = _sort_events_desc(merged)
    if limit_events > 0:
        merged = merged[:limit_events]
    return merged


def _build_next_actions(plan_items: list[dict[str, Any]], tracks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    track_by_id = {str(track.get("id") or ""): track for track in tracks}
    next_actions = [
        {
            "id": item.get("id", ""),
            "title": item.get("title", ""),
            "owner": item.get("owner", ""),
            "status": item.get("status", "TODO"),
            "progress_pct": _coerce_int(item.get("progress_pct"), default=0, lower=0, upper=100),
            "target_date": item.get("target_date", ""),
            "notes": item.get("notes", ""),
            "track_id": item.get("track_id", ""),
            "branch_name": item.get("branch_name", ""),
            "drift_status": item.get("drift_status", "ON_PLAN"),
            "acceptance_gate": item.get("acceptance_gate", ""),
            "latest_event_summary": item.get("latest_event_summary", ""),
            "next_gate": track_by_id.get(str(item.get("track_id") or ""), {}).get("next_gate_name", ""),
        }
        for item in plan_items
        if item.get("status") in {"TODO", "IN_PROGRESS", "BLOCKED"}
    ]
    next_actions.sort(
        key=lambda item: (
            item.get("status") != "IN_PROGRESS",
            item.get("drift_status") == "OFF_PLAN",
            item.get("drift_status") == "RISK",
            -item.get("progress_pct", 0),
        )
    )
    return next_actions[:8]


def _build_track_cards(
    registry: dict[str, Any],
    plan_items: list[dict[str, Any]],
    gates: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    plan_by_track = {str(item.get("track_id") or ""): item for item in plan_items if str(item.get("track_id") or "").strip()}
    branch_by_track = {str(item.get("track_id") or ""): item for item in branches if str(item.get("track_id") or "").strip()}
    event_by_track: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        track_id = str(event.get("track_id") or "").strip()
        if not track_id:
            continue
        event_by_track.setdefault(track_id, []).append(event)
    gate_by_track: dict[str, list[dict[str, Any]]] = {}
    for gate in gates:
        gate_by_track.setdefault(str(gate.get("track_id") or ""), []).append(gate)

    cards: list[dict[str, Any]] = []
    for track in registry.get("tracks", []):
        track_id = str(track.get("id") or "").strip()
        plan_item = plan_by_track.get(track_id) or _track_to_plan_item(track)
        branch = branch_by_track.get(track_id) or {}
        track_gates = gate_by_track.get(track_id, [])
        pass_count = len([gate for gate in track_gates if gate.get("status") == "PASS"])
        fail_count = len([gate for gate in track_gates if gate.get("status") == "FAIL"])
        risk_count = len([gate for gate in track_gates if gate.get("status") == "RISK"])
        next_gate = next((gate for gate in track_gates if gate.get("status") != "PASS"), None)
        latest_event = event_by_track.get(track_id, [{}])[0] if event_by_track.get(track_id) else {}
        latest_event_time = str(latest_event.get("time") or plan_item.get("last_update_at") or "")
        all_required_gates_passed = bool(track_gates) and all(gate.get("status") == "PASS" for gate in track_gates)
        drift_status = str(branch.get("drift_status") or plan_item.get("drift_status") or "ON_PLAN")
        cards.append(
            {
                "id": track_id,
                "title": str(track.get("title") or track_id),
                "lane_order": _coerce_int(track.get("lane_order"), default=999),
                "track_status": str(track.get("status") or plan_item.get("status") or "QUEUED"),
                "plan_status": str(plan_item.get("status") or "TODO"),
                "progress_pct": _coerce_int(plan_item.get("progress_pct"), default=0, lower=0, upper=100),
                "owner": str(plan_item.get("owner") or track.get("owner_role") or ""),
                "branch_name": str(branch.get("branch_name") or plan_item.get("branch_name") or track.get("branch_name") or ""),
                "worktree_path": str(branch.get("worktree_path") or plan_item.get("worktree_path") or track.get("worktree_path") or ""),
                "skill_name": str(track.get("skill_name") or ""),
                "scope_summary": str(plan_item.get("scope_summary") or track.get("scope_summary") or ""),
                "non_goals": str(track.get("non_goals") or ""),
                "acceptance_gate": str(plan_item.get("acceptance_gate") or ", ".join(track.get("required_gates", []))),
                "required_gate_count": len(track_gates),
                "passed_gate_count": pass_count,
                "failed_gate_count": fail_count,
                "risk_gate_count": risk_count,
                "next_gate_name": str(next_gate.get("gate_name") or ""),
                "next_gate_status": str(next_gate.get("status") or ""),
                "all_required_gates_passed": all_required_gates_passed,
                "drift_status": drift_status,
                "git_status": str(branch.get("git_status") or ""),
                "latest_event_summary": str(plan_item.get("latest_event_summary") or latest_event.get("detail") or ""),
                "latest_event_title": str(latest_event.get("title") or ""),
                "latest_event_time": latest_event_time,
                "last_commit_at": str(branch.get("last_commit_at") or ""),
                "target_date": str(plan_item.get("target_date") or track.get("target_date") or ""),
            }
        )
    cards.sort(key=lambda item: (item["lane_order"], item["id"]))
    return cards


def _build_alerts(
    tracks: list[dict[str, Any]],
    branches: list[dict[str, Any]],
    gates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    branch_by_track = {str(item.get("track_id") or ""): item for item in branches}
    for track in tracks:
        track_id = str(track.get("id") or "")
        branch = branch_by_track.get(track_id, {})
        drift_status = str(track.get("drift_status") or "")
        if drift_status == "OFF_PLAN":
            alerts.append(
                {
                    "severity": "OFF_PLAN",
                    "track_id": track_id,
                    "title": f"{track.get('title', track_id)} drifted off plan",
                    "detail": str(branch.get("drift_detail") or "Changed files left the declared track scope."),
                }
            )
        elif drift_status == "RISK":
            alerts.append(
                {
                    "severity": "RISK",
                    "track_id": track_id,
                    "title": f"{track.get('title', track_id)} requires attention",
                    "detail": str(branch.get("drift_detail") or track.get("latest_event_summary") or "Gate or checkpoint risk detected."),
                }
            )
        latest_event_time = _parse_datetime(str(track.get("latest_event_time") or ""))
        if str(track.get("track_status") or "") in TRACK_STATUS_ACTIVE and latest_event_time is not None:
            if datetime.now() - latest_event_time > timedelta(days=2):
                alerts.append(
                    {
                        "severity": "RISK",
                        "track_id": track_id,
                        "title": f"{track.get('title', track_id)} has no recent checkpoint",
                        "detail": f"Latest tracked activity was at {latest_event_time.isoformat(timespec='seconds')}.",
                    }
                )
    for gate in gates:
        if str(gate.get("status") or "") in {"FAIL", "RISK"}:
            alerts.append(
                {
                    "severity": "RISK" if gate.get("status") == "RISK" else "OFF_PLAN",
                    "track_id": str(gate.get("track_id") or ""),
                    "title": f"{gate.get('track_title', gate.get('track_id', 'track'))} gate {gate.get('gate_name', '')} is {gate.get('status', '')}",
                    "detail": str(gate.get("detail") or ""),
                }
            )
    return alerts[:24]


def _inspect_track_branch(track: dict[str, Any], plan_items: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    track_id = str(track.get("id") or "").strip()
    declared_branch = str(track.get("branch_name") or "").strip()
    declared_path = Path(str(track.get("worktree_path") or "").strip())
    plan_item = next((item for item in plan_items if item.get("track_id") == track_id), {})
    latest_event = next((item for item in events if item.get("track_id") == track_id), {})
    if not declared_path.exists():
        active_track = str(track.get("status") or "").strip().upper() in TRACK_STATUS_ACTIVE
        plan_status = str(plan_item.get("status") or "TODO").strip().upper()
        if active_track and plan_status in {"IN_PROGRESS", "BLOCKED"}:
            drift_status = "OFF_PLAN"
            git_status = "missing_worktree"
            detail = "Started track worktree is missing."
        else:
            drift_status = "ON_PLAN"
            git_status = "not_started"
            detail = "Track worktree has not been created yet."
        return {
            "track_id": track_id,
            "track_title": str(track.get("title") or track_id),
            "lane_order": _coerce_int(track.get("lane_order"), default=999),
            "branch_name": declared_branch,
            "worktree_path": str(declared_path),
            "git_status": git_status,
            "dirty": False,
            "last_commit_sha": "",
            "last_commit_at": "",
            "ahead_behind": {"ahead": 0, "behind": 0, "has_upstream": False},
            "changed_files": [],
            "drift_status": drift_status,
            "drift_detail": detail,
            "latest_event_time": str(latest_event.get("time") or plan_item.get("last_update_at") or ""),
        }

    actual_branch = _run_git(["branch", "--show-current"], declared_path).strip()
    status_lines = _run_git(["status", "--short"], declared_path).splitlines()
    changed_files = [_parse_status_path(line) for line in status_lines if _parse_status_path(line)]
    dirty = bool(changed_files)
    commit_meta = _run_git(["log", "-1", "--format=%H%x1f%cI"], declared_path).strip()
    last_commit_sha = ""
    last_commit_at = ""
    if commit_meta:
        parts = commit_meta.split("\x1f")
        last_commit_sha = parts[0]
        if len(parts) > 1:
            last_commit_at = parts[1]
    ahead_behind = _ahead_behind(declared_path)
    drift_status = "ON_PLAN"
    drift_detail = "Branch matches declared scope."
    if actual_branch and declared_branch and actual_branch != declared_branch:
        drift_status = "OFF_PLAN"
        drift_detail = f"Expected branch {declared_branch}, found {actual_branch}."
    else:
        changed_scope = _classify_changed_paths(changed_files, track)
        if changed_scope["blocked"]:
            drift_status = "OFF_PLAN"
            drift_detail = f"Blocked paths changed: {', '.join(changed_scope['blocked'][:4])}"
        elif changed_scope["out_of_scope"]:
            drift_status = "OFF_PLAN"
            drift_detail = f"Out-of-scope paths changed: {', '.join(changed_scope['out_of_scope'][:4])}"
        elif changed_scope["allowed"] and str(plan_item.get("status") or "") not in {"DONE", "BLOCKED"}:
            drift_status = "ON_PLAN"
            drift_detail = "Changed files stay within allowed scope."
        latest_event_time = _parse_datetime(str(latest_event.get("time") or plan_item.get("last_update_at") or ""))
        if drift_status == "ON_PLAN" and latest_event_time is not None:
            if datetime.now() - latest_event_time > timedelta(days=2) and str(track.get("status") or "").upper() in TRACK_STATUS_ACTIVE:
                drift_status = "RISK"
                drift_detail = "No checkpoint was recorded in the last 48 hours."
    if any(str(event.get("status") or "") == "BLOCKED" for event in events if event.get("track_id") == track_id):
        drift_status = "RISK" if drift_status == "ON_PLAN" else drift_status
    return {
        "track_id": track_id,
        "track_title": str(track.get("title") or track_id),
        "lane_order": _coerce_int(track.get("lane_order"), default=999),
        "branch_name": actual_branch or declared_branch,
        "worktree_path": str(declared_path),
        "git_status": "dirty" if dirty else "clean",
        "dirty": dirty,
        "last_commit_sha": last_commit_sha,
        "last_commit_at": last_commit_at,
        "ahead_behind": ahead_behind,
        "changed_files": changed_files,
        "drift_status": drift_status,
        "drift_detail": drift_detail,
        "latest_event_time": str(latest_event.get("time") or plan_item.get("last_update_at") or ""),
    }


def _classify_changed_paths(changed_files: list[str], track: dict[str, Any]) -> dict[str, list[str]]:
    allowed_patterns = list(track.get("allowed_paths", [])) + [GLOBAL_CONTEXT_PATH, str(track.get("branch_context_doc") or "")]
    blocked_patterns = list(track.get("blocked_paths", []))
    allowed: list[str] = []
    blocked: list[str] = []
    out_of_scope: list[str] = []
    for path in changed_files:
        normalized = path.replace("\\", "/")
        if _matches_any(normalized, blocked_patterns):
            blocked.append(normalized)
        elif _matches_any(normalized, allowed_patterns):
            allowed.append(normalized)
        else:
            out_of_scope.append(normalized)
    return {"allowed": allowed, "blocked": blocked, "out_of_scope": out_of_scope}


def _matches_any(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        clean = str(pattern or "").replace("\\", "/").strip()
        if not clean:
            continue
        if fnmatch.fnmatch(path, clean):
            return True
        if clean.endswith("*") and path.startswith(clean[:-1]):
            return True
        if path == clean:
            return True
    return False


def _parse_status_path(line: str) -> str:
    value = str(line or "").rstrip()
    if not value:
        return ""
    token = value[3:] if len(value) > 3 else value
    if " -> " in token:
        token = token.split(" -> ", 1)[1]
    return token.strip()


def _ahead_behind(cwd: Path) -> dict[str, Any]:
    upstream = _run_git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"], cwd).strip()
    if not upstream:
        return {"ahead": 0, "behind": 0, "has_upstream": False}
    counts = _run_git(["rev-list", "--left-right", "--count", f"{upstream}...HEAD"], cwd).strip()
    if not counts:
        return {"ahead": 0, "behind": 0, "has_upstream": True}
    parts = counts.split()
    if len(parts) != 2:
        return {"ahead": 0, "behind": 0, "has_upstream": True}
    behind = _coerce_int(parts[0], default=0, lower=0)
    ahead = _coerce_int(parts[1], default=0, lower=0)
    return {"ahead": ahead, "behind": behind, "has_upstream": True}


def _run_git(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return ""
    return result.stdout.strip()


def _track_lane_order(track_id: str, registry: dict[str, Any]) -> int:
    for track in registry.get("tracks", []):
        if str(track.get("id") or "") == track_id:
            return _coerce_int(track.get("lane_order"), default=999)
    return 999


def _get_track(track_id: str) -> dict[str, Any]:
    registry = load_track_registry()
    for track in registry.get("tracks", []):
        if str(track.get("id") or "").strip() == track_id.strip():
            return track
    raise ValueError(f"Unknown track_id: {track_id}")


def _track_to_plan_item(track: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"track-{track.get('id', '')}",
        "title": str(track.get("title") or track.get("id") or "").strip(),
        "owner": str(track.get("owner_role") or "").strip(),
        "status": "TODO",
        "progress_pct": 0,
        "target_date": str(track.get("target_date") or "").strip(),
        "notes": str(track.get("scope_summary") or "").strip(),
        "track_id": str(track.get("id") or "").strip(),
        "branch_name": str(track.get("branch_name") or "").strip(),
        "worktree_path": str(track.get("worktree_path") or "").strip(),
        "scope_summary": str(track.get("scope_summary") or "").strip(),
        "acceptance_gate": ", ".join(track.get("required_gates", [])),
        "depends_on": "",
        "drift_status": "ON_PLAN",
        "last_update_at": "",
        "latest_event_summary": "",
    }


def _ensure_plan_item_for_track(tracker: dict[str, Any], track: dict[str, Any]) -> dict[str, Any]:
    track_id = str(track.get("id") or "").strip()
    for item in tracker.setdefault("plan", []):
        if str(item.get("track_id") or "").strip() == track_id:
            return item
    new_item = _track_to_plan_item(track)
    tracker["plan"].append(new_item)
    return new_item


def _apply_event_to_plan(tracker: dict[str, Any], event: dict[str, Any]) -> None:
    track_id = str(event.get("track_id") or "").strip()
    if not track_id:
        return
    try:
        track = _get_track(track_id)
    except ValueError:
        return
    item = _ensure_plan_item_for_track(tracker, track)
    item["last_update_at"] = str(event.get("time") or _now_iso())
    item["latest_event_summary"] = str(event.get("detail") or event.get("title") or "").strip()
    if str(event.get("branch_name") or "").strip():
        item["branch_name"] = str(event.get("branch_name") or "").strip()
    status = str(event.get("status") or "INFO").strip().upper()
    if status == "DONE" and str(event.get("event_type") or "") == "done":
        item["status"] = "DONE"
        item["progress_pct"] = 100
        item["drift_status"] = "ON_PLAN"
    elif status == "BLOCKED":
        item["status"] = "BLOCKED"
        item["drift_status"] = "RISK"
    elif status == "RISK":
        item["drift_status"] = "RISK"
        if item.get("status") == "TODO":
            item["status"] = "IN_PROGRESS"
    elif item.get("status") == "TODO":
        item["status"] = "IN_PROGRESS"


def _sort_events_desc(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(events, key=lambda item: _sort_key(str(item.get("time", ""))), reverse=True)


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
        drift_status = str(raw.get("drift_status") or "ON_PLAN").strip().upper()
        if drift_status not in ALLOWED_DRIFT_STATUS:
            drift_status = "ON_PLAN"
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
                "track_id": str(raw.get("track_id") or "").strip(),
                "branch_name": str(raw.get("branch_name") or "").strip(),
                "worktree_path": str(raw.get("worktree_path") or "").strip(),
                "scope_summary": str(raw.get("scope_summary") or "").strip(),
                "acceptance_gate": str(raw.get("acceptance_gate") or "").strip(),
                "depends_on": str(raw.get("depends_on") or "").strip(),
                "drift_status": drift_status,
                "last_update_at": _normalize_time(str(raw.get("last_update_at") or "")) if str(raw.get("last_update_at") or "").strip() else "",
                "latest_event_summary": str(raw.get("latest_event_summary") or "").strip(),
            }
        )
    return normalized


def _shared_runtime_dir() -> Path:
    env = str(os.environ.get(RUNTIME_DIR_ENV) or "").strip()
    if env:
        return Path(env).resolve()
    common_dir_raw = _run_git(["rev-parse", "--git-common-dir"], REPO_ROOT)
    if common_dir_raw:
        common_dir = Path(common_dir_raw)
        if not common_dir.is_absolute():
            common_dir = (REPO_ROOT / common_dir).resolve()
        return common_dir / RUNTIME_DIR_NAME
    return PROGRESS_DIR


def _runtime_tracker_path() -> Path:
    return _shared_runtime_dir() / TRACKER_FILE_NAME


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
    values = [_coerce_int(item.get("progress_pct"), default=0, lower=0, upper=100) for item in plan_items]
    return round(sum(values) / len(values), 2)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
