from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import db, main
from app.services import progress_tracker
from scripts import trackctl


def _registry_payload(base_dir: Path) -> dict:
    return {
        "updated_at": "2026-04-09T18:00:00",
        "tracks": [
            {
                "id": "release-foundation",
                "title": "Release Foundation",
                "lane_order": 1,
                "status": "ACTIVE",
                "branch_name": "codex/release-foundation",
                "worktree_path": str(base_dir / "worktrees" / "release-foundation"),
                "skill_name": "reqpilot-track-release-foundation",
                "branch_context_doc": "prototype/docs/release-foundation-active-context.md",
                "scope_summary": "Foundation scope",
                "non_goals": "No parser tuning",
                "allowed_paths": ["prototype/app/main.py", "prototype/tests/*"],
                "blocked_paths": ["prototype/app/services/parser.py"],
                "required_docs": [
                    "AGENTS.md",
                    "docs/项目.md",
                    "docs/开发执行清单.md",
                    "docs/active-context.md",
                    "prototype/eval/progress/track_registry.json",
                    "prototype/docs/release-foundation-active-context.md",
                ],
                "required_gates": ["p0-smoke", "auth-login"],
                "owner_role": "Ops",
                "target_date": "",
            },
            {
                "id": "rag-quality-loop",
                "title": "RAG Quality Loop",
                "lane_order": 2,
                "status": "ACTIVE",
                "branch_name": "codex/rag-quality-loop",
                "worktree_path": str(base_dir / "worktrees" / "rag-quality-loop"),
                "skill_name": "reqpilot-track-rag-quality",
                "branch_context_doc": "prototype/docs/rag-quality-active-context.md",
                "scope_summary": "RAG scope",
                "non_goals": "No auth work",
                "allowed_paths": ["prototype/app/services/rag.py", "prototype/tests/*"],
                "blocked_paths": ["prototype/app/main.py"],
                "required_docs": [
                    "AGENTS.md",
                    "docs/项目.md",
                    "docs/开发执行清单.md",
                    "docs/active-context.md",
                    "prototype/eval/progress/track_registry.json",
                    "prototype/docs/rag-quality-active-context.md",
                ],
                "required_gates": ["sample10"],
                "owner_role": "AI",
                "target_date": "",
            },
        ],
    }


class ProgressControlTowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmpdir.name)
        self.runtime_dir = self.tmp_path / "runtime"
        self.progress_dir = self.tmp_path / "prototype" / "eval" / "progress"
        self.iterations_dir = self.tmp_path / "prototype" / "eval" / "iterations"
        self.regression_dir = self.tmp_path / "prototype" / "eval" / "regression_suite"
        self.progress_dir.mkdir(parents=True, exist_ok=True)
        self.iterations_dir.mkdir(parents=True, exist_ok=True)
        self.regression_dir.mkdir(parents=True, exist_ok=True)
        registry_path = self.progress_dir / "track_registry.json"
        registry_path.write_text(json.dumps(_registry_payload(self.tmp_path), ensure_ascii=False, indent=2), encoding="utf-8")
        bootstrap_path = self.progress_dir / "progress_tracker.json"
        bootstrap_path.write_text(
            json.dumps(
                {
                    "project_name": "ReqPilotX Project Control Tower",
                    "north_star": "Test control tower",
                    "updated_at": "2026-04-09T18:00:00",
                    "plan": [],
                    "events": [],
                    "gates": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.tmp_path / "storage"))
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.tmp_path / "storage" / "app.db"))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.tmp_path / "storage"))
        self.exit_stack.enter_context(patch.object(main, "UPLOADS_DIR", self.tmp_path / "storage" / "uploads"))
        self.exit_stack.enter_context(patch.object(main, "KNOWLEDGE_DIR", self.tmp_path / "storage" / "knowledge"))
        self.exit_stack.enter_context(patch.object(main, "EXPORTS_DIR", self.tmp_path / "storage" / "exports"))
        self.exit_stack.enter_context(patch.object(main, "TABLE_SNAPSHOT_DIR", self.tmp_path / "storage" / "table_snapshots"))
        self.exit_stack.enter_context(patch.dict(os.environ, {progress_tracker.RUNTIME_DIR_ENV: str(self.runtime_dir)}, clear=False))
        self.exit_stack.enter_context(patch.object(progress_tracker, "PROGRESS_DIR", self.progress_dir))
        self.exit_stack.enter_context(patch.object(progress_tracker, "TRACK_REGISTRY_PATH", registry_path))
        self.exit_stack.enter_context(patch.object(progress_tracker, "TRACKER_BOOTSTRAP_PATH", bootstrap_path))
        self.exit_stack.enter_context(patch.object(progress_tracker, "ITERATIONS_DIR", self.iterations_dir))
        self.exit_stack.enter_context(patch.object(progress_tracker, "BOARD_CSV", self.iterations_dir / "phase0_iteration_board.csv"))
        self.exit_stack.enter_context(patch.object(progress_tracker, "REGRESSION_DIR", self.regression_dir))
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        db_file = self.tmp_path / "storage" / "app.db"
        for _ in range(5):
            try:
                for suffix in ("-wal", "-shm"):
                    sidecar = Path(f"{db_file}{suffix}")
                    if sidecar.exists():
                        sidecar.unlink()
                self.tmpdir.cleanup()
                return
            except OSError:
                time.sleep(0.05)
        try:
            self.tmpdir.cleanup()
        except OSError:
            pass

    def test_bootstrap_control_tower_seeds_track_plan(self) -> None:
        result = progress_tracker.bootstrap_control_tower(force=True)
        snapshot = progress_tracker.load_progress_snapshot(limit_rounds=5, limit_events=20)

        self.assertTrue(result["ok"])
        self.assertEqual(result["plan_count"], 2)
        self.assertEqual(len(snapshot["tracks"]), 2)
        self.assertEqual(snapshot["tracks"][0]["id"], "release-foundation")
        self.assertEqual(snapshot["plan"][0]["track_id"], "release-foundation")

    def test_checkpoint_and_gate_update_are_visible_in_snapshot(self) -> None:
        progress_tracker.bootstrap_control_tower(force=True)
        progress_tracker.update_progress_checkpoint(
            track_id="release-foundation",
            progress_pct=35,
            status="INFO",
            summary="Foundation bootstrap completed.",
            branch_name="codex/release-foundation",
        )
        progress_tracker.update_gate(
            track_id="release-foundation",
            gate_name="p0-smoke",
            result="PASS",
            detail="Smoke remains green.",
        )

        snapshot = progress_tracker.load_progress_snapshot(limit_rounds=5, limit_events=20)
        release_track = next(item for item in snapshot["tracks"] if item["id"] == "release-foundation")
        smoke_gate = next(item for item in snapshot["gates"] if item["track_id"] == "release-foundation")

        self.assertEqual(release_track["progress_pct"], 35)
        self.assertEqual(release_track["passed_gate_count"], 1)
        self.assertEqual(smoke_gate["status"], "PASS")

    def test_progress_api_exposes_branches_gates_and_checkpoint(self) -> None:
        progress_tracker.bootstrap_control_tower(force=True)
        (self.tmp_path / "worktrees" / "release-foundation").mkdir(parents=True, exist_ok=True)
        (self.tmp_path / "worktrees" / "rag-quality-loop").mkdir(parents=True, exist_ok=True)

        def fake_git(args: list[str], cwd: Path) -> str:
            cwd_name = Path(cwd).name
            if args[:2] == ["branch", "--show-current"]:
                return "codex/release-foundation" if cwd_name == "release-foundation" else "codex/rag-quality-loop"
            if args[:2] == ["status", "--short"]:
                return " M prototype/app/main.py" if cwd_name == "release-foundation" else ""
            if args[:2] == ["log", "-1"]:
                return "abc12345\x1f2026-04-09T18:10:00"
            if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
                return "origin/main"
            if args[:3] == ["rev-list", "--left-right", "--count"]:
                return "0 1"
            return ""

        with patch("app.services.progress_tracker._run_git", side_effect=fake_git):
            with TestClient(main.app) as client:
                checkpoint = client.post(
                    "/api/progress/checkpoint",
                    json={
                        "track_id": "release-foundation",
                        "progress_pct": 45,
                        "status": "INFO",
                        "summary": "Checkpoint from API",
                        "branch_name": "codex/release-foundation",
                    },
                )
                branches = client.get("/api/progress/branches")
                gates = client.get("/api/progress/gates")

        self.assertEqual(checkpoint.status_code, 200)
        self.assertEqual(branches.status_code, 200)
        self.assertEqual(gates.status_code, 200)
        branch_rows = branches.json()["branches"]
        self.assertEqual(branch_rows[0]["branch_name"], "codex/release-foundation")
        self.assertEqual(branch_rows[0]["drift_status"], "ON_PLAN")
        gate_rows = gates.json()["gates"]
        self.assertEqual(gate_rows[0]["track_id"], "release-foundation")


class TrackCtlTests(unittest.TestCase):
    def test_start_track_records_kickoff_and_syncs_plan(self) -> None:
        with patch.object(trackctl.progress_tracker, "bootstrap_control_tower", return_value={"ok": True}):
            with patch.object(trackctl, "require_track", return_value={"id": "release-foundation", "title": "Release Foundation", "branch_name": "codex/release-foundation", "worktree_path": "/tmp/release-foundation"}):
                with patch.object(trackctl, "current_worktree_root", return_value=Path("/tmp/release-foundation")):
                    with patch.object(trackctl, "current_branch", return_value="codex/release-foundation"):
                        with patch.object(trackctl, "ensure_track_matches_worktree") as ensure_mock:
                            with patch.object(trackctl.progress_tracker, "add_progress_event", return_value={"detail": "kickoff"}) as add_event:
                                with patch.object(trackctl, "sync_plan_item") as sync_mock:
                                    result = trackctl.start_track("release-foundation")

        self.assertEqual(result, 0)
        ensure_mock.assert_called_once()
        add_event.assert_called_once()
        sync_mock.assert_called_once()

    def test_handoff_track_runs_context_check_and_updates_plan_notes(self) -> None:
        with patch.object(trackctl, "require_track", return_value={"id": "rag-quality-loop", "title": "RAG Quality Loop", "branch_name": "codex/rag-quality-loop", "worktree_path": "/tmp/rag-quality-loop"}):
            with patch.object(trackctl, "current_worktree_root", return_value=Path("/tmp/rag-quality-loop")):
                with patch.object(trackctl, "current_branch", return_value="codex/rag-quality-loop"):
                    with patch.object(trackctl, "ensure_track_matches_worktree") as ensure_mock:
                        with patch.object(trackctl, "run_context_sync_check") as context_mock:
                            with patch.object(trackctl.progress_tracker, "add_progress_event") as add_event:
                                with patch.object(trackctl, "sync_plan_item") as sync_mock:
                                    result = trackctl.handoff_track("rag-quality-loop", "RAG summary", "Tune next batch")

        self.assertEqual(result, 0)
        ensure_mock.assert_called_once()
        context_mock.assert_called_once()
        add_event.assert_called_once()
        sync_mock.assert_called_once()
