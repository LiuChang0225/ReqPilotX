from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
import multiprocessing
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

from app import db


SOURCE_SHA256 = "a" * 64
PUBLICATION_KEY = "b" * 64
OWNER_A = "publication-owner-" + "1" * 32
OWNER_B = "publication-owner-" + "2" * 32
HISTORICAL_RECOVERY_CONFIRMATION = (
    "RECOVER_HISTORICAL_EMPTY_PUBLICATION_LEASE"
)


def _hold_table_projection_revision_until_released(
    database_path: str,
    document_id: str,
    project_id: str,
    inserted_event,
    release_event,
) -> None:
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO table_projection_revisions (
                id, project_id, sor_document_id, source_sha256,
                structure_snapshot_id, logical_table_id, revision, action,
                projection_id, projection_sha256, projection_json,
                evidence_bindings_json, supersedes_id, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, 'KEEP_WHOLE', '', '', '', '[]', '', ?)
            """,
            (
                "revision-cross-process",
                project_id,
                document_id,
                SOURCE_SHA256,
                "structure-cross-process",
                "table-cross-process",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        inserted_event.set()
        if not release_event.wait(10):
            raise RuntimeError("cross-process revision release timed out")
        connection.commit()
    finally:
        connection.close()


class RequirementSetPublicationLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temporary.name)
        self.db_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.temporary.cleanup)
        db.init_db()
        self.project = db.create_project("Publication lease", "Customer")
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="source.pdf",
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256=SOURCE_SHA256,
        )
        self.now = datetime(2026, 8, 12, 8, 0, tzinfo=timezone.utc)

    def _begin(
        self,
        *,
        owner: str,
        now: datetime,
        publication_key: str = PUBLICATION_KEY,
    ) -> tuple[dict, bool]:
        return db.create_or_get_requirement_set_for_publication(
            self.project["id"],
            self.document["id"],
            publication_key=publication_key,
            source_hash=SOURCE_SHA256,
            publication_owner_token=owner,
            expected_publication_currentness_generation=(
                db.get_sor_document_publication_currentness_generation(
                    self.project["id"], self.document["id"]
                )
                or 0
            ),
            publication_lease_seconds=60,
            now_utc=now,
        )

    def _insert(self, requirement_set_id: str, *, owner: str, now: datetime) -> dict:
        return db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=requirement_set_id,
            requirement_id="requirement-" + "3" * 32,
            req_code="REQ0001",
            source_page=1,
            source_anchor="source-1",
            requirement_text="系统应记录结果。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
            publication_owner_token=owner,
            now_utc=now,
        )

    def test_active_lease_cannot_be_taken_over_and_heartbeat_extends_it(self) -> None:
        created, was_created = self._begin(owner=OWNER_A, now=self.now)

        replay, replay_created = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(seconds=30),
        )
        heartbeat = db.heartbeat_requirement_set_publication(
            self.project["id"],
            self.document["id"],
            created["id"],
            publication_owner_token=OWNER_A,
            publication_lease_seconds=60,
            now_utc=self.now + timedelta(seconds=40),
        )

        self.assertTrue(was_created)
        self.assertFalse(replay_created)
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(replay["publication_owner_token"], OWNER_A)
        self.assertEqual(heartbeat["publication_owner_token"], OWNER_A)
        self.assertEqual(
            heartbeat["publication_lease_expires_at"],
            (self.now + timedelta(seconds=100)).isoformat(),
        )
        still_active, _ = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(seconds=90),
        )
        self.assertEqual(still_active["publication_owner_token"], OWNER_A)

    def test_expired_owner_is_replaced_on_same_set_and_partial_rows_are_cleaned(
        self,
    ) -> None:
        created, _ = self._begin(owner=OWNER_A, now=self.now)
        self._insert(
            created["id"],
            owner=OWNER_A,
            now=self.now + timedelta(seconds=10),
        )

        takeover, takeover_created = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(seconds=61),
        )

        self.assertFalse(takeover_created)
        self.assertEqual(takeover["id"], created["id"])
        self.assertEqual(takeover["version"], created["version"])
        self.assertEqual(takeover["publication_key"], PUBLICATION_KEY)
        self.assertEqual(takeover["publication_owner_token"], OWNER_B)
        self.assertEqual(
            db.list_requirements(
                self.project["id"],
                requirement_set_id=created["id"],
            ),
            [],
        )
        with self.assertRaises(db.RequirementSetMutationBlocked):
            self._insert(
                created["id"],
                owner=OWNER_A,
                now=self.now + timedelta(seconds=62),
            )

    def test_stale_owner_cannot_finalize_or_discard_after_takeover(self) -> None:
        created, _ = self._begin(owner=OWNER_A, now=self.now)
        takeover, _ = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(seconds=61),
        )

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.attach_requirement_tree_snapshot(
                created["id"],
                object_key="requirement-sets/tree.json",
                tree_sha256="c" * 64,
                schema_version="requirement-tree/v1",
                structure_snapshot_id="structure-1",
                structure_tree_sha256="d" * 64,
                structure_revision=1,
                publication_owner_token=OWNER_A,
                expected_publication_currentness_generation=0,
                now_utc=self.now + timedelta(seconds=62),
            )
        self.assertFalse(
            db.discard_inactive_requirement_set(
                self.document["id"],
                created["id"],
                project_id=self.project["id"],
                expected_publication_state="PUBLISHING",
                publication_owner_token=OWNER_A,
                now_utc=self.now + timedelta(seconds=62),
            )
        )

        ready = db.attach_requirement_tree_snapshot(
            takeover["id"],
            object_key="requirement-sets/tree.json",
            tree_sha256="c" * 64,
            schema_version="requirement-tree/v1",
            structure_snapshot_id="structure-1",
            structure_tree_sha256="d" * 64,
            structure_revision=1,
            publication_owner_token=OWNER_B,
            expected_publication_currentness_generation=0,
            now_utc=self.now + timedelta(seconds=62),
        )
        self.assertEqual(ready["publication_state"], "READY")
        self.assertEqual(ready["publication_owner_token"], "")
        self.assertEqual(ready["publication_lease_expires_at"], "")
        self.assertEqual(ready["publication_heartbeat_at"], "")

    def test_cross_process_table_revision_race_cannot_finalize_stale_tree(
        self,
    ) -> None:
        created, _ = self._begin(owner=OWNER_A, now=self.now)
        context = multiprocessing.get_context("spawn")
        inserted = context.Event()
        release = context.Event()
        mutation = context.Process(
            target=_hold_table_projection_revision_until_released,
            args=(
                str(self.root / "app.db"),
                self.document["id"],
                self.project["id"],
                inserted,
                release,
            ),
        )
        mutation.start()
        self.assertTrue(inserted.wait(10))

        finalize_started = Event()
        outcome: list[BaseException | dict] = []

        def finalize() -> None:
            finalize_started.set()
            try:
                outcome.append(
                    db.attach_requirement_tree_snapshot(
                        created["id"],
                        object_key="requirement-sets/tree.json",
                        tree_sha256="c" * 64,
                        schema_version="requirement-tree/v1",
                        structure_snapshot_id="structure-1",
                        structure_tree_sha256="d" * 64,
                        structure_revision=1,
                        publication_owner_token=OWNER_A,
                        expected_publication_currentness_generation=0,
                        now_utc=self.now + timedelta(seconds=1),
                    )
                )
            except BaseException as exc:
                outcome.append(exc)

        finalizer = Thread(target=finalize)
        finalizer.start()
        self.assertTrue(finalize_started.wait(5))
        release.set()
        mutation.join(10)
        finalizer.join(10)

        self.assertEqual(mutation.exitcode, 0)
        self.assertFalse(finalizer.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(
            outcome[0],
            db.RequirementSetPublicationCurrentnessConflict,
        )
        retained = db.get_requirement_set(created["id"]) or {}
        self.assertEqual(retained["publication_state"], "PUBLISHING")
        self.assertEqual(
            db.get_sor_document_publication_currentness_generation(
                self.project["id"], self.document["id"]
            ),
            1,
        )

    def test_active_owner_can_discard_but_expired_owner_cannot(self) -> None:
        active, _ = self._begin(owner=OWNER_A, now=self.now)
        self.assertTrue(
            db.discard_inactive_requirement_set(
                self.document["id"],
                active["id"],
                project_id=self.project["id"],
                expected_publication_state="PUBLISHING",
                publication_owner_token=OWNER_A,
                now_utc=self.now + timedelta(seconds=30),
            )
        )

        expired, _ = self._begin(
            owner=OWNER_A,
            now=self.now,
            publication_key="e" * 64,
        )
        self.assertFalse(
            db.discard_inactive_requirement_set(
                self.document["id"],
                expired["id"],
                project_id=self.project["id"],
                expected_publication_state="PUBLISHING",
                publication_owner_token=OWNER_A,
                now_utc=self.now + timedelta(seconds=61),
            )
        )
        self.assertIsNotNone(db.get_requirement_set(expired["id"]))

    def test_historical_publishing_row_with_empty_lease_fails_closed(self) -> None:
        created, _ = self._begin(owner=OWNER_A, now=self.now)
        self._insert(
            created["id"],
            owner=OWNER_A,
            now=self.now + timedelta(seconds=10),
        )
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET publication_owner_token = '',
                    publication_lease_expires_at = '',
                    publication_heartbeat_at = ''
                WHERE id = ?
                """,
                (created["id"],),
            )

        replay, replay_created = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(days=365),
        )

        self.assertFalse(replay_created)
        self.assertEqual(replay["id"], created["id"])
        self.assertEqual(replay["publication_owner_token"], "")
        self.assertEqual(
            len(
                db.list_requirements(
                    self.project["id"],
                    requirement_set_id=created["id"],
                )
            ),
            1,
        )
        self.assertFalse(
            db.discard_inactive_requirement_set(
                self.document["id"],
                created["id"],
                project_id=self.project["id"],
                expected_publication_state="PUBLISHING",
                publication_owner_token=OWNER_B,
                now_utc=self.now + timedelta(days=365),
            )
        )

        with self.assertRaises(ValueError):
            db.recover_historical_requirement_set_publication(
                self.project["id"],
                self.document["id"],
                created["id"],
                publication_key=PUBLICATION_KEY,
                operator_confirmation="",
                now_utc=self.now + timedelta(days=365),
            )
        recovered = db.recover_historical_requirement_set_publication(
            self.project["id"],
            self.document["id"],
            created["id"],
            publication_key=PUBLICATION_KEY,
            operator_confirmation=HISTORICAL_RECOVERY_CONFIRMATION,
            now_utc=self.now + timedelta(days=365),
        )
        self.assertEqual(recovered["id"], created["id"])
        self.assertTrue(recovered["publication_owner_token"])
        self.assertEqual(
            recovered["publication_lease_expires_at"],
            recovered["publication_heartbeat_at"],
        )
        # Explicit recovery does not itself infer ownership or delete data.
        self.assertEqual(
            len(
                db.list_requirements(
                    self.project["id"],
                    requirement_set_id=created["id"],
                )
            ),
            1,
        )
        takeover, takeover_created = self._begin(
            owner=OWNER_B,
            now=self.now + timedelta(days=365),
        )
        self.assertFalse(takeover_created)
        self.assertEqual(takeover["id"], created["id"])
        self.assertEqual(takeover["publication_owner_token"], OWNER_B)
        self.assertEqual(
            db.list_requirements(
                self.project["id"],
                requirement_set_id=created["id"],
            ),
            [],
        )

    def test_explicit_historical_recovery_rejects_leased_and_completed_rows(
        self,
    ) -> None:
        leased, _ = self._begin(owner=OWNER_A, now=self.now)
        self.assertEqual(
            db.recover_historical_requirement_set_publication(
                self.project["id"],
                self.document["id"],
                leased["id"],
                publication_key=PUBLICATION_KEY,
                operator_confirmation=HISTORICAL_RECOVERY_CONFIRMATION,
                now_utc=self.now + timedelta(days=365),
            ),
            {},
        )
        ready = db.attach_requirement_tree_snapshot(
            leased["id"],
            object_key="requirement-sets/tree.json",
            tree_sha256="c" * 64,
            schema_version="requirement-tree/v1",
            structure_snapshot_id="structure-1",
            structure_tree_sha256="d" * 64,
            structure_revision=1,
            publication_owner_token=OWNER_A,
            expected_publication_currentness_generation=0,
            now_utc=self.now + timedelta(seconds=1),
        )
        self.assertEqual(ready["publication_state"], "READY")
        self.assertEqual(
            db.recover_historical_requirement_set_publication(
                self.project["id"],
                self.document["id"],
                leased["id"],
                publication_key=PUBLICATION_KEY,
                operator_confirmation=HISTORICAL_RECOVERY_CONFIRMATION,
                now_utc=self.now + timedelta(days=365),
            ),
            {},
        )

    def test_unkeyed_enrichment_publishing_draft_keeps_legacy_cleanup(self) -> None:
        draft = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=SOURCE_SHA256,
        )
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE requirement_sets SET publication_state = 'PUBLISHING' "
                "WHERE id = ?",
                (draft["id"],),
            )

        self.assertTrue(
            db.discard_inactive_requirement_set(
                self.document["id"],
                draft["id"],
                project_id=self.project["id"],
            )
        )

    def test_idempotent_existing_tree_finalization_clears_lease(self) -> None:
        created, _ = self._begin(owner=OWNER_A, now=self.now)
        with db.get_conn() as conn:
            conn.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_object_key = ?,
                    requirement_tree_sha256 = ?,
                    requirement_tree_schema_version = ?,
                    structure_snapshot_id = ?,
                    structure_tree_sha256 = ?,
                    structure_revision = ?
                WHERE id = ?
                """,
                (
                    "requirement-sets/tree.json",
                    "c" * 64,
                    "requirement-tree/v1",
                    "structure-1",
                    "d" * 64,
                    1,
                    created["id"],
                ),
            )

        ready = db.attach_requirement_tree_snapshot(
            created["id"],
            object_key="requirement-sets/tree.json",
            tree_sha256="c" * 64,
            schema_version="requirement-tree/v1",
            structure_snapshot_id="structure-1",
            structure_tree_sha256="d" * 64,
            structure_revision=1,
            publication_owner_token=OWNER_A,
            expected_publication_currentness_generation=0,
            now_utc=self.now + timedelta(seconds=1),
        )

        self.assertEqual(ready["publication_state"], "READY")
        self.assertEqual(ready["publication_owner_token"], "")
        self.assertEqual(ready["publication_lease_expires_at"], "")
        self.assertEqual(ready["publication_heartbeat_at"], "")

    def test_postgresql_migration_does_not_backfill_historical_leases(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0020_requirement_set_publication_leases.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("publication_owner_token", migration)
        self.assertIn("publication_lease_expires_at", migration)
        self.assertIn("publication_heartbeat_at", migration)
        self.assertNotIn("UPDATE requirement_sets", migration)

        currentness_migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0021_publication_currentness_generation.sql"
        ).read_text(encoding="utf-8")
        self.assertIn("publication_currentness_generation", currentness_migration)
        self.assertIn(
            "trg_table_projection_publication_currentness",
            currentness_migration,
        )
        self.assertIn(
            "trg_extraction_result_publication_currentness",
            currentness_migration,
        )


if __name__ == "__main__":
    unittest.main()
