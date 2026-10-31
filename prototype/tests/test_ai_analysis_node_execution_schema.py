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


_TREE_SHA256 = "a" * 64
_INPUT_SHA256 = "b" * 64
_SCOPE_VERSION = "requirement-analysis-scope/v2"
_PUBLICATION_MODE = "PER_NODE_RECOVERABLE"


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class AiAnalysisNodeExecutionSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-analysis-node-schema-"
        )
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()
        self.project = db.create_project("Node execution", "Customer")
        self.source = self.root / "source.txt"
        self.source.write_text("requirement", encoding="utf-8")
        self.document = db.add_sor_document(
            self.project["id"],
            self.source.name,
            "txt",
            str(self.source),
            "requirement",
        )
        self.requirement = db.insert_requirement(
            self.project["id"],
            self.document["id"],
            "REQ0001",
            1,
            "Page 1",
            "The product shall retain an audit record.",
            "FUNCTIONAL",
            "HIGH",
        )
        self.requirement_set_id = str(self.requirement["requirement_set_id"])
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_sets SET requirement_tree_sha256 = ? WHERE id = ?",
                (_TREE_SHA256, self.requirement_set_id),
            )

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def _create_job(self, *, status: str = "COMPLETED") -> dict[str, object]:
        scope_payload = {
            "schema_version": _SCOPE_VERSION,
            "requirement_set_id": self.requirement_set_id,
            "requirement_set_version": 1,
            "tree_sha256": _TREE_SHA256,
            "scope": "CURRENT",
            "requested_node_ids": ["node-1"],
            "target_pairs": [
                {
                    "node_id": "node-1",
                    "requirement_id": self.requirement["id"],
                }
            ],
            "analysis_universe_pairs": [
                {
                    "node_id": "node-1",
                    "requirement_id": self.requirement["id"],
                }
            ],
            "skipped": [],
            "target_examples": [],
            "skipped_examples": [],
            "publication_mode": _PUBLICATION_MODE,
            "skipped_reason_counts": {},
            "source_job_id": "",
            "failed_snapshot_sha256": "",
            "retry_summary": {
                "source_failed_count": 0,
                "target": 0,
                "already_current": 0,
                "skipped": 0,
            },
        }
        payload_json = _canonical_json(scope_payload)
        scope_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        command_sha256 = assessment_command_sha256(
            command_id="analysis-node-command-1",
            scope_sha256=scope_sha256,
            requirement_set_id=self.requirement_set_id,
            requirement_set_version=1,
            requirement_tree_sha256=_TREE_SHA256,
            knowledge_base_version=0,
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
        )
        return db.create_ai_analysis_job(
            self.project["id"],
            self.document["id"],
            requirement_set_id=self.requirement_set_id,
            requirement_set_version=1,
            requirement_tree_sha256=_TREE_SHA256,
            knowledge_base_version=0,
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
            command_id="analysis-node-command-1",
            command_sha256=command_sha256,
            scope_schema_version=_SCOPE_VERSION,
            scope_kind="CURRENT",
            scope_payload_json=payload_json,
            scope_sha256=scope_sha256,
            publication_mode=_PUBLICATION_MODE,
            total_requirements=1,
            published_requirements=1 if status == "COMPLETED" else 0,
            status=status,
        )

    def _insert_success(self, job_id: str) -> tuple[str, str, str]:
        target_id = "target-1"
        attempt_id = "attempt-1"
        result_json = _canonical_json({"decision": "PASS", "reason": "grounded"})
        result_sha256 = hashlib.sha256(result_json.encode("utf-8")).hexdigest()
        timestamp = "2026-08-08T00:00:00+00:00"
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO ai_analysis_job_targets (
                    id, job_id, sor_document_id, node_id, requirement_id,
                    requirement_code, source_order, status, latest_attempt_no,
                    latest_attempt_id, published_attempt_id, retryable,
                    error_code, error_message, created_at, updated_at
                ) VALUES (?, ?, ?, 'node-1', ?, 'DISPLAY-REQ0001', 0, 'SUCCEEDED',
                          1, ?, ?, 0, '', '', ?, ?)
                """,
                (
                    target_id,
                    job_id,
                    self.document["id"],
                    self.requirement["id"],
                    attempt_id,
                    attempt_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_analysis_node_attempts (
                    id, target_id, job_id, node_id, requirement_id, attempt_no,
                    fence_token, status, result_schema_version,
                    result_payload_json, result_sha256,
                    analysis_input_schema_version, analysis_input_sha256,
                    error_code, error_message, started_at, finished_at, updated_at
                ) VALUES (?, ?, ?, 'node-1', ?, 1, 'fence-1', 'SUCCEEDED',
                          'requirement-analysis-node-result/v1', ?, ?,
                          'requirement-analysis-input/v1', ?, '', '', ?, ?, ?)
                """,
                (
                    attempt_id,
                    target_id,
                    job_id,
                    self.requirement["id"],
                    result_json,
                    result_sha256,
                    _INPUT_SHA256,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        return target_id, attempt_id, result_json

    def test_sqlite_round_trip_orders_targets_and_binds_assessment_runs(self) -> None:
        job = self._create_job()
        target_id, attempt_id, _ = self._insert_success(str(job["id"]))
        second_requirement = db.insert_requirement(
            self.project["id"],
            self.document["id"],
            "REQ0002",
            2,
            "Page 2",
            "The product shall expose an audit export.",
            "FUNCTIONAL",
            "MEDIUM",
        )
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE ai_analysis_job_targets SET source_order = 5 WHERE id = ?",
                (target_id,),
            )
            connection.execute(
                """
                INSERT INTO ai_analysis_job_targets (
                    id, job_id, sor_document_id, node_id, requirement_id,
                    requirement_code, source_order, created_at, updated_at
                ) VALUES ('target-2', ?, ?, 'node-2', ?, 'REQ0002', 2, ?, ?)
                """,
                (
                    job["id"],
                    self.document["id"],
                    second_requirement["id"],
                    "2026-08-08T00:00:00+00:00",
                    "2026-08-08T00:00:00+00:00",
                ),
            )
        run = db.add_assessment_run(
            self.requirement["id"],
            analysis_node_attempt_id=attempt_id,
            run_index=0,
            profile_id="profile-1",
            provider="provider-1",
            mode="heuristic",
            retrieval_backend="local",
            model_name="model-1",
            prompt_version="prompt-1",
            input_payload_json="{}",
            output_payload_json="{}",
            validation_status="passed",
        )

        targets = db.list_ai_analysis_job_targets(str(job["id"]))
        attempts = db.list_ai_analysis_node_attempts(target_id)

        self.assertEqual(
            [item["id"] for item in targets], ["target-2", target_id]
        )
        self.assertTrue(targets[0]["retryable"])
        self.assertFalse(targets[1]["retryable"])
        self.assertEqual(attempts[0]["id"], attempt_id)
        self.assertEqual(attempts[0]["result_payload"]["decision"], "PASS")
        self.assertEqual(run["analysis_node_attempt_id"], attempt_id)
        self.assertEqual(run["run_index"], 0)
        self.assertEqual(db.get_ai_analysis_node_attempt(attempt_id)["id"], attempt_id)

        with self.assertRaises(sqlite3.IntegrityError):
            db.add_assessment_run(
                self.requirement["id"],
                analysis_node_attempt_id=attempt_id,
                run_index=0,
                profile_id="profile-1",
                provider="provider-1",
                mode="heuristic",
                retrieval_backend="local",
                model_name="model-1",
                prompt_version="prompt-1",
                input_payload_json="{}",
                output_payload_json="{}",
                validation_status="passed",
            )

    def test_sqlite_additive_upgrade_creates_attempt_columns_before_index(self) -> None:
        legacy_database = self.root / "legacy.db"
        with sqlite3.connect(legacy_database) as connection:
            connection.execute(
                """
                CREATE TABLE assessment_runs (
                    id TEXT PRIMARY KEY,
                    requirement_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL DEFAULT '',
                    provider TEXT NOT NULL DEFAULT '',
                    mode TEXT NOT NULL,
                    retrieval_backend TEXT NOT NULL,
                    model_name TEXT NOT NULL,
                    prompt_version TEXT NOT NULL,
                    input_payload_json TEXT NOT NULL,
                    output_payload_json TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    fallback_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )
                """
            )

        db.prepare_test_legacy_sqlite_fixture(legacy_database)
        with patch.object(db, "DB_PATH", legacy_database), patch.object(
            db, "STORAGE_DIR", self.root
        ):
            db.init_db()
            with db.get_conn() as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(assessment_runs)"
                    ).fetchall()
                }
                indexes = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA index_list(assessment_runs)"
                    ).fetchall()
                }

        self.assertIn("analysis_node_attempt_id", columns)
        self.assertIn("run_index", columns)
        self.assertIn("idx_assessment_runs_node_attempt_index", indexes)

    def test_cutover_preserves_canonical_success_and_relationships(self) -> None:
        job = self._create_job()
        target_id, attempt_id, result_json = self._insert_success(str(job["id"]))
        db.add_assessment_run(
            self.requirement["id"],
            analysis_node_attempt_id=attempt_id,
            run_index=0,
            profile_id="profile-1",
            provider="provider-1",
            mode="heuristic",
            retrieval_backend="local",
            model_name="model-1",
            prompt_version="prompt-1",
            input_payload_json="{}",
            output_payload_json=result_json,
            validation_status="passed",
        )

        plan = build_cutover_plan(Path(db.DB_PATH))

        self.assertEqual(
            plan.rewritten_rows["ai_analysis_job_targets"][0]["id"],
            target_id,
        )
        self.assertEqual(
            plan.rewritten_rows["ai_analysis_node_attempts"][0]["result_payload_json"],
            result_json,
        )
        self.assertEqual(
            plan.rewritten_rows["assessment_runs"][0]["analysis_node_attempt_id"],
            attempt_id,
        )

    def test_cutover_fails_closed_for_active_target_and_attempt(self) -> None:
        job = self._create_job(status="RUNNING")
        timestamp = "2026-08-08T00:00:00+00:00"
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO ai_analysis_job_targets (
                    id, job_id, sor_document_id, node_id, requirement_id,
                    requirement_code, source_order, status, latest_attempt_no,
                    latest_attempt_id, published_attempt_id, retryable,
                    error_code, error_message, created_at, updated_at
                ) VALUES ('target-1', ?, ?, 'node-1', ?, 'REQ0001', 0,
                          'RUNNING', 1, 'attempt-1', '', 1, '', '', ?, ?)
                """,
                (
                    job["id"],
                    self.document["id"],
                    self.requirement["id"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_analysis_node_attempts (
                    id, target_id, job_id, node_id, requirement_id, attempt_no,
                    fence_token, status, result_payload_json, started_at, updated_at
                ) VALUES ('attempt-1', 'target-1', ?, 'node-1', ?, 1,
                          'fence-1', 'RUNNING', '{}', ?, ?)
                """,
                (job["id"], self.requirement["id"], timestamp, timestamp),
            )

        plan = build_cutover_plan(Path(db.DB_PATH))
        rewritten_job = plan.rewritten_rows["ai_analysis_jobs"][0]
        rewritten_target = plan.rewritten_rows["ai_analysis_job_targets"][0]
        rewritten_attempt = plan.rewritten_rows["ai_analysis_node_attempts"][0]

        self.assertEqual(rewritten_job["status"], "FAILED")
        self.assertEqual(rewritten_target["status"], "FAILED")
        self.assertEqual(rewritten_attempt["status"], "FAILED")
        self.assertEqual(rewritten_attempt["error_code"], "hard_cutover_interrupted")
        self.assertTrue(rewritten_attempt["finished_at"])

    def test_cutover_accepts_recovered_waiting_target_high_water_mark(self) -> None:
        job = self._create_job(status="RUNNING")
        timestamp = "2026-08-08T00:00:00+00:00"
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO ai_analysis_job_targets (
                    id, job_id, sor_document_id, node_id, requirement_id,
                    requirement_code, source_order, status, latest_attempt_no,
                    latest_attempt_id, published_attempt_id, retryable,
                    error_code, error_message, created_at, updated_at
                ) VALUES ('target-1', ?, ?, 'node-1', ?, 'REQ0001', 0,
                          'WAITING', 1, '', '', 0, '', '', ?, ?)
                """,
                (
                    job["id"],
                    self.document["id"],
                    self.requirement["id"],
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO ai_analysis_node_attempts (
                    id, target_id, job_id, node_id, requirement_id, attempt_no,
                    fence_token, status, result_payload_json, error_code,
                    error_message, started_at, finished_at, updated_at
                ) VALUES ('attempt-1', 'target-1', ?, 'node-1', ?, 1,
                          'fence-1', 'FAILED', '{}', 'analysis_worker_interrupted',
                          'interrupted', ?, ?, ?)
                """,
                (
                    job["id"],
                    self.requirement["id"],
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )

        plan = build_cutover_plan(Path(db.DB_PATH))

        self.assertEqual(
            plan.rewritten_rows["ai_analysis_job_targets"][0]["status"],
            "FAILED",
        )
        self.assertEqual(
            plan.rewritten_rows["ai_analysis_node_attempts"][0]["status"],
            "FAILED",
        )

    def test_cutover_rejects_noncanonical_attempt_result(self) -> None:
        job = self._create_job()
        _, attempt_id, _ = self._insert_success(str(job["id"]))
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE ai_analysis_node_attempts
                SET result_payload_json = result_payload_json || ' '
                WHERE id = ?
                """,
                (attempt_id,),
            )

        with self.assertRaisesRegex(CutoverRejected, "not canonical JSON"):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_rejects_attempt_result_hash_mismatch(self) -> None:
        job = self._create_job()
        _, attempt_id, _ = self._insert_success(str(job["id"]))
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE ai_analysis_node_attempts
                SET result_sha256 = ?
                WHERE id = ?
                """,
                ("f" * 64, attempt_id),
            )

        with self.assertRaisesRegex(CutoverRejected, "hash does not match"):
            build_cutover_plan(Path(db.DB_PATH))

    def test_postgresql_migration_expands_scope_and_creates_node_tables(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0019_assessment_node_execution.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("CREATE TABLE IF NOT EXISTS ai_analysis_job_targets", migration)
        self.assertIn("CREATE TABLE IF NOT EXISTS ai_analysis_node_attempts", migration)
        self.assertIn("requirement-analysis-scope/v2", migration)
        self.assertIn("FAILED_ONLY", migration)
        self.assertIn("PER_NODE_RECOVERABLE", migration)
        self.assertIn("idx_assessment_runs_node_attempt_index", migration)
        self.assertNotIn("UPDATE ai_analysis_job_targets", migration)


if __name__ == "__main__":
    unittest.main()
