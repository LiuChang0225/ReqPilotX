from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Event, Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.adapters.persistence.assessment_results import (
    PER_NODE_RECOVERABLE_PUBLICATION_MODE,
    cancel_waiting_analysis_targets,
    claim_analysis_job,
    claim_analysis_node_attempt,
    fail_analysis_node_attempt,
    finalize_incremental_analysis_job,
    publish_staged_analysis_node_result,
    recover_analysis_job_targets,
    stage_analysis_node_result,
)
from app.application import assessment, runtime as application_runtime
from app.application.assessment_contracts import StopAIAnalysisRequest
from app.application.errors import ApplicationError
from app.infrastructure.runtime_settings import DatabaseBackend
from app.ports.job_queue import (
    DurableJob,
    JobLeaseFence,
    JobLeaseLostError,
    JobStatus,
)


_TREE_SHA256 = "a" * 64
_INPUT_SHA256 = "b" * 64
_SCOPE_SCHEMA_VERSION = "requirement-analysis-scope/v2"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class IncrementalAIAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="reqpilotx-ai-analysis-incremental-"
        )
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()

        self.project = db.create_project("Incremental analysis", "Customer")
        source = self.root / "source.txt"
        source.write_text("Three requirements", encoding="utf-8")
        self.document = db.add_sor_document(
            self.project["id"],
            source.name,
            "txt",
            str(source),
            source.read_text(encoding="utf-8"),
        )
        self.requirements = [
            db.insert_requirement(
                self.project["id"],
                self.document["id"],
                f"REQ000{index}",
                index,
                f"Page {index}",
                f"The supplier shall retain evidence {index}.",
                "REGULATION",
                "MEDIUM",
            )
            for index in (1, 2, 3)
        ]
        self.requirement_set_id = str(self.requirements[0]["requirement_set_id"])
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_sha256 = ?
                WHERE id = ? AND status = 'DRAFT'
                """,
                (_TREE_SHA256, self.requirement_set_id),
            )
        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=self.requirement_set_id,
        )
        self.document = db.get_sor_document(self.document["id"]) or self.document
        self.requirement_set = db.get_requirement_set(self.requirement_set_id) or {}

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def _claim_job(
        self,
        target_indexes: tuple[int, ...],
        *,
        command_id: str,
        universe_indexes: tuple[int, ...] = (0, 1, 2),
        scope_kind: str = "ALL",
    ) -> tuple[dict, list[dict]]:
        target_pairs = [
            {
                "node_id": f"node-{index + 1}",
                "requirement_id": self.requirements[index]["id"],
            }
            for index in target_indexes
        ]
        universe_pairs = [
            {
                "node_id": f"node-{index + 1}",
                "requirement_id": self.requirements[index]["id"],
            }
            for index in universe_indexes
        ]
        payload = {
            "analysis_universe_pairs": universe_pairs,
            "failed_snapshot_sha256": "",
            "publication_mode": PER_NODE_RECOVERABLE_PUBLICATION_MODE,
            "requested_node_ids": [item["node_id"] for item in target_pairs],
            "requirement_set_id": self.requirement_set_id,
            "requirement_set_version": int(self.requirement_set["version"]),
            "retry_summary": {
                "already_current": 0,
                "skipped": 0,
                "source_failed_count": 0,
                "target": 0,
            },
            "schema_version": _SCOPE_SCHEMA_VERSION,
            "scope": scope_kind,
            "skipped": [],
            "source_job_id": "",
            "target_pairs": target_pairs,
            "tree_sha256": _TREE_SHA256,
        }
        payload_json = _canonical_json(payload)
        scope_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        descriptors = [
            {
                **pair,
                "requirement_code": str(
                    self.requirements[index].get("req_code")
                    or self.requirements[index].get("code")
                    or pair["requirement_id"]
                ),
                "source_order": source_order,
            }
            for source_order, (index, pair) in enumerate(
                zip(target_indexes, target_pairs, strict=True)
            )
        ]
        job, claimed = claim_analysis_job(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set_id,
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
            chat_model="model-1",
            embedding_model="embedding-1",
            total_requirements=len(target_pairs),
            queued_message="queued",
            command_id=command_id,
            command_sha256=hashlib.sha256(command_id.encode("utf-8")).hexdigest(),
            scope_schema_version=_SCOPE_SCHEMA_VERSION,
            scope_kind=scope_kind,
            scope_payload_json=payload_json,
            scope_sha256=scope_sha256,
            requirement_set_version=int(self.requirement_set["version"]),
            requirement_tree_sha256=_TREE_SHA256,
            expected_knowledge_base_version=0,
            publication_mode=PER_NODE_RECOVERABLE_PUBLICATION_MODE,
            target_descriptors=descriptors,
        )
        self.assertTrue(claimed)
        job = db.update_ai_analysis_job(
            str(job["id"]),
            status="RUNNING",
            current_stage="assessment",
            started_at="2026-08-08T00:00:00+00:00",
        )
        return job, db.list_ai_analysis_job_targets(str(job["id"]))

    @staticmethod
    def _result(reason: str, *, with_run: bool = False) -> dict:
        runs = []
        if with_run:
            runs.append(
                {
                    "profile_id": "profile-1",
                    "provider": "provider-1",
                    "mode": "heuristic",
                    "retrieval_backend": "local",
                    "model_name": "model-1",
                    "prompt_version": "prompt-1",
                    "input_payload_json": "{}",
                    "output_payload_json": "{}",
                    "validation_status": "VALID",
                    "fallback_reason": "",
                }
            )
        return {
            "assessment": {
                "ai_decision": "ACCEPT",
                "ai_reason": reason,
                "ai_confidence": 0.91,
                "assessment_mode": "heuristic",
                "retrieval_backend": "local",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "source-bound test result",
                "normalized_requirement": reason,
                "risk_level": "LOW",
                "clarification_question": "",
                "model_name": "model-1",
                "prompt_version": "prompt-1",
            },
            "evidences": [],
            "runs": runs,
        }

    def _claim_attempt(self, job: dict, target: dict) -> dict:
        return claim_analysis_node_attempt(
            job_id=str(job["id"]),
            target_id=str(target["id"]),
        )

    def _stage_and_publish(
        self,
        job: dict,
        target: dict,
        *,
        reason: str,
        with_run: bool = False,
    ) -> tuple[dict, dict]:
        attempt = self._claim_attempt(job, target)
        stage_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(target["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
            result=self._result(reason, with_run=with_run),
            analysis_input_schema_version="analysis-input/v1",
            analysis_input_sha256=_INPUT_SHA256,
        )
        published = publish_staged_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(target["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
        )
        return attempt, published

    def _fail_target(
        self,
        job: dict,
        target: dict,
        *,
        retryable: bool,
    ) -> dict:
        attempt = self._claim_attempt(job, target)
        return fail_analysis_node_attempt(
            job_id=str(job["id"]),
            target_id=str(target["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
            error_code="provider_timeout",
            error_message="The provider timed out.",
            retryable=retryable,
        )

    def _durable_job(
        self,
        job: dict,
        *,
        status: JobStatus,
        error_message: str = "",
    ) -> DurableJob:
        now = datetime.now(timezone.utc)
        return DurableJob(
            job_id=f"runtime-{job['id']}",
            kind="document.assess",
            aggregate_type="sor-document",
            aggregate_id=self.document["id"],
            idempotency_key=f"runtime-{job['id']}",
            payload={},
            status=status,
            priority=0,
            attempts=2,
            max_attempts=2,
            generation=2,
            cancel_requested=status is JobStatus.CANCELLED,
            lease_owner="",
            lease_expires_at=None,
            available_at=now,
            created_at=now,
            updated_at=now,
            progress={},
            result={} if status is JobStatus.SUCCEEDED else None,
            error_code="job_attempts_exhausted" if status is JobStatus.FAILED else "",
            error_message=error_message,
        )

    def _finalize_as_postgres_orphan(
        self,
        job: dict,
        *,
        durable_status: JobStatus,
        error_message: str = "",
    ) -> dict:
        durable = self._durable_job(
            job,
            status=durable_status,
            error_message=error_message,
        )
        queue = SimpleNamespace(
            get=lambda job_id: durable if job_id == durable.job_id else None
        )
        with (
            patch.object(
                application_runtime,
                "RUNTIME_SETTINGS",
                SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL),
            ),
            patch.object(application_runtime, "_durable_jobs", return_value=queue),
            patch.object(
                db,
                "get_running_ai_analysis_job",
                return_value={**job, "runtime_job_id": durable.job_id},
            ),
        ):
            return (
                assessment._finalize_orphaned_ai_analysis_job(self.document["id"]) or {}
            )

    def test_target_ledger_keeps_frozen_source_order(self) -> None:
        job, targets = self._claim_job(
            (2, 0, 1),
            command_id="source-order-command",
        )

        self.assertEqual(
            [target["node_id"] for target in targets],
            ["node-3", "node-1", "node-2"],
        )
        self.assertEqual([target["source_order"] for target in targets], [0, 1, 2])

        self._fail_target(job, targets[2], retryable=True)
        self._fail_target(job, targets[0], retryable=True)
        self.assertEqual(
            [
                target["node_id"]
                for target in db.list_ai_analysis_job_targets(job["id"])
            ],
            ["node-3", "node-1", "node-2"],
        )

    def test_scheduler_runs_at_most_two_targets_in_parallel(self) -> None:
        job, _targets = self._claim_job(
            (0, 1, 2),
            command_id="controlled-parallel-command",
        )
        rows = [
            {
                **requirement,
                "matrix_node_id": f"node-{index + 1}",
                "display_text": requirement["requirement_text"],
            }
            for index, requirement in enumerate(self.requirements)
        ]
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
        )
        first_pair = Barrier(2)
        state_lock = Lock()
        active = 0
        maximum_active = 0
        calls: list[str] = []

        def provider(row, _knowledge_docs, **_kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                calls.append(str(row["id"]))
                call_number = len(calls)
            try:
                if call_number <= 2:
                    try:
                        first_pair.wait(timeout=2)
                    except BrokenBarrierError as exc:
                        self.fail(
                            f"the first two targets did not run in parallel: {exc}"
                        )
                result = self._result(f"parallel result {row['id']}")
                result["trace_path"] = f"/private/runtime/{row['id']}.json"
                return result
            finally:
                with state_lock:
                    active -= 1

        with patch.object(
            assessment,
            "run_ai_copilot_assessment",
            side_effect=provider,
        ):
            completed = assessment._execute_incremental_document_ai_analysis(
                self.project,
                self.document,
                rows,
                [],
                ai_config=ai_config,
                job_id=str(job["id"]),
            )

        self.assertEqual(completed["status"], "COMPLETED")
        self.assertEqual(maximum_active, 2)
        self.assertCountEqual(calls, [item["id"] for item in self.requirements])
        self.assertEqual(
            [
                target["status"]
                for target in db.list_ai_analysis_job_targets(str(job["id"]))
            ],
            ["SUCCEEDED", "SUCCEEDED", "SUCCEEDED"],
        )
        with db.get_conn() as connection:
            staged_payloads = [
                json.loads(str(row["result_payload_json"] or "{}"))
                for row in connection.execute(
                    """
                    SELECT result_payload_json
                    FROM ai_analysis_node_attempts
                    WHERE job_id = ? ORDER BY attempt_no, id
                    """,
                    (job["id"],),
                ).fetchall()
            ]
        self.assertTrue(staged_payloads)
        self.assertTrue(
            all("trace_path" not in payload["result"] for payload in staged_payloads)
        )

    def test_success_is_published_when_a_peer_fails(self) -> None:
        job, targets = self._claim_job(
            (0, 1),
            command_id="partial-success-command",
            universe_indexes=(0, 1),
        )

        self._stage_and_publish(job, targets[1], reason="published peer result")
        self._fail_target(job, targets[0], retryable=True)
        completed = finalize_incremental_analysis_job(str(job["id"]))

        self.assertEqual(completed["status"], "FAILED")
        self.assertEqual(int(completed["published_requirements"]), 1)
        self.assertEqual(int(completed["completed_requirements"]), 1)
        self.assertEqual(
            (db.get_assessment(self.requirements[1]["id"]) or {})["ai_reason"],
            "published peer result",
        )
        target_states = {
            target["node_id"]: target
            for target in db.list_ai_analysis_job_targets(str(job["id"]))
        }
        self.assertEqual(target_states["node-1"]["status"], "FAILED")
        self.assertTrue(target_states["node-1"]["retryable"])
        self.assertEqual(target_states["node-2"]["status"], "SUCCEEDED")

    def test_staged_publication_resumes_without_provider_and_replays_idempotently(
        self,
    ) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="staged-replay-command",
            universe_indexes=(0,),
        )
        attempt = self._claim_attempt(job, targets[0])
        stage_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(targets[0]["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
            result=self._result("replayed staged result", with_run=True),
            analysis_input_schema_version="analysis-input/v1",
            analysis_input_sha256=_INPUT_SHA256,
        )

        with (
            patch.object(assessment, "run_ai_copilot_assessment") as run_provider,
            patch.object(
                assessment,
                "_prepare_document_ai_analysis_request",
                side_effect=RuntimeError("provider configuration unavailable"),
            ) as prepare,
        ):
            assessment._run_document_ai_analysis_worker(
                str(job["id"]),
                self.project["id"],
                self.document["id"],
            )
        run_provider.assert_not_called()
        prepare.assert_not_called()

        first_assessment = db.get_assessment(self.requirements[0]["id"]) or {}
        replay = publish_staged_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(targets[0]["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(
            (db.get_assessment(self.requirements[0]["id"]) or {})["id"],
            first_assessment["id"],
        )
        with db.get_conn() as connection:
            run_count = connection.execute(
                """
                SELECT COUNT(*) FROM assessment_runs
                WHERE analysis_node_attempt_id = ?
                """,
                (attempt["attempt_id"],),
            ).fetchone()[0]
        self.assertEqual(int(run_count), 1)
        self.assertEqual(
            finalize_incremental_analysis_job(str(job["id"]))["status"],
            "COMPLETED",
        )
        terminal_replay = publish_staged_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(targets[0]["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
        )
        self.assertTrue(terminal_replay["replayed"])

    def test_postgres_publication_binds_case_guards_as_booleans(self) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="postgres-boolean-publication-command",
            universe_indexes=(0,),
        )
        attempt = self._claim_attempt(job, targets[0])
        stage_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(targets[0]["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
            result=self._result("postgres boolean publication"),
            analysis_input_schema_version="analysis-input/v1",
            analysis_input_sha256=_INPUT_SHA256,
        )
        original_get_conn = db.get_conn
        observed_guards: list[bool] = []

        class BooleanGuardConnection:
            def __init__(self, connection) -> None:
                self._connection = connection

            def execute(self, sql, parameters=None):
                values = tuple(parameters or ())
                if "UPDATE sor_documents" in sql and "CASE WHEN ? THEN" in sql:
                    guards = (values[1], values[3], values[5])
                    if not all(isinstance(value, bool) for value in guards):
                        raise TypeError("PostgreSQL CASE guards must be boolean")
                    observed_guards.extend(guards)
                if parameters is None:
                    return self._connection.execute(sql)
                return self._connection.execute(sql, parameters)

            def __getattr__(self, name):
                return getattr(self._connection, name)

        @contextmanager
        def guarded_get_conn():
            with original_get_conn() as connection:
                yield BooleanGuardConnection(connection)

        with patch.object(db, "get_conn", guarded_get_conn):
            published = publish_staged_analysis_node_result(
                job_id=str(job["id"]),
                target_id=str(targets[0]["id"]),
                attempt_id=str(attempt["attempt_id"]),
                fence_token=str(attempt["fence_token"]),
            )

        self.assertFalse(published["replayed"])
        self.assertEqual(observed_guards, [True, True, True])

    def test_recovery_fences_the_interrupted_attempt(self) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="stale-fence-command",
            universe_indexes=(0,),
        )
        interrupted = self._claim_attempt(job, targets[0])
        recovered = recover_analysis_job_targets(str(job["id"]))
        self.assertEqual(recovered[0]["status"], "WAITING")
        replacement = self._claim_attempt(job, recovered[0])
        self.assertEqual(int(replacement["attempt_no"]), 2)

        with self.assertRaisesRegex(RuntimeError, "analysis_node_attempt_stale"):
            stage_analysis_node_result(
                job_id=str(job["id"]),
                target_id=str(targets[0]["id"]),
                attempt_id=str(interrupted["attempt_id"]),
                fence_token=str(interrupted["fence_token"]),
                result=self._result("late old worker"),
                analysis_input_schema_version="analysis-input/v1",
                analysis_input_sha256=_INPUT_SHA256,
            )
        with self.assertRaisesRegex(RuntimeError, "analysis_node_attempt_stale"):
            stage_analysis_node_result(
                job_id=str(job["id"]),
                target_id=str(targets[0]["id"]),
                attempt_id=str(replacement["attempt_id"]),
                fence_token=str(interrupted["fence_token"]),
                result=self._result("wrong fence"),
                analysis_input_schema_version="analysis-input/v1",
                analysis_input_sha256=_INPUT_SHA256,
            )
        attempts = db.list_ai_analysis_node_attempts(str(targets[0]["id"]))
        self.assertEqual([item["status"] for item in attempts], ["FAILED", "RUNNING"])

    def test_stale_durable_generation_is_rejected_before_target_claim(self) -> None:
        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def __init__(self) -> None:
                self.calls: list[tuple[str, tuple]] = []

            def execute(self, sql, parameters=()):
                self.calls.append((str(sql), tuple(parameters or ())))
                return Cursor()

        connection = Connection()

        @contextmanager
        def rejected_fence_connection():
            yield connection

        with (
            patch.object(db, "get_conn", rejected_fence_connection),
            self.assertRaises(JobLeaseLostError),
        ):
            claim_analysis_node_attempt(
                job_id="analysis-job",
                target_id="target-1",
                delivery_fence=JobLeaseFence(
                    job_id="runtime-job",
                    generation=4,
                    worker_id="replacement-worker",
                ),
            )

        self.assertEqual(len(connection.calls), 2)
        self.assertIn("BEGIN IMMEDIATE", connection.calls[0][0])
        self.assertIn("FOR UPDATE OF runtime", connection.calls[1][0])
        self.assertEqual(
            connection.calls[1][1],
            ("analysis-job", "runtime-job", 4, "replacement-worker"),
        )
        self.assertFalse(
            any("INSERT INTO ai_analysis_node_attempts" in sql for sql, _ in connection.calls)
        )

    def test_superseded_delivery_cannot_abandon_replacement_attempt(self) -> None:
        job, _targets = self._claim_job(
            (0,),
            command_id="interleaved-delivery-command",
            universe_indexes=(0,),
        )
        row = {
            **self.requirements[0],
            "matrix_node_id": "node-1",
            "display_text": self.requirements[0]["requirement_text"],
        }
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
        )
        first_started = Event()
        replacement_started = Event()
        release_first = Event()
        release_replacement = Event()
        call_lock = Lock()
        call_count = 0
        outcomes: dict[str, dict] = {}
        failures: dict[str, BaseException] = {}

        def provider(*_args, **_kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count
            if current_call == 1:
                first_started.set()
                if not release_first.wait(timeout=5):
                    raise RuntimeError("first delivery test release timed out")
                return self._result("late first delivery result")
            replacement_started.set()
            if not release_replacement.wait(timeout=5):
                raise RuntimeError("replacement delivery test release timed out")
            return self._result("replacement delivery result")

        def run_delivery(name: str) -> None:
            try:
                outcomes[name] = assessment._execute_incremental_document_ai_analysis(
                    self.project,
                    self.document,
                    [row],
                    [],
                    ai_config=ai_config,
                    job_id=str(job["id"]),
                )
            except BaseException as exc:
                failures[name] = exc

        first = Thread(target=run_delivery, args=("first",), daemon=True)
        replacement = Thread(
            target=run_delivery,
            args=("replacement",),
            daemon=True,
        )
        with patch.object(
            assessment,
            "run_ai_copilot_assessment",
            side_effect=provider,
        ):
            try:
                first.start()
                self.assertTrue(first_started.wait(timeout=5))
                replacement.start()
                self.assertTrue(replacement_started.wait(timeout=5))

                release_first.set()
                first.join(timeout=5)
                self.assertFalse(first.is_alive())
                self.assertIsInstance(
                    failures.get("first"),
                    assessment._IncrementalAnalysisDeliverySuperseded,
                )

                live_targets = db.list_ai_analysis_job_targets(str(job["id"]))
                self.assertEqual(live_targets[0]["status"], "RUNNING")
                attempts = db.list_ai_analysis_node_attempts(str(live_targets[0]["id"]))
                self.assertEqual(
                    [item["status"] for item in attempts],
                    ["FAILED", "RUNNING"],
                )

                release_replacement.set()
                replacement.join(timeout=5)
                self.assertFalse(replacement.is_alive())
            finally:
                release_first.set()
                release_replacement.set()
                first.join(timeout=5)
                if replacement.ident is not None:
                    replacement.join(timeout=5)

        self.assertNotIn("replacement", failures)
        self.assertEqual(outcomes["replacement"]["status"], "COMPLETED")
        self.assertEqual(
            (db.get_assessment(self.requirements[0]["id"]) or {})["ai_reason"],
            "replacement delivery result",
        )

    def test_lease_lost_delivery_exits_before_recovery_or_provider_setup(self) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="lease-lost-delivery-command",
            universe_indexes=(0,),
        )

        def lose_lease() -> None:
            raise JobLeaseLostError("lease moved to replacement delivery")

        context = SimpleNamespace(heartbeat=lose_lease)

        with patch.object(
            assessment,
            "_prepare_document_ai_analysis_request",
        ) as prepare:
            superseded = assessment._run_document_ai_analysis_worker(
                str(job["id"]),
                self.project["id"],
                self.document["id"],
                delivery_context=context,
            )

        self.assertTrue(superseded)
        prepare.assert_not_called()
        current_targets = db.list_ai_analysis_job_targets(str(job["id"]))
        self.assertEqual(current_targets[0]["id"], targets[0]["id"])
        self.assertEqual(current_targets[0]["status"], "WAITING")
        self.assertEqual(
            db.list_ai_analysis_node_attempts(str(targets[0]["id"])),
            [],
        )

    def test_superseded_delivery_checks_lease_before_claiming_recovered_target(
        self,
    ) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="pre-claim-lease-fence-command",
            universe_indexes=(0,),
        )
        row = {
            **self.requirements[0],
            "matrix_node_id": "node-1",
            "display_text": self.requirements[0]["requirement_text"],
        }
        ai_config = SimpleNamespace(
            profile_id="profile-1",
            provider="provider-1",
            assessment_mode="heuristic",
            retrieval_backend="local",
        )

        class SupersededContext:
            def cancellation_requested(inner_self) -> bool:
                recover_analysis_job_targets(str(job["id"]))
                raise JobLeaseLostError("replacement delivery owns the runtime job")

        with (
            patch.object(assessment, "run_ai_copilot_assessment") as provider,
            self.assertRaises(assessment._IncrementalAnalysisDeliverySuperseded),
        ):
            assessment._execute_incremental_analysis_target(
                job_id=str(job["id"]),
                document_id=self.document["id"],
                target=targets[0],
                row=row,
                knowledge_docs=[],
                ai_config=ai_config,
                delivery_context=SupersededContext(),
            )

        provider.assert_not_called()
        self.assertEqual(
            db.list_ai_analysis_job_targets(str(job["id"]))[0]["status"],
            "WAITING",
        )
        self.assertEqual(
            db.list_ai_analysis_node_attempts(str(targets[0]["id"])),
            [],
        )
        replacement_attempt = self._claim_attempt(job, targets[0])
        self.assertEqual(int(replacement_attempt["attempt_no"]), 1)

    def test_exhausted_delivery_abandons_business_job_and_releases_document(
        self,
    ) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="exhausted-durable-delivery-command",
            universe_indexes=(0,),
        )

        finalized = self._finalize_as_postgres_orphan(
            job,
            durable_status=JobStatus.FAILED,
            error_message="The worker lease expired after the final delivery attempt.",
        )

        self.assertEqual(finalized["status"], "FAILED")
        self.assertEqual(
            db.list_ai_analysis_job_targets(str(job["id"]))[0]["status"],
            "FAILED",
        )
        self.assertTrue(db.list_ai_analysis_job_targets(str(job["id"]))[0]["retryable"])
        replacement, _replacement_targets = self._claim_job(
            (0,),
            command_id="replacement-after-exhaustion-command",
            universe_indexes=(0,),
        )
        self.assertEqual(replacement["status"], "RUNNING")
        self.assertNotEqual(replacement["id"], job["id"])
        self.assertEqual(targets[0]["status"], "WAITING")

    def test_succeeded_delivery_cannot_leave_business_job_running(self) -> None:
        job, _targets = self._claim_job(
            (0,),
            command_id="succeeded-orphan-delivery-command",
            universe_indexes=(0,),
        )

        finalized = self._finalize_as_postgres_orphan(
            job,
            durable_status=JobStatus.SUCCEEDED,
        )

        self.assertEqual(finalized["status"], "FAILED")
        self.assertEqual(
            (db.get_ai_analysis_job(str(job["id"])) or {})["status"],
            "FAILED",
        )
        self.assertEqual(
            db.list_ai_analysis_job_targets(str(job["id"]))[0]["error_code"],
            "analysis_worker_interrupted",
        )

    def test_exhausted_delivery_publishes_staged_result_before_orphan_cleanup(
        self,
    ) -> None:
        job, targets = self._claim_job(
            (0,),
            command_id="staged-result-before-exhaustion-command",
            universe_indexes=(0,),
        )
        attempt = self._claim_attempt(job, targets[0])
        stage_analysis_node_result(
            job_id=str(job["id"]),
            target_id=str(targets[0]["id"]),
            attempt_id=str(attempt["attempt_id"]),
            fence_token=str(attempt["fence_token"]),
            result=self._result("publish after durable exhaustion"),
            analysis_input_schema_version="analysis-input/v1",
            analysis_input_sha256=_INPUT_SHA256,
        )
        self.assertEqual(
            db.list_ai_analysis_job_targets(str(job["id"]))[0]["status"],
            "PUBLISHING",
        )

        finalized = self._finalize_as_postgres_orphan(
            job,
            durable_status=JobStatus.FAILED,
            error_message="The worker lease expired after the final delivery attempt.",
        )

        self.assertEqual(finalized["status"], "COMPLETED")
        self.assertEqual(
            db.list_ai_analysis_job_targets(str(job["id"]))[0]["status"],
            "SUCCEEDED",
        )
        self.assertEqual(
            (db.get_assessment(self.requirements[0]["id"]) or {})["ai_reason"],
            "publish after durable exhaustion",
        )

    def test_cancel_keeps_published_success_and_cancels_waiting_target(self) -> None:
        job, targets = self._claim_job(
            (0, 1),
            command_id="cancel-command",
            universe_indexes=(0, 1),
        )
        self._stage_and_publish(job, targets[0], reason="keep this result")
        db.update_ai_analysis_job(
            str(job["id"]),
            status="CANCEL_REQUESTED",
            current_stage="cancelling",
        )

        cancelled_targets = cancel_waiting_analysis_targets(str(job["id"]))
        completed = finalize_incremental_analysis_job(str(job["id"]))

        self.assertEqual(completed["status"], "CANCELLED")
        self.assertEqual(int(completed["published_requirements"]), 1)
        self.assertEqual(
            [target["status"] for target in cancelled_targets],
            ["SUCCEEDED", "CANCELLED"],
        )
        self.assertEqual(
            (db.get_assessment(self.requirements[0]["id"]) or {})["ai_reason"],
            "keep this result",
        )

    def test_failed_only_retry_excludes_nonretryable_and_later_repaired_targets(
        self,
    ) -> None:
        source_job, source_targets = self._claim_job(
            (0, 1, 2),
            command_id="failed-source-command",
        )
        self._fail_target(source_job, source_targets[0], retryable=True)
        self._fail_target(source_job, source_targets[1], retryable=True)
        self._fail_target(source_job, source_targets[2], retryable=False)
        source_job = finalize_incremental_analysis_job(str(source_job["id"]))
        self.assertEqual(source_job["status"], "FAILED")

        repair_job, repair_targets = self._claim_job(
            (1,),
            command_id="later-repair-command",
        )
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE ai_analysis_jobs SET created_at = ? WHERE id = ?",
                ("9999-12-31T23:59:59+00:00", repair_job["id"]),
            )
        self._stage_and_publish(
            repair_job,
            repair_targets[0],
            reason="later repair",
        )
        finalize_incremental_analysis_job(str(repair_job["id"]))

        matrix = SimpleNamespace(
            nodes=tuple(
                SimpleNamespace(
                    node_id=f"node-{index + 1}",
                    requirement_id=requirement["id"],
                    matrix_visibility="VISIBLE",
                    disposition="ANALYZE",
                    analysis_eligibility="ELIGIBLE",
                )
                for index, requirement in enumerate(self.requirements)
            )
        )
        retry_node_ids, metadata = assessment._resolve_failed_analysis_retry_targets(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.requirement_set_id,
            requirement_set_version=int(self.requirement_set["version"]),
            requirement_tree_sha256=_TREE_SHA256,
            knowledge_base_version=0,
            matrix=matrix,
            source_job_id=str(source_job["id"]),
        )

        self.assertEqual(retry_node_ids, ("node-1",))
        self.assertEqual(metadata["source_failed_count"], 2)
        self.assertEqual(metadata["already_current"], 1)
        self.assertEqual(metadata["skipped"], 0)
        self.assertRegex(metadata["failed_snapshot_sha256"], r"^[0-9a-f]{64}$")

        retry_job, retry_targets = self._claim_job(
            (0,),
            command_id="failed-retry-redelivery-command",
        )
        self._stage_and_publish(
            retry_job,
            retry_targets[0],
            reason="retry job partial success",
        )
        retry_node_ids, retry_metadata = (
            assessment._resolve_failed_analysis_retry_targets(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set_id,
                requirement_set_version=int(self.requirement_set["version"]),
                requirement_tree_sha256=_TREE_SHA256,
                knowledge_base_version=0,
                matrix=matrix,
                source_job_id=str(source_job["id"]),
                exclude_job_id=str(retry_job["id"]),
            )
        )
        self.assertEqual(retry_node_ids, ("node-1",))
        self.assertEqual(retry_metadata["already_current"], 1)

    def test_failed_only_retry_rejects_changed_knowledge_snapshot(self) -> None:
        source_job, source_targets = self._claim_job(
            (0,),
            command_id="failed-source-before-kb-change",
            universe_indexes=(0,),
        )
        self._fail_target(source_job, source_targets[0], retryable=True)
        source_job = finalize_incremental_analysis_job(str(source_job["id"]))
        db.bump_project_knowledge_base_version(self.project["id"])
        matrix = SimpleNamespace(
            nodes=(
                SimpleNamespace(
                    node_id="node-1",
                    requirement_id=self.requirements[0]["id"],
                    matrix_visibility="VISIBLE",
                    disposition="ANALYZE",
                    analysis_eligibility="ELIGIBLE",
                ),
            )
        )

        with self.assertRaises(ApplicationError) as raised:
            assessment._resolve_failed_analysis_retry_targets(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.requirement_set_id,
                requirement_set_version=int(self.requirement_set["version"]),
                requirement_tree_sha256=_TREE_SHA256,
                knowledge_base_version=1,
                matrix=matrix,
                source_job_id=str(source_job["id"]),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "analysis_retry_source_stale",
        )

    def test_stop_expected_job_id_is_a_compare_and_set_guard(self) -> None:
        job, _targets = self._claim_job(
            (0,),
            command_id="stop-cas-command",
            universe_indexes=(0,),
        )

        with patch.object(
            assessment,
            "_finalize_orphaned_ai_analysis_job",
            return_value=job,
        ):
            with self.assertRaises(ApplicationError) as raised:
                assessment.stop_document_ai_analysis(
                    self.project["id"],
                    self.document["id"],
                    StopAIAnalysisRequest(expected_job_id="superseded-job"),
                )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "analysis_stop_job_changed",
        )
        self.assertEqual(
            raised.exception.detail["active_job_id"],
            job["id"],
        )
        self.assertEqual(
            (db.get_ai_analysis_job(str(job["id"])) or {})["status"], "RUNNING"
        )

        def finish_before_cancel(_document_id: str, _job_id: str) -> bool:
            db.update_ai_analysis_job(
                str(job["id"]),
                status="COMPLETED",
                current_stage="completed",
                finished_at="2026-08-08T00:05:00+00:00",
            )
            return True

        with (
            patch.object(
                assessment,
                "_finalize_orphaned_ai_analysis_job",
                return_value=job,
            ),
            patch.object(
                assessment,
                "_has_live_ai_analysis_thread",
                return_value=True,
            ),
            patch.object(
                assessment,
                "_request_ai_analysis_cancel",
                side_effect=finish_before_cancel,
            ),
        ):
            response = assessment.stop_document_ai_analysis(
                self.project["id"],
                self.document["id"],
                StopAIAnalysisRequest(expected_job_id=str(job["id"])),
            )

        self.assertEqual((response.get("job") or {})["status"], "COMPLETED")
        self.assertEqual(
            (db.get_ai_analysis_job(str(job["id"])) or {})["status"],
            "COMPLETED",
        )
        self.assertFalse(
            any(
                event["event_type"] == "job_cancel_requested"
                for event in db.list_ai_analysis_events(str(job["id"]))
            )
        )


if __name__ == "__main__":
    unittest.main()
