from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from app.adapters.agents.codex_app_server.native_loop_history import (
    adopt_sdk_history_reconstruction,
    _connection,
    verify_sdk_history_reconstruction,
    reconcile_native_loop_history,
    recover_completed_native_result,
)
from app.adapters.agents.codex_app_server import native_loop_history as history


class NativeHistoryTests(unittest.TestCase):
    def test_reconcile_never_touches_live_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run-state.json").write_text(
                json.dumps({"pid": 123, "status": "RUNNING"})
            )
            with patch.object(history, "rebuild_sdk_history_copy") as rebuild:
                with self.assertRaisesRegex(ValueError, "stopped"):
                    reconcile_native_loop_history(
                        driver=SimpleNamespace(), session_dir=root, apply=True
                    )
                rebuild.assert_not_called()

    def test_adoption_receipt_failure_after_sql_updates_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, rebuilt, _ = self.fixture(root)
            (root / "sqlite").mkdir()
            (root / "run-state.json").write_text(
                json.dumps(
                    {"thread_id": "thread", "turn_id": "turn", "status": "UNKNOWN"}
                )
            )
            with _connection(root / "sqlite/state_5.sqlite") as db:
                db.execute(
                    "CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT)"
                )
                db.execute("INSERT INTO threads VALUES(?,?)", ("thread", str(original)))
            for name, status in (
                ("sqlite/thread_history_1.sqlite", "inProgress"),
                ("rebuilt.sqlite", "completed"),
            ):
                with _connection(root / name) as db:
                    for table in (
                        "thread_turns",
                        "thread_items",
                        "thread_history_projection_state",
                        "thread_realtime_items",
                    ):
                        db.execute(
                            f"CREATE TABLE {table}(thread_id TEXT,turn_id TEXT,status TEXT)"
                        )
                        db.execute(
                            f"INSERT INTO {table} VALUES('thread','turn',?)", (status,)
                        )
            real_write = history._write_json

            def fail_receipt(path, value):
                if path.name == "adoption-receipt.json":
                    raise OSError("synthetic receipt write failure after SQL updates")
                return real_write(path, value)

            with patch.object(history, "_write_json", side_effect=fail_receipt):
                with self.assertRaises(OSError):
                    adopt_sdk_history_reconstruction(
                        session_dir=root,
                        rebuilt_rollout=rebuilt,
                        rebuilt_sqlite=root / "rebuilt.sqlite",
                    )
            with _connection(root / "sqlite/state_5.sqlite") as db:
                self.assertEqual(
                    db.execute("SELECT rollout_path FROM threads").fetchone()[0],
                    str(original),
                )
            with _connection(root / "sqlite/thread_history_1.sqlite") as db:
                self.assertEqual(
                    db.execute("SELECT status FROM thread_turns").fetchone()[0],
                    "inProgress",
                )
            with self.assertRaisesRegex(ValueError, "inspect before retry"):
                adopt_sdk_history_reconstruction(
                    session_dir=root,
                    rebuilt_rollout=rebuilt,
                    rebuilt_sqlite=root / "rebuilt.sqlite",
                )

    def test_only_actual_completed_event_plus_sdk_verification_can_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = {
                "attempt_id": "attempt",
                "thread_id": "thread",
                "turn_id": "turn",
                "identity": {"tools": "actual"},
                "status": "UNKNOWN",
                "reason_code": "NATIVE_LOOP_TERMINAL_SNAPSHOT_MISMATCH",
                "tools": [],
            }
            state = {
                "status": "COMPLETED",
                "thread_id": "thread",
                "turn_id": "turn",
                "identity": original["identity"],
                "last_receipt": original,
                "terminal_observation": {
                    "verified_terminal": True,
                    "observed_status": "completed",
                    "thread_id": "thread",
                    "turn_id": "turn",
                },
            }
            (root / "run-state.json").write_text(json.dumps(state))
            receipt = root / "receipt-attempt.json"
            receipt.write_text(json.dumps(original))
            old_bytes = receipt.read_bytes()
            event = {
                "attempt_id": "attempt",
                "thread_id": "thread",
                "turn_id": "turn",
                "type": "turn/completed",
                "status": "completed",
            }
            (root / "events.jsonl").write_text(json.dumps(event) + "\n")
            result = recover_completed_native_result(root)
            self.assertEqual(result.status, "COMPLETED")
            self.assertFalse(
                result.receipt["host_terminal_recovery"]["new_model_turn_started"]
            )
            self.assertEqual(receipt.read_bytes(), old_bytes)
            event["status"] = "interrupted"
            (root / "events.jsonl").write_text(json.dumps(event) + "\n")
            with self.assertRaisesRegex(ValueError, "actually complete"):
                recover_completed_native_result(root)

    def fixture(self, root):
        old = [
            {"type": "session_meta", "ordinal": 0, "payload": {"id": "thread"}},
            {
                "type": "compacted",
                "ordinal": 0,
                "payload": {"guardian_history": [{"type": "reasoning", "summary": []}]},
            },
            {
                "type": "event_msg",
                "ordinal": 1,
                "payload": {"type": "task_complete", "turn_id": "turn"},
            },
        ]
        new = json.loads(json.dumps(old))
        for i, row in enumerate(new):
            row["ordinal"] = i
        new[1]["payload"]["guardian_history"][0]["content"] = None
        original, rebuilt = root / "original.jsonl", root / "rebuilt.jsonl"
        for p, rows in ((original, old), (rebuilt, new)):
            p.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return original, rebuilt, new

    def test_sdk_optional_nulls_and_ordinals_preserve_payloads_but_changed_text_fails(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, rebuilt, rows = self.fixture(root)
            proof = verify_sdk_history_reconstruction(original, rebuilt, "thread")
            self.assertTrue(proof["same_order_and_payloads"])
            self.assertEqual(proof["sdk_normalizations"]["ordinal_reindexed"], 2)
            rows[-1]["payload"]["type"] = "task_started"
            rebuilt.write_text("".join(json.dumps(row) + "\n" for row in rows))
            with self.assertRaisesRegex(ValueError, "changed payload"):
                verify_sdk_history_reconstruction(original, rebuilt, "thread")

    def test_adoption_imports_only_sdk_target_rows_and_preserves_original_and_other_thread(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, rebuilt, _ = self.fixture(root)
            before = original.read_bytes()
            (root / "sqlite").mkdir()
            (root / "run-state.json").write_text(
                json.dumps(
                    {
                        "thread_id": "thread",
                        "turn_id": "turn",
                        "status": "UNKNOWN",
                        "pid": None,
                        "app_server_pid": None,
                    }
                )
            )
            state_db = root / "sqlite/state_5.sqlite"
            with _connection(state_db) as db:
                db.execute(
                    "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT)"
                )
                db.executemany(
                    "INSERT INTO threads VALUES (?,?)",
                    [("thread", str(original)), ("other", "untouched")],
                )
            source_db, target_db = (
                root / "rebuilt.sqlite",
                root / "sqlite/thread_history_1.sqlite",
            )
            for path, status in ((source_db, "completed"), (target_db, "inProgress")):
                with _connection(path) as db:
                    for table in (
                        "thread_turns",
                        "thread_items",
                        "thread_history_projection_state",
                        "thread_realtime_items",
                    ):
                        db.execute(
                            f"CREATE TABLE {table} (thread_id TEXT, turn_id TEXT, status TEXT)"
                        )
                        db.executemany(
                            f"INSERT INTO {table} VALUES (?,?,?)",
                            [
                                ("thread", "turn", status),
                                ("other", "other-turn", "untouched"),
                            ],
                        )
            receipt = adopt_sdk_history_reconstruction(
                session_dir=root, rebuilt_rollout=rebuilt, rebuilt_sqlite=source_db
            )
            self.assertEqual(original.read_bytes(), before)
            self.assertTrue(
                (Path(receipt["backup_directory"]) / "history-before.sqlite").exists()
            )
            with _connection(target_db) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT status FROM thread_turns WHERE thread_id='thread'"
                    ).fetchone()[0],
                    "completed",
                )
                self.assertEqual(
                    db.execute(
                        "SELECT status FROM thread_turns WHERE thread_id='other'"
                    ).fetchone()[0],
                    "untouched",
                )
            with _connection(state_db) as db:
                self.assertEqual(
                    db.execute(
                        "SELECT rollout_path FROM threads WHERE id='thread'"
                    ).fetchone()[0],
                    receipt["active_rollout_path"],
                )
                self.assertEqual(
                    db.execute(
                        "SELECT rollout_path FROM threads WHERE id='other'"
                    ).fetchone()[0],
                    "untouched",
                )
