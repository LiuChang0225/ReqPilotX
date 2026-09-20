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
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app import db
from app.application.assessment_scope import assessment_command_sha256
from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
)


_SCOPE_SCHEMA_VERSION = "requirement-analysis-scope/v1"
_TREE_SHA256 = "a" * 64
_COMMAND_SHA256 = "b" * 64


def _scope_payload(
    *,
    requirement_set_id: str,
    scope: str = "CURRENT",
) -> tuple[str, str]:
    payload = {
        "schema_version": _SCOPE_SCHEMA_VERSION,
        "requirement_set_id": requirement_set_id,
        "requirement_set_version": 1,
        "tree_sha256": _TREE_SHA256,
        "scope": scope,
        "requested_node_ids": ["node-1"],
        "target_pairs": [{"node_id": "node-1", "requirement_id": "requirement-1"}],
        "analysis_universe_pairs": [
            {"node_id": "node-1", "requirement_id": "requirement-1"}
        ],
        "skipped": [],
        "target_examples": [],
        "skipped_examples": [],
        "publication_mode": "ATOMIC_BATCH",
        "skipped_reason_counts": {},
    }
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


class AiAnalysisJobScopeSqliteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-assessment-scope-db-"
        )
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_scope_identity_and_published_count_round_trip(self) -> None:
        project = db.create_project("Assessment Scope", "Customer")
        source = self.root / "source.txt"
        source.write_text("requirement", encoding="utf-8")
        document = db.add_sor_document(
            project["id"],
            source.name,
            "txt",
            str(source),
            "requirement",
        )
        requirement = db.insert_requirement(
            project["id"],
            document["id"],
            "REQ0001",
            1,
            "Page 1",
            "The product shall retain an audit record.",
            "FUNCTIONAL",
            "HIGH",
        )
        requirement_set_id = str(requirement["requirement_set_id"])
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_sets SET requirement_tree_sha256 = ? WHERE id = ?",
                (_TREE_SHA256, requirement_set_id),
            )
        payload_json, scope_sha256 = _scope_payload(
            requirement_set_id=requirement_set_id
        )

        job = db.create_ai_analysis_job(
            project["id"],
            document["id"],
            requirement_set_id=requirement_set_id,
            knowledge_base_version=2,
            command_id="analysis-command-1",
            command_sha256=_COMMAND_SHA256,
            scope_schema_version=_SCOPE_SCHEMA_VERSION,
            scope_kind="CURRENT",
            scope_payload_json=payload_json,
            scope_sha256=scope_sha256,
            total_requirements=1,
            status="COMPLETED",
        )
        updated = db.update_ai_analysis_job(job["id"], published_requirements=1)
        db.init_db()
        persisted = db.get_ai_analysis_job(job["id"])

        self.assertEqual(updated["published_requirements"], 1)
        self.assertEqual(persisted["requirement_set_version"], 1)
        self.assertEqual(persisted["requirement_tree_sha256"], _TREE_SHA256)
        self.assertEqual(persisted["command_id"], "analysis-command-1")
        self.assertEqual(persisted["command_sha256"], _COMMAND_SHA256)
        self.assertEqual(persisted["scope_schema_version"], _SCOPE_SCHEMA_VERSION)
        self.assertEqual(persisted["scope_kind"], "CURRENT")
        self.assertEqual(persisted["scope_payload_json"], payload_json)
        self.assertEqual(persisted["scope_sha256"], scope_sha256)
        self.assertEqual(persisted["publication_mode"], "ATOMIC_BATCH")
        self.assertEqual(persisted["published_requirements"], 1)

        with self.assertRaises(sqlite3.IntegrityError):
            db.create_ai_analysis_job(
                project["id"],
                document["id"],
                requirement_set_id=requirement_set_id,
                command_id="analysis-command-1",
                command_sha256=_COMMAND_SHA256,
                scope_schema_version=_SCOPE_SCHEMA_VERSION,
                scope_kind="CURRENT",
                scope_payload_json=payload_json,
                scope_sha256=scope_sha256,
                total_requirements=1,
                status="FAILED",
            )

    def test_legacy_backfill_recovers_only_safe_lineage_and_publication(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            f"""
            CREATE TABLE requirement_sets(
                id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                requirement_tree_sha256 TEXT NOT NULL
            );
            CREATE TABLE ai_analysis_jobs(
                id TEXT PRIMARY KEY,
                sor_document_id TEXT NOT NULL,
                requirement_set_id TEXT NOT NULL,
                requirement_set_version INTEGER NOT NULL DEFAULT 0,
                requirement_tree_sha256 TEXT NOT NULL DEFAULT '',
                command_id TEXT NOT NULL DEFAULT '',
                command_sha256 TEXT NOT NULL DEFAULT '',
                scope_schema_version TEXT NOT NULL DEFAULT '',
                scope_kind TEXT NOT NULL DEFAULT '',
                scope_payload_json TEXT NOT NULL DEFAULT '{{}}',
                scope_sha256 TEXT NOT NULL DEFAULT '',
                publication_mode TEXT NOT NULL DEFAULT 'ATOMIC_BATCH',
                total_requirements INTEGER NOT NULL DEFAULT 0,
                published_requirements INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL
            );
            INSERT INTO requirement_sets VALUES ('set-legacy', 3, '{_TREE_SHA256}');
            INSERT INTO ai_analysis_jobs(
                id, sor_document_id, requirement_set_id,
                total_requirements, status
            ) VALUES ('job-legacy', 'document-1', 'set-legacy', 4, 'COMPLETED');
            """
        )

        db._ensure_ai_analysis_job_scope_schema(
            connection,
            backfill_set_version=True,
            backfill_tree_sha256=True,
            backfill_published_count=True,
        )
        row = connection.execute(
            "SELECT * FROM ai_analysis_jobs WHERE id = 'job-legacy'"
        ).fetchone()
        connection.close()

        self.assertEqual(row["requirement_set_version"], 3)
        self.assertEqual(row["requirement_tree_sha256"], _TREE_SHA256)
        self.assertEqual(row["published_requirements"], 4)
        self.assertEqual(row["command_id"], "")
        self.assertEqual(row["scope_sha256"], "")
        self.assertEqual(row["scope_payload_json"], "{}")


class AiAnalysisJobScopeCutoverTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-assessment-scope-cutover-"
        )
        self.database = Path(self.temporary.name) / "source.db"
        self.payload_json, self.scope_sha256 = _scope_payload(
            requirement_set_id="set-1"
        )
        self.command_sha256 = assessment_command_sha256(
            command_id="analysis-command-1",
            scope_sha256=self.scope_sha256,
            requirement_set_id="set-1",
            requirement_set_version=1,
            requirement_tree_sha256=_TREE_SHA256,
            knowledge_base_version=2,
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """
                CREATE TABLE ai_analysis_jobs(
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    sor_document_id TEXT NOT NULL,
                    requirement_set_id TEXT NOT NULL,
                    requirement_set_version INTEGER NOT NULL,
                    requirement_tree_sha256 TEXT NOT NULL,
                    knowledge_base_version INTEGER NOT NULL,
                    profile_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    assessment_mode TEXT NOT NULL,
                    retrieval_backend TEXT NOT NULL,
                    chat_model TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    command_id TEXT NOT NULL,
                    command_sha256 TEXT NOT NULL,
                    scope_schema_version TEXT NOT NULL,
                    scope_kind TEXT NOT NULL,
                    scope_payload_json TEXT NOT NULL,
                    scope_sha256 TEXT NOT NULL,
                    publication_mode TEXT NOT NULL,
                    total_requirements INTEGER NOT NULL,
                    completed_requirements INTEGER NOT NULL,
                    published_requirements INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO ai_analysis_jobs VALUES (
                    'job-1', 'project-1', 'document-1', 'set-1', 1, ?, 2,
                    'profile-1', 'provider-1', 'heuristic', 'local',
                    'model-1', 'embedding-1',
                    'analysis-command-1', ?, ?, 'CURRENT', ?, ?,
                    'ATOMIC_BATCH', 1, 1, 1, 'COMPLETED', '',
                    '2026-08-08T00:00:00+00:00', '2026-08-08T00:00:00+00:00'
                )
                """,
                (
                    _TREE_SHA256,
                    self.command_sha256,
                    _SCOPE_SCHEMA_VERSION,
                    self.payload_json,
                    self.scope_sha256,
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cutover_preserves_and_validates_canonical_scope(self) -> None:
        plan = build_cutover_plan(self.database)
        row = plan.rewritten_rows["ai_analysis_jobs"][0]

        self.assertEqual(row["scope_payload_json"], self.payload_json)
        self.assertEqual(row["scope_sha256"], self.scope_sha256)
        self.assertEqual(row["published_requirements"], 1)

        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0018_assessment_scope.sql"
        ).read_text(encoding="utf-8")
        for column in (
            "command_id",
            "command_sha256",
            "scope_schema_version",
            "scope_kind",
            "scope_payload_json",
            "scope_sha256",
            "requirement_set_version",
            "requirement_tree_sha256",
            "publication_mode",
            "published_requirements",
        ):
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {column}", migration)
        self.assertIn("idx_ai_analysis_jobs_document_command", migration)

    def test_cutover_rejects_tampered_scope_hash(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET scope_sha256 = ?",
                ("f" * 64,),
            )

        with self.assertRaisesRegex(CutoverRejected, "payload hash"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_noncanonical_scope_payload(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET scope_payload_json = scope_payload_json || ' '"
            )

        with self.assertRaisesRegex(CutoverRejected, "not canonical JSON"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_command_identity_drift(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE ai_analysis_jobs SET knowledge_base_version = 9")

        with self.assertRaisesRegex(CutoverRejected, "command hash"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_noncanonical_scope_kind(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE ai_analysis_jobs SET scope_kind = 'current'")

        with self.assertRaisesRegex(CutoverRejected, "scope kind"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_invalid_publication_mode(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET publication_mode = 'BROKEN'"
            )

        with self.assertRaisesRegex(CutoverRejected, "publication mode"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_noncanonical_tree_hash(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET requirement_tree_sha256 = ?",
                (_TREE_SHA256.upper(),),
            )

        with self.assertRaisesRegex(CutoverRejected, "tree hash"):
            build_cutover_plan(self.database)

    def test_cutover_rejects_invalid_command_id(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET command_id = 'invalid command'"
            )

        with self.assertRaisesRegex(CutoverRejected, "command id"):
            build_cutover_plan(self.database)


if __name__ == "__main__":
    unittest.main()
