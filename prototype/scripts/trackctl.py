#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from app.services import progress_tracker


PROTOTYPE_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PROTOTYPE_DIR.parent
CONTEXT_CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_context_sync.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ReqPilotX control-tower CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Bootstrap the control tower tracker from track registry.")
    init_parser.add_argument("--force", action="store_true", help="Replace existing track plan items.")

    start_parser = subparsers.add_parser("start", help="Mark a track as started in the current worktree.")
    start_parser.add_argument("--track", required=True, help="Track id from track_registry.json")

    checkpoint_parser = subparsers.add_parser("checkpoint", help="Write a progress checkpoint.")
    checkpoint_parser.add_argument("--track", required=True, help="Track id from track_registry.json")
    checkpoint_parser.add_argument("--progress", required=True, type=int, help="Progress percentage 0-100")
    checkpoint_parser.add_argument("--status", default="INFO", help="INFO | RISK | DONE | BLOCKED")
    checkpoint_parser.add_argument("--summary", required=True, help="Checkpoint summary")

    gate_parser = subparsers.add_parser("gate", help="Record a gate result.")
    gate_parser.add_argument("--track", required=True, help="Track id from track_registry.json")
    gate_parser.add_argument("--gate", required=True, help="Gate name")
    gate_parser.add_argument("--result", required=True, help="PASS | RISK | FAIL")
    gate_parser.add_argument("--detail", default="", help="Gate detail")

    handoff_parser = subparsers.add_parser("handoff", help="Record handoff summary and next action.")
    handoff_parser.add_argument("--track", required=True, help="Track id from track_registry.json")
    handoff_parser.add_argument("--summary", required=True, help="Handoff summary")
    handoff_parser.add_argument("--next", required=True, dest="next_step", help="Next action")

    subparsers.add_parser("status", help="Print current track status snapshot.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "init":
        result = progress_tracker.bootstrap_control_tower(force=bool(args.force))
        print(f"Initialized control tower: {result['plan_count']} plan items, {result['gate_count']} gates.")
        print(result["runtime_tracker_path"])
        return 0
    if args.command == "start":
        return start_track(args.track)
    if args.command == "checkpoint":
        return checkpoint_track(args.track, args.progress, args.status, args.summary)
    if args.command == "gate":
        return gate_track(args.track, args.gate, args.result, args.detail)
    if args.command == "handoff":
        return handoff_track(args.track, args.summary, args.next_step)
    if args.command == "status":
        return print_status()
    return 1


def start_track(track_id: str) -> int:
    progress_tracker.bootstrap_control_tower(force=False)
    track = require_track(track_id)
    worktree_root = current_worktree_root()
    branch_name = current_branch(worktree_root)
    ensure_track_matches_worktree(track, worktree_root, branch_name)
    event = progress_tracker.add_progress_event(
        title=f"{track['title']} kickoff",
        detail=f"Started in {worktree_root}",
        category="manual",
        status="INFO",
        track_id=track_id,
        branch_name=branch_name,
        event_type="kickoff",
    )
    sync_plan_item(track_id, branch_name=branch_name, status="IN_PROGRESS", latest_event_summary=event["detail"])
    print(f"Started {track_id} on {branch_name}")
    return 0


def checkpoint_track(track_id: str, progress: int, status: str, summary: str) -> int:
    track = require_track(track_id)
    worktree_root = current_worktree_root()
    branch_name = current_branch(worktree_root)
    ensure_track_matches_worktree(track, worktree_root, branch_name)
    result = progress_tracker.update_progress_checkpoint(
        track_id=track_id,
        progress_pct=progress,
        status=status,
        summary=summary,
        branch_name=branch_name,
    )
    print(f"{track_id}: {result['item']['progress_pct']}% | {result['item']['status']}")
    return 0


def gate_track(track_id: str, gate_name: str, result: str, detail: str) -> int:
    track = require_track(track_id)
    worktree_root = current_worktree_root()
    branch_name = current_branch(worktree_root)
    ensure_track_matches_worktree(track, worktree_root, branch_name)
    gate_result = progress_tracker.update_gate(track_id=track_id, gate_name=gate_name, result=result, detail=detail)
    print(f"{track_id}: {gate_result['gate']['gate_name']} -> {gate_result['gate']['status']}")
    return 0


def handoff_track(track_id: str, summary: str, next_step: str) -> int:
    track = require_track(track_id)
    worktree_root = current_worktree_root()
    branch_name = current_branch(worktree_root)
    ensure_track_matches_worktree(track, worktree_root, branch_name)
    run_context_sync_check(worktree_root)
    progress_tracker.add_progress_event(
        title=f"{track['title']} handoff",
        detail=f"{summary.strip()}\nNext: {next_step.strip()}",
        category="manual",
        status="INFO",
        track_id=track_id,
        branch_name=branch_name,
        event_type="handoff",
    )
    sync_plan_item(
        track_id,
        branch_name=branch_name,
        notes=next_step.strip(),
        latest_event_summary=summary.strip(),
    )
    print(f"Handoff recorded for {track_id}")
    return 0


def print_status() -> int:
    snapshot = progress_tracker.load_progress_snapshot(limit_rounds=20, limit_events=40)
    print(f"Project: {snapshot['tracker']['project_name']}")
    print(f"Runtime tracker: {snapshot['tracker']['runtime_tracker_path']}")
    print("Tracks:")
    for track in snapshot.get("tracks", []):
        print(
            f"- {track['id']}: {track['plan_status']} | progress={track['progress_pct']}% | drift={track['drift_status']} | next_gate={track.get('next_gate_name') or '-'}"
        )
    if snapshot.get("alerts"):
        print("Alerts:")
        for alert in snapshot["alerts"]:
            print(f"- {alert['severity']} {alert['track_id']}: {alert['title']}")
    return 0


def require_track(track_id: str) -> dict[str, str]:
    return progress_tracker._get_track(track_id)  # type: ignore[attr-defined]


def current_worktree_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path.cwd()),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def current_branch(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def ensure_track_matches_worktree(track: dict[str, str], worktree_root: Path, branch_name: str) -> None:
    expected_path = Path(str(track.get("worktree_path") or "")).resolve()
    expected_branch = str(track.get("branch_name") or "").strip()
    if expected_path != worktree_root:
        raise SystemExit(f"Track {track['id']} expects worktree {expected_path}, current is {worktree_root}.")
    if expected_branch and expected_branch != branch_name:
        raise SystemExit(f"Track {track['id']} expects branch {expected_branch}, current is {branch_name}.")


def run_context_sync_check(worktree_root: Path) -> None:
    if not CONTEXT_CHECK_SCRIPT.exists():
        return
    result = subprocess.run(
        [sys.executable, str(CONTEXT_CHECK_SCRIPT)],
        cwd=str(worktree_root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr or result.stdout or "Context sync check failed.")


def sync_plan_item(
    track_id: str,
    *,
    branch_name: str = "",
    status: str | None = None,
    notes: str | None = None,
    latest_event_summary: str | None = None,
) -> None:
    snapshot = progress_tracker.load_progress_snapshot(limit_rounds=5, limit_events=20)
    items = list(snapshot.get("plan", []))
    updated = False
    for item in items:
        if str(item.get("track_id") or "") != track_id:
            continue
        if branch_name:
            item["branch_name"] = branch_name
        if status:
            item["status"] = status
        if notes is not None:
            item["notes"] = notes
        if latest_event_summary is not None:
            item["latest_event_summary"] = latest_event_summary
        item["last_update_at"] = progress_tracker._now_iso()  # type: ignore[attr-defined]
        updated = True
        break
    if not updated:
        track = require_track(track_id)
        item = progress_tracker._track_to_plan_item(track)  # type: ignore[attr-defined]
        if branch_name:
            item["branch_name"] = branch_name
        if status:
            item["status"] = status
        if notes is not None:
            item["notes"] = notes
        if latest_event_summary is not None:
            item["latest_event_summary"] = latest_event_summary
        item["last_update_at"] = progress_tracker._now_iso()  # type: ignore[attr-defined]
        items.append(item)
    progress_tracker.replace_plan_items(items)


if __name__ == "__main__":
    raise SystemExit(main())
