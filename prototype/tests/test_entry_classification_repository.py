from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from app import db
from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
)
from app.application.extraction.entry_classification_repository import (
    EntryClassificationCommandConflict,
    EntryClassificationSnapshotCorrupted,
    EntryClassificationStaleWrite,
    _lock_entry_classification_lineage,
    append_entry_classification_snapshot,
    count_entry_classification_command_receipts,
    count_entry_classification_snapshots,
    get_entry_classification_command_result,
    get_entry_classification_snapshot_or_initial,
    record_entry_classification_command_receipt,
)
from app.domain.entry_classification import (
    EntryClassificationType,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    revise_entry_classification_snapshot,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)


class EntryClassificationRepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        db.init_db()

    def test_initial_projection_read_does_not_persist_a_revision_zero_row(self) -> None:
        initial = self._initial()
        statements: list[str] = []
        real_get_conn = db.get_conn

        class RecordingConnection:
            def __init__(self, connection) -> None:
                self.connection = connection

            def execute(self, sql, parameters=()):
                statements.append(str(sql).strip().upper())
                return self.connection.execute(sql, parameters)

        @contextmanager
        def recording_connection():
            with real_get_conn() as connection:
                yield RecordingConnection(connection)

        with patch(
            "app.application.extraction.entry_classification_repository.db.get_conn",
            recording_connection,
        ):
            current = get_entry_classification_snapshot_or_initial(initial)

        self.assertEqual(current, initial)
        self.assertEqual(count_entry_classification_snapshots(initial), 0)
        self.assertTrue(statements)
        self.assertFalse(
            any(
                statement.startswith(("CREATE", "ALTER", "INSERT", "UPDATE", "DELETE"))
                for statement in statements
            )
        )

    def test_append_replay_conflicts_and_exact_optimistic_head(self) -> None:
        initial = self._initial()
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-1",
            created_at="2026-08-07T02:00:00+00:00",
        )

        stored, created = append_entry_classification_snapshot(
            base_snapshot=initial,
            next_snapshot=working,
            command_id="command-1",
            command_sha256="1" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )
        replayed, replay_created = append_entry_classification_snapshot(
            base_snapshot=initial,
            next_snapshot=working,
            command_id="command-1",
            command_sha256="1" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(replayed, stored)
        self.assertEqual(
            get_entry_classification_snapshot_or_initial(initial),
            stored,
        )
        with self.assertRaises(EntryClassificationCommandConflict):
            append_entry_classification_snapshot(
                base_snapshot=initial,
                next_snapshot=working,
                command_id="command-1",
                command_sha256="2" * 64,
                expected_revision=0,
                expected_snapshot_sha256=initial.payload_sha256,
            )
        with self.assertRaises(EntryClassificationStaleWrite):
            append_entry_classification_snapshot(
                base_snapshot=initial,
                next_snapshot=working,
                command_id="command-stale-revision",
                command_sha256="3" * 64,
                expected_revision=0,
                expected_snapshot_sha256=initial.payload_sha256,
            )
        with self.assertRaises(EntryClassificationStaleWrite):
            append_entry_classification_snapshot(
                base_snapshot=stored,
                next_snapshot=accept_entry_classification_snapshot(
                    stored,
                    snapshot_id=f"{initial.snapshot_id}-accepted-stale",
                    accepted_at="2026-08-07T02:01:00+00:00",
                    accepted_by="engineer-1",
                ),
                command_id="command-stale-sha",
                command_sha256="4" * 64,
                expected_revision=stored.revision,
                expected_snapshot_sha256="f" * 64,
            )

    def test_command_result_lookup_precedes_state_transition_and_is_hash_safe(self) -> None:
        initial = self._initial()
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-command-lookup",
            created_at="2026-08-07T02:30:00+00:00",
        )
        stored, _ = append_entry_classification_snapshot(
            base_snapshot=initial,
            next_snapshot=working,
            command_id="command-lookup",
            command_sha256="c" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )

        self.assertEqual(
            get_entry_classification_command_result(
                initial,
                command_id="command-lookup",
                command_sha256="c" * 64,
            ),
            stored,
        )
        self.assertIsNone(
            get_entry_classification_command_result(
                initial,
                command_id="command-missing",
                command_sha256="d" * 64,
            )
        )
        with self.assertRaises(EntryClassificationCommandConflict):
            get_entry_classification_command_result(
                initial,
                command_id="command-lookup",
                command_sha256="e" * 64,
            )

    def test_noop_command_receipt_is_audited_without_business_revision(self) -> None:
        initial = self._initial()
        result, created = record_entry_classification_command_receipt(
            current_snapshot=initial,
            command_id="command-noop",
            command_sha256="1" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )
        replay, replay_created = record_entry_classification_command_receipt(
            current_snapshot=initial,
            command_id="command-noop",
            command_sha256="1" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )

        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(result, initial)
        self.assertEqual(replay, initial)
        self.assertEqual(count_entry_classification_snapshots(initial), 0)
        self.assertEqual(count_entry_classification_command_receipts(initial), 1)
        self.assertEqual(
            get_entry_classification_command_result(
                initial,
                command_id="command-noop",
                command_sha256="1" * 64,
            ),
            initial,
        )
        with self.assertRaises(EntryClassificationCommandConflict):
            get_entry_classification_command_result(
                initial,
                command_id="command-noop",
                command_sha256="2" * 64,
            )

        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-cross-command",
            created_at="2026-08-07T02:45:00+00:00",
        )
        replay_from_append, append_created = append_entry_classification_snapshot(
            base_snapshot=initial,
            next_snapshot=working,
            command_id="command-noop",
            command_sha256="1" * 64,
            expected_revision=0,
            expected_snapshot_sha256=initial.payload_sha256,
        )
        self.assertFalse(append_created)
        self.assertEqual(replay_from_append, initial)
        with self.assertRaises(EntryClassificationCommandConflict):
            append_entry_classification_snapshot(
                base_snapshot=initial,
                next_snapshot=working,
                command_id="command-noop",
                command_sha256="2" * 64,
                expected_revision=0,
                expected_snapshot_sha256=initial.payload_sha256,
            )

    def test_accept_then_edit_appends_working_and_lineages_are_isolated(self) -> None:
        initial = self._initial()
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.INFORMATION},
            snapshot_id=f"{initial.snapshot_id}-working-1",
            created_at="2026-08-07T03:00:00+00:00",
        )
        working, _ = self._append(initial, working, "command-working", "5")
        accepted = accept_entry_classification_snapshot(
            working,
            snapshot_id=f"{initial.snapshot_id}-accepted-2",
            accepted_at="2026-08-07T03:01:00+00:00",
            accepted_by="engineer-1",
        )
        accepted, _ = self._append(working, accepted, "command-accept", "6")
        reopened = revise_entry_classification_snapshot(
            accepted,
            overrides={"entry": EntryClassificationType.NON_TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-3",
            created_at="2026-08-07T03:02:00+00:00",
        )
        reopened, _ = self._append(accepted, reopened, "command-reopen", "7")

        other_lineage = self._initial(
            project_id=initial.project_id,
            document_id=initial.sor_document_id,
            source_sha256="9" * 64,
            effective_structure_id="effective-other",
        )
        self.assertEqual(reopened.revision, 3)
        self.assertEqual(reopened.status.value, "WORKING")
        self.assertEqual(count_entry_classification_snapshots(initial), 3)
        self.assertEqual(
            get_entry_classification_snapshot_or_initial(other_lineage),
            other_lineage,
        )
        self.assertEqual(count_entry_classification_snapshots(other_lineage), 0)

    def test_payload_hash_and_model_are_verified_on_every_read(self) -> None:
        initial = self._initial()
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-corruption",
            created_at="2026-08-07T04:00:00+00:00",
        )
        stored, _ = self._append(initial, working, "command-corruption", "8")
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT payload_json FROM entry_classification_snapshots WHERE id = ?",
                (stored.snapshot_id,),
            ).fetchone()
            conn.execute(
                "UPDATE entry_classification_snapshots SET payload_json = ? WHERE id = ?",
                (str(row["payload_json"]) + " ", stored.snapshot_id),
            )
        with self.assertRaises(EntryClassificationSnapshotCorrupted):
            get_entry_classification_snapshot_or_initial(initial)

        invalid_payload = json.loads(str(row["payload_json"]))
        invalid_payload["status"] = "BROKEN"
        invalid_json = json.dumps(
            invalid_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        invalid_sha = hashlib.sha256(invalid_json.encode("utf-8")).hexdigest()
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE entry_classification_snapshots
                SET payload_json = ?, payload_sha256 = ?
                WHERE id = ?
                """,
                (invalid_json, invalid_sha, stored.snapshot_id),
            )
        with self.assertRaises(EntryClassificationSnapshotCorrupted):
            get_entry_classification_snapshot_or_initial(initial)

    def test_persisted_command_identity_is_covered_by_the_envelope_hash(self) -> None:
        initial = self._initial()
        working = revise_entry_classification_snapshot(
            initial,
            overrides={"entry": EntryClassificationType.TECHNICAL},
            snapshot_id=f"{initial.snapshot_id}-working-envelope",
            created_at="2026-08-07T04:30:00+00:00",
        )
        stored, _ = self._append(initial, working, "command-envelope", "a")
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE entry_classification_snapshots
                SET command_id = ?
                WHERE id = ?
                """,
                ("command-tampered", stored.snapshot_id),
            )
        with self.assertRaises(EntryClassificationSnapshotCorrupted):
            get_entry_classification_snapshot_or_initial(initial)

        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE entry_classification_snapshots
                SET command_id = ?, command_sha256 = ?
                WHERE id = ?
                """,
                ("command-envelope", "b" * 64, stored.snapshot_id),
            )
        with self.assertRaises(EntryClassificationSnapshotCorrupted):
            get_entry_classification_snapshot_or_initial(initial)

    def test_cutover_rejects_entry_classification_payload_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_database = Path(temporary) / "source.db"
            with closing(sqlite3.connect(source_database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE entry_classification_snapshots (
                        id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        payload_sha256 TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO entry_classification_snapshots VALUES (?, ?, ?)",
                    ("snapshot-1", "{}", "f" * 64),
                )
                connection.commit()

            with self.assertRaises(CutoverRejected):
                build_cutover_plan(source_database)

    def test_cutover_rejects_command_receipt_envelope_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_database = Path(temporary) / "source.db"
            with closing(sqlite3.connect(source_database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE entry_classification_command_receipts (
                        id TEXT PRIMARY KEY,
                        envelope_sha256 TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO entry_classification_command_receipts VALUES (?, ?)",
                    ("receipt-1", "f" * 64),
                )
                connection.commit()

            with self.assertRaises(CutoverRejected):
                build_cutover_plan(source_database)

    def test_postgres_lock_migration_cutover_and_requirement_set_columns(self) -> None:
        class RecordingConnection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple[str, ...] | None]] = []

            def execute(self, sql, parameters=None):
                self.calls.append((str(sql), parameters))

        connection = RecordingConnection()
        initial = self._initial()
        with patch(
            "app.application.extraction.entry_classification_repository."
            "db.is_postgresql_runtime",
            return_value=True,
        ):
            _lock_entry_classification_lineage(connection, initial)
        self.assertEqual(len(connection.calls), 1)
        self.assertIn("pg_advisory_xact_lock", connection.calls[0][0])
        self.assertNotIn("BEGIN IMMEDIATE", connection.calls[0][0])
        sqlite_connection = RecordingConnection()
        with patch(
            "app.application.extraction.entry_classification_repository."
            "db.is_postgresql_runtime",
            return_value=False,
        ):
            _lock_entry_classification_lineage(sqlite_connection, initial)
        self.assertEqual(len(sqlite_connection.calls), 1)
        self.assertIn("BEGIN IMMEDIATE", sqlite_connection.calls[0][0])

        prototype_dir = Path(__file__).resolve().parents[1]
        migration = (
            prototype_dir
            / "migrations"
            / "postgresql"
            / "0013_entry_classification_snapshots.sql"
        ).read_text(encoding="utf-8")
        cutover = (
            prototype_dir
            / "app"
            / "application"
            / "cutover"
            / "sqlite_to_postgres.py"
        ).read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS entry_classification_snapshots", migration)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS entry_classification_command_receipts",
            migration,
        )
        self.assertIn("entry_classification_snapshot_sha256", migration)
        self.assertGreaterEqual(cutover.count('"entry_classification_snapshots"'), 3)
        self.assertGreaterEqual(
            cutover.count('"entry_classification_command_receipts"'),
            2,
        )
        with db.get_conn() as conn:
            snapshot_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(entry_classification_snapshots)"
                ).fetchall()
            }
            requirement_set_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(requirement_sets)").fetchall()
            }
        self.assertIn("payload_sha256", snapshot_columns)
        self.assertIn("entry_classification_snapshot_id", requirement_set_columns)
        self.assertIn("entry_classification_snapshot_sha256", requirement_set_columns)

    def _initial(
        self,
        *,
        project_id: str = "",
        document_id: str = "",
        source_sha256: str = "a" * 64,
        effective_structure_id: str = "effective-1",
    ):
        if not project_id or not document_id:
            project = db.create_project("Classification", "Customer")
            document = db.add_sor_document(
                project["id"],
                "source.pdf",
                "pdf",
                "uploads/source.pdf",
                "customer text excluded from classification payload",
                source_sha256=source_sha256,
                source_size=1,
                declared_extension="pdf",
                detected_format="pdf",
            )
            project_id = str(project["id"])
            document_id = str(document["id"])
        tree = RequirementTreeSnapshotV1(
            source_identity=RequirementTreeSourceIdentity(
                project_id=project_id,
                document_id=document_id,
                source_ir_id="source-ir-1",
                source_sha256=source_sha256,
            ),
            structure_identity=RequirementTreeStructureIdentity(
                base_snapshot_id="base-1",
                effective_snapshot_id=effective_structure_id,
                effective_tree_sha256="b" * 64,
                revision=1,
            ),
            mode=RequirementTreeMode.WORKING,
            state=RequirementTreeState.READY,
            root_node_ids=("entry",),
            nodes=(
                RequirementTreeNodeV1(
                    id="entry",
                    kind="CLAUSE",
                    depth=0,
                    source_order=0,
                    label="customer label",
                    summary="customer requirement text",
                    projection_role="REQUIREMENT",
                    include_in_view=True,
                ),
            ),
        )
        return build_initial_entry_classification_snapshot(tree)

    def _append(self, base, next_snapshot, command_id: str, digest_char: str):
        return append_entry_classification_snapshot(
            base_snapshot=base,
            next_snapshot=next_snapshot,
            command_id=command_id,
            command_sha256=digest_char * 64,
            expected_revision=base.revision,
            expected_snapshot_sha256=base.payload_sha256,
        )


if __name__ == "__main__":
    unittest.main()
