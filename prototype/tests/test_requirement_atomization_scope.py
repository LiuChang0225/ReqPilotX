from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import multiprocessing
import time
import unittest
import tempfile
from contextlib import ExitStack, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.adapters.documents.local_requirement_atomization_provider import (
    LocalPassThroughRequirementAtomizationProvider,
)
from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    build_cutover_plan,
)
from app.application.extraction.atomization_scope import (
    RequirementAtomizationScopeExecutionError,
    ScopedRequirementAtomizationProvider,
    execute_requirement_atomization_scope,
    serialize_requirement_atomization_scope_state,
)
from app.application.extraction.contracts import (
    RequirementAtomizationScopeRunRequest,
)
from app.domain.atomization_scope import (
    AtomizationScopeAttemptStatus,
    AtomizationScopeAttemptV1,
    canonical_atomization_scope_attempt_sha256,
    stable_atomization_scope_attempt_id,
    stable_atomization_target_scope_sha256,
)
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationType,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    revise_entry_classification_snapshot,
)
from app.application.extraction.entry_classification_repository import (
    append_entry_classification_snapshot,
    get_latest_entry_classification_snapshot_for_document,
)
from app.application.errors import ApplicationError
from app import db
from app.adapters.persistence.sqlite_requirement_enrichment import (
    SQLiteRequirementEnrichmentRepository,
)
from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.application.extraction.requirement_enrichment import (
    build_requirement_enrichment_lineage,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewSession,
)
from app.domain.extraction.requirement_tree import (
    RequirementTreeMode,
    RequirementTreeNodeV1,
    RequirementTreeSnapshotV1,
    RequirementTreeSourceIdentity,
    RequirementTreeState,
    RequirementTreeStructureIdentity,
)
from app.ports.requirement_atomization_provider import (
    RequirementAtomizationProviderRequest,
    RequirementAtomizationRange,
    RequirementAtomizationSourceSegment,
)
from app.ports.requirement_enrichment_repository import (
    RequirementEnrichmentConflictError,
)
from tests.test_requirement_enrichment import _AtomizationProvider
from tests.test_requirement_enrichment import (
    _coverage,
    _session_with_split_direct_nodes,
)
from tests.test_requirement_enrichment_api import _RecordingAtomizationProvider
from tests.test_llm_requirement_review import _prediction


def _begin_scope_attempt_in_spawned_process(
    database_path: str,
    attempt_payload: dict,
    result_queue,
) -> None:
    repository = SQLiteRequirementEnrichmentRepository(database_path)
    attempt = AtomizationScopeAttemptV1.model_validate(attempt_payload)
    try:
        repository.begin_atomization_scope_attempt(
            attempt=attempt,
            expected_scope_revision=attempt.expected_scope_revision,
        )
    except BaseException as exc:
        result_queue.put((type(exc).__name__, str(exc)))
    else:
        result_queue.put(("OK", ""))


class RequirementAtomizationScopeContractTests(unittest.TestCase):
    def test_python_scope_hash_matches_shared_wire_golden(self) -> None:
        fixture_path = (
            Path(__file__).parents[1]
            / "contracts"
            / "fixtures"
            / "atomization_scope_command_v2.json"
        )
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        hash_payload = dict(fixture["hash_payload"])
        hash_payload.pop("schema_version")
        hash_payload["target_review_node_ids"] = tuple(
            hash_payload["target_review_node_ids"]
        )

        digest = stable_atomization_target_scope_sha256(**hash_payload)

        self.assertEqual(digest, fixture["expected_sha256"])

    def _payload(self, **overrides):
        values = {
            "schema_version": "requirement-atomization-run-request/v2",
            "command_id": "atomize-selected-1",
            "operation": "APPLY",
            "scope": "SELECTED",
            "expected_review_revision": 0,
            "expected_scope_revision": 0,
            "expected_entry_classification_snapshot_id": "accepted-1",
            "expected_entry_classification_revision": 2,
            "expected_entry_classification_sha256": "a" * 64,
            "expected_working_tree_id": "working-tree-1",
            "expected_working_tree_sha256": "b" * 64,
            "expected_effective_structure_id": "effective-structure-1",
            "anchor_tree_node_id": None,
            "target_review_node_ids": ("review-node-1",),
            "target_scope_sha256": "0" * 64,
            "unselected_policy": "SOURCE_BOUND_PASSTHROUGH",
        }
        values.update(overrides)
        values["target_scope_sha256"] = stable_atomization_target_scope_sha256(
            project_id="project-1",
            document_id="document-1",
            session_id="llm-review-" + "1" * 32,
            command_id=values["command_id"],
            operation=values["operation"],
            scope=values["scope"],
            expected_review_revision=values["expected_review_revision"],
            expected_scope_revision=values["expected_scope_revision"],
            expected_entry_classification_snapshot_id=values[
                "expected_entry_classification_snapshot_id"
            ],
            expected_entry_classification_revision=values[
                "expected_entry_classification_revision"
            ],
            expected_entry_classification_sha256=values[
                "expected_entry_classification_sha256"
            ],
            expected_working_tree_id=values["expected_working_tree_id"],
            expected_working_tree_sha256=values["expected_working_tree_sha256"],
            expected_effective_structure_id=values["expected_effective_structure_id"],
            anchor_tree_node_id=values["anchor_tree_node_id"],
            target_review_node_ids=tuple(values["target_review_node_ids"]),
            unselected_policy=values["unselected_policy"],
        )
        return values

    def test_v2_contract_rejects_duplicate_exact_target_ids(self) -> None:
        with self.assertRaises(ValidationError):
            RequirementAtomizationScopeRunRequest.model_validate(
                self._payload(target_review_node_ids=("review-node-1", "review-node-1"))
            )

    def test_v2_contract_accepts_exact_working_tree_identity_and_anchor(self) -> None:
        values = self._payload(
            scope="CURRENT",
            expected_working_tree_id="working-tree-1",
            expected_working_tree_sha256="b" * 64,
            expected_effective_structure_id="effective-structure-1",
            anchor_tree_node_id="tree-node-1",
        )

        payload = RequirementAtomizationScopeRunRequest.model_validate(values)

        self.assertEqual(payload.expected_working_tree_id, "working-tree-1")
        self.assertEqual(payload.anchor_tree_node_id, "tree-node-1")

    def test_target_hash_binds_working_tree_identity_and_anchor(self) -> None:
        common = {
            "project_id": "project-1",
            "document_id": "document-1",
            "session_id": "llm-review-" + "1" * 32,
            "command_id": "atomize-current-1",
            "operation": "APPLY",
            "scope": "CURRENT",
            "expected_review_revision": 0,
            "expected_scope_revision": 0,
            "expected_entry_classification_snapshot_id": "accepted-1",
            "expected_entry_classification_revision": 2,
            "expected_entry_classification_sha256": "a" * 64,
            "expected_working_tree_id": "working-tree-1",
            "expected_working_tree_sha256": "b" * 64,
            "expected_effective_structure_id": "effective-structure-1",
            "anchor_tree_node_id": "tree-node-1",
            "target_review_node_ids": ("review-node-1",),
            "unselected_policy": "SOURCE_BOUND_PASSTHROUGH",
        }

        original = stable_atomization_target_scope_sha256(**common)
        changed = stable_atomization_target_scope_sha256(
            **{**common, "expected_working_tree_sha256": "c" * 64}
        )

        self.assertNotEqual(original, changed)

    def test_v2_contract_rejects_invalid_scope_shapes(self) -> None:
        invalid_payloads = (
            self._payload(
                scope="CURRENT",
                target_review_node_ids=("review-node-1", "review-node-2"),
                anchor_tree_node_id="tree-node-1",
            ),
            self._payload(scope="BRANCH", anchor_tree_node_id=None),
            self._payload(scope="ALL", anchor_tree_node_id="tree-node-1"),
            self._payload(
                operation="UNDO",
                scope="SELECTED",
                target_review_node_ids=(),
            ),
        )

        for values in invalid_payloads:
            with self.subTest(scope=values["scope"], operation=values["operation"]):
                with self.assertRaises(ValidationError):
                    RequirementAtomizationScopeRunRequest.model_validate(values)

    def test_hybrid_provider_never_sends_unselected_target_remote(self) -> None:
        remote = _AtomizationProvider()
        provider = ScopedRequirementAtomizationProvider(
            selected_review_node_ids=frozenset({"review-node-selected"}),
            selected_provider=remote,
            unselected_provider=LocalPassThroughRequirementAtomizationProvider(),
        )

        result = provider.atomize(_request("review-node-unselected"))

        self.assertEqual(remote.call_count, 0)
        self.assertEqual(result.run.request_count, 0)
        self.assertEqual(result.run.status.value, "SKIPPED")
        self.assertEqual(
            provider.pass_through_review_node_ids, ("review-node-unselected",)
        )

    def test_v2_request_dispatches_without_entering_v1_implicit_flow(self) -> None:
        from fastapi import Response
        from app.application.extraction import enrichment_workspace

        payload = RequirementAtomizationScopeRunRequest.model_validate(self._payload())
        expected = {"schema_version": "requirement-atomization-run-response/v2"}
        with patch(
            "app.application.extraction.atomization_scope."
            "create_requirement_atomization_scope_run",
            return_value=expected,
        ) as delegated:
            actual = enrichment_workspace.create_requirement_atomization_run(
                "project-1",
                "document-1",
                "llm-review-" + "1" * 32,
                payload,
                Response(),
            )

        self.assertEqual(actual, expected)
        delegated.assert_called_once()

    def test_real_fastapi_route_accepts_v2_scope_request(self) -> None:
        from app.api.routers.review import build

        app = FastAPI()
        app.include_router(build())
        client = TestClient(app)
        expected = {"schema_version": "requirement-atomization-run-response/v2"}
        url = (
            "/api/projects/project-1/sor-documents/document-1/extraction/"
            "llm-reviews/llm-review-" + "1" * 32 + "/atomization-runs"
        )
        with patch(
            "app.application.extraction.atomization_scope."
            "create_requirement_atomization_scope_run",
            return_value=expected,
        ) as delegated:
            response = client.post(url, json=self._payload())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), expected)
        delegated.assert_called_once()

    def test_postgres_migration_discovers_legacy_unique_and_registers_cutover(
        self,
    ) -> None:
        prototype_dir = Path(__file__).resolve().parents[1]
        migration = (
            prototype_dir
            / "migrations"
            / "postgresql"
            / "0014_explicit_atomization_scope.sql"
        ).read_text(encoding="utf-8")
        running_index_migration = (
            prototype_dir
            / "migrations"
            / "postgresql"
            / "0016_atomization_scope_running_index.sql"
        ).read_text(encoding="utf-8")
        cutover = (
            prototype_dir / "app" / "application" / "cutover" / "sqlite_to_postgres.py"
        ).read_text(encoding="utf-8")

        self.assertIn("FROM pg_constraint", migration)
        self.assertIn("UNNEST(constraint_def.conkey)", migration)
        self.assertIn("atomization_revision", migration)
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS requirement_atomization_scope_receipts",
            migration,
        )
        self.assertIn(
            "CREATE TABLE IF NOT EXISTS requirement_atomization_scope_attempts",
            migration,
        )
        self.assertIn(
            "idx_requirement_atomization_scope_attempt_running",
            running_index_migration,
        )
        self.assertNotIn(
            "requirement_atomization_runs_project_id_document_id_session_id_key",
            migration,
        )
        self.assertGreaterEqual(
            cutover.count('"requirement_atomization_scope_receipts"'),
            3,
        )
        self.assertGreaterEqual(
            cutover.count('"requirement_atomization_scope_attempts"'),
            3,
        )


class RequirementAtomizationScopeExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.project = db.create_project(name="Scope", customer_name="Customer")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=source.name,
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256="b" * 64,
            source_size=source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        prediction = _prediction()
        reviewed = initial_llm_requirement_review(prediction)
        now = datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc)
        session = LlmRequirementReviewSession(
            session_id="llm-review-" + "8" * 32,
            project_id=self.project["id"],
            document_id=self.document["id"],
            prediction=prediction,
            reviewed_result=reviewed,
            delta=evaluate_llm_requirement_review(prediction, reviewed),
            created_at_utc=now,
            updated_at_utc=now,
        )
        self.session = _session_with_split_direct_nodes(session, count=2)
        self.coverage = _coverage(self.session)
        self.direct_ids = tuple(
            item.review_node_id
            for item in self.session.reviewed_result.nodes
            if item.decision_mode.value == "DIRECT"
        )
        self.information_id = "workflow-information-1"
        self.working_tree = _working_tree_for_classification(
            self.session,
            extra_entries=((self.information_id, EntryClassificationType.INFORMATION),),
        )
        self.classification = _classification(
            self.session,
            extra_entries=((self.information_id, EntryClassificationType.INFORMATION),),
            working_tree=self.working_tree,
        )
        initial_classification = build_initial_entry_classification_snapshot(
            self.working_tree
        )
        stored_classification, created = append_entry_classification_snapshot(
            base_snapshot=initial_classification,
            next_snapshot=self.classification,
            command_id="scope-test-classification",
            command_sha256="9" * 64,
            expected_revision=initial_classification.revision,
            expected_snapshot_sha256=initial_classification.payload_sha256,
        )
        self.assertTrue(created)
        self.classification = stored_classification
        self.repository = SQLiteRequirementEnrichmentRepository(db.DB_PATH)

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_apply_accumulates_exact_technical_targets_and_reuses_checkpoints(
        self,
    ) -> None:
        provider = _RecordingAtomizationProvider()
        first_payload = self._payload(
            command_id="scope-apply-1",
            expected_scope_revision=0,
            targets=(self.direct_ids[0], self.information_id),
        )

        first = self._execute(first_payload, provider)

        self.assertEqual(provider.request_review_node_ids, [self.direct_ids[0]])
        self.assertEqual(first.run.revision, 1)
        self.assertEqual(first.receipt.executed_review_node_ids, (self.direct_ids[0],))
        self.assertEqual(
            tuple(item.review_node_id for item in first.receipt.skipped_targets),
            (self.information_id,),
        )
        self.assertEqual(
            first.receipt.pass_through_review_node_ids, (self.direct_ids[1],)
        )
        self.assertEqual(first.receipt.provider_call_count, 1)
        self.assertEqual(first.receipt.checkpoint_created_count, 1)

        second_payload = self._payload(
            command_id="scope-apply-2",
            expected_scope_revision=1,
            targets=(self.direct_ids[1],),
        )
        second = self._execute(second_payload, provider)

        self.assertEqual(
            provider.request_review_node_ids,
            [self.direct_ids[0], self.direct_ids[1]],
        )
        self.assertEqual(second.receipt.scope_revision, 2)
        self.assertEqual(second.receipt.new_review_node_ids, (self.direct_ids[1],))
        self.assertEqual(second.receipt.reused_review_node_ids, ())
        self.assertEqual(second.receipt.cumulative_review_node_ids, self.direct_ids)
        self.assertEqual(second.receipt.provider_call_count, 1)
        self.assertEqual(second.receipt.checkpoint_reuse_count, 1)
        self.assertEqual(second.receipt.pass_through_review_node_ids, ())

        first_atom = next(
            item
            for item in first.run.atomic_nodes
            if item.review_node_id == self.direct_ids[0]
        )
        second_atom = next(
            item
            for item in second.run.atomic_nodes
            if item.review_node_id == self.direct_ids[0]
        )
        self.assertEqual(first_atom.node_id, second_atom.node_id)
        self.assertEqual(first_atom.atomic_statement, second_atom.atomic_statement)

        replay = self._execute(second_payload, provider)
        self.assertTrue(replay.replayed)
        self.assertEqual(replay.receipt, second.receipt)
        self.assertEqual(tuple(provider.request_review_node_ids), self.direct_ids)

    def test_concurrent_attempt_audit_ids_follow_canonical_source_order(self) -> None:
        second_stored = Event()
        first_review_node_id = self.direct_ids[0]
        original_store = self.repository.create_or_get_atomization_checkpoint

        class ReverseCheckpointRepository:
            def create_or_get_atomization_checkpoint(_, **kwargs):
                request = kwargs["request"]
                if request.review_node_id == first_review_node_id:
                    if not second_stored.wait(timeout=2):
                        raise AssertionError("second checkpoint was not stored")
                    return original_store(**kwargs)
                stored = original_store(**kwargs)
                second_stored.set()
                return stored

            def __getattr__(_, name):
                return getattr(self.repository, name)

        execute_requirement_atomization_scope(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            working_tree=self.working_tree,
            payload=self._payload(
                command_id="scope-canonical-attempt-audit-order",
                expected_scope_revision=0,
                targets=self.direct_ids,
            ),
            repository=ReverseCheckpointRepository(),
            remote_provider=_RecordingAtomizationProvider(),
            max_concurrent_provider_calls=2,
        )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual(attempt.executed_review_node_ids, self.direct_ids)
        self.assertEqual(attempt.completed_review_node_ids, self.direct_ids)
        self.assertEqual(
            attempt.checkpoint_created_review_node_ids,
            self.direct_ids,
        )

    def test_second_repository_cannot_begin_same_revision_while_attempt_runs(
        self,
    ) -> None:
        first_provider_entered = Event()
        release_first_provider = Event()
        first_error: list[BaseException] = []
        first_result = []

        class BlockingProvider(_RecordingAtomizationProvider):
            def atomize(provider, request):
                first_provider_entered.set()
                if not release_first_provider.wait(timeout=3):
                    raise AssertionError("first provider was not released")
                return super().atomize(request)

        def run_first() -> None:
            try:
                first_result.append(
                    execute_requirement_atomization_scope(
                        project_id=self.project["id"],
                        document_id=self.document["id"],
                        session=self.session,
                        coverage=self.coverage,
                        classification_snapshot=self.classification,
                        working_tree=self.working_tree,
                        payload=self._payload(
                            command_id="scope-running-first-repository",
                            expected_scope_revision=0,
                            targets=(self.direct_ids[0],),
                        ),
                        repository=SQLiteRequirementEnrichmentRepository(db.DB_PATH),
                        remote_provider=BlockingProvider(),
                        max_concurrent_provider_calls=1,
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                first_error.append(exc)

        worker = Thread(target=run_first, daemon=True)
        worker.start()
        self.assertTrue(first_provider_entered.wait(timeout=3))
        second_provider = _RecordingAtomizationProvider()
        try:
            with self.assertRaisesRegex(
                RequirementEnrichmentConflictError,
                "attempt is already running",
            ):
                execute_requirement_atomization_scope(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    session=self.session,
                    coverage=self.coverage,
                    classification_snapshot=self.classification,
                    working_tree=self.working_tree,
                    payload=self._payload(
                        command_id="scope-running-second-repository",
                        expected_scope_revision=0,
                        targets=(self.direct_ids[1],),
                    ),
                    repository=SQLiteRequirementEnrichmentRepository(db.DB_PATH),
                    remote_provider=second_provider,
                    max_concurrent_provider_calls=1,
                )
        finally:
            release_first_provider.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(len(first_result), 1)
        self.assertEqual(second_provider.request_review_node_ids, [])

    def test_heartbeat_keeps_blocked_provider_lease_fresh_past_stale_window(
        self,
    ) -> None:
        from app.adapters.persistence import sql_requirement_enrichment

        provider_entered = Event()
        release_provider = Event()
        heartbeat_observed = Event()
        first_error: list[BaseException] = []
        first_result = []
        heartbeat_count = 0
        heartbeat_lock = multiprocessing.Lock()

        class BlockingProvider(_RecordingAtomizationProvider):
            def atomize(provider, request):
                provider_entered.set()
                if not release_provider.wait(timeout=3):
                    raise AssertionError("provider was not released")
                return super().atomize(request)

        repository = SQLiteRequirementEnrichmentRepository(db.DB_PATH)

        class ObservedHeartbeatRepository:
            def heartbeat_atomization_scope_attempt(_, **kwargs):
                nonlocal heartbeat_count
                refreshed = repository.heartbeat_atomization_scope_attempt(**kwargs)
                with heartbeat_lock:
                    heartbeat_count += 1
                    if heartbeat_count >= 3:
                        heartbeat_observed.set()
                return refreshed

            def __getattr__(_, name):
                return getattr(repository, name)

        def run_first() -> None:
            try:
                first_result.append(
                    execute_requirement_atomization_scope(
                        project_id=self.project["id"],
                        document_id=self.document["id"],
                        session=self.session,
                        coverage=self.coverage,
                        classification_snapshot=self.classification,
                        working_tree=self.working_tree,
                        payload=self._payload(
                            command_id="scope-heartbeat-blocked-provider",
                            expected_scope_revision=0,
                            targets=(self.direct_ids[0],),
                        ),
                        repository=ObservedHeartbeatRepository(),
                        remote_provider=BlockingProvider(),
                        max_concurrent_provider_calls=1,
                        lease_heartbeat_interval_seconds=0.01,
                    )
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                first_error.append(exc)

        with patch.object(
            sql_requirement_enrichment,
            "_ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER",
            timedelta(seconds=0.1),
        ):
            started = time.monotonic()
            worker = Thread(target=run_first, daemon=True)
            worker.start()
            self.assertTrue(provider_entered.wait(timeout=3))
            self.assertTrue(heartbeat_observed.wait(timeout=3))
            remaining = 0.25 - (time.monotonic() - started)
            if remaining > 0:
                Event().wait(remaining)
            second_provider = _RecordingAtomizationProvider()
            try:
                with self.assertRaisesRegex(
                    RequirementEnrichmentConflictError,
                    "attempt is already running",
                ):
                    execute_requirement_atomization_scope(
                        project_id=self.project["id"],
                        document_id=self.document["id"],
                        session=self.session,
                        coverage=self.coverage,
                        classification_snapshot=self.classification,
                        working_tree=self.working_tree,
                        payload=self._payload(
                            command_id="scope-heartbeat-second-owner",
                            expected_scope_revision=0,
                            targets=(self.direct_ids[1],),
                        ),
                        repository=SQLiteRequirementEnrichmentRepository(
                            db.DB_PATH
                        ),
                        remote_provider=second_provider,
                        max_concurrent_provider_calls=1,
                    )
            finally:
                release_provider.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(len(first_result), 1)
        self.assertEqual(second_provider.request_review_node_ids, [])

    def test_lost_attempt_owner_cannot_finalize_scope_revision(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-fencing-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        finalize_entered = Event()
        release_finalize = Event()
        execution_error: list[BaseException] = []
        original_finalize = self.repository.create_atomization_scope_revision

        class BlockingFinalizeRepository:
            def create_atomization_scope_revision(_, **kwargs):
                finalize_entered.set()
                if not release_finalize.wait(timeout=3):
                    raise AssertionError("finalize was not released")
                return original_finalize(**kwargs)

            def __getattr__(_, name):
                return getattr(self.repository, name)

        def run_losing_owner() -> None:
            try:
                execute_requirement_atomization_scope(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    session=self.session,
                    coverage=self.coverage,
                    classification_snapshot=self.classification,
                    working_tree=self.working_tree,
                    payload=self._payload(
                        command_id="scope-fencing-losing-owner",
                        expected_scope_revision=1,
                        targets=(self.direct_ids[1],),
                    ),
                    repository=BlockingFinalizeRepository(),
                    remote_provider=_RecordingAtomizationProvider(),
                    max_concurrent_provider_calls=1,
                    lease_heartbeat_interval_seconds=0.01,
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                execution_error.append(exc)

        worker = Thread(target=run_losing_owner, daemon=True)
        worker.start()
        self.assertTrue(finalize_entered.wait(timeout=3))
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        running = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(running)
        assert running is not None
        self._interrupt_running_attempt(
            running,
            error_code="FENCING_OWNER_REPLACED",
        )
        release_finalize.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(execution_error), 1)
        self.assertIsInstance(
            execution_error[0],
            RequirementEnrichmentConflictError,
        )
        with db.get_conn() as connection:
            receipt_count = connection.execute(
                "SELECT COUNT(*) AS count FROM requirement_atomization_scope_receipts"
            ).fetchone()
            run_count = connection.execute(
                "SELECT COUNT(*) AS count FROM requirement_atomization_runs"
            ).fetchone()
        self.assertEqual(int(receipt_count["count"]), 1)
        self.assertEqual(int(run_count["count"]), 1)

    def test_lost_attempt_owner_cannot_store_provider_checkpoint(self) -> None:
        provider_entered = Event()
        release_provider = Event()
        execution_error: list[BaseException] = []

        class BlockingProvider(_RecordingAtomizationProvider):
            def atomize(provider, request):
                provider_entered.set()
                if not release_provider.wait(timeout=3):
                    raise AssertionError("provider was not released")
                return super().atomize(request)

        def run_losing_owner() -> None:
            try:
                self._execute(
                    self._payload(
                        command_id="scope-checkpoint-losing-owner",
                        expected_scope_revision=0,
                        targets=(self.direct_ids[0],),
                    ),
                    BlockingProvider(),
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                execution_error.append(exc)

        worker = Thread(target=run_losing_owner, daemon=True)
        worker.start()
        self.assertTrue(provider_entered.wait(timeout=3))
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        running = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(running)
        assert running is not None
        self._interrupt_running_attempt(
            running,
            error_code="FENCING_CHECKPOINT_OWNER_REPLACED",
        )
        release_provider.set()
        worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(len(execution_error), 1)
        self.assertIsInstance(
            execution_error[0],
            RequirementEnrichmentConflictError,
        )
        with db.get_conn() as connection:
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) AS count FROM requirement_atomization_checkpoints"
            ).fetchone()
            receipt_count = connection.execute(
                "SELECT COUNT(*) AS count FROM requirement_atomization_scope_receipts"
            ).fetchone()
        self.assertEqual(int(checkpoint_count["count"]), 0)
        self.assertEqual(int(receipt_count["count"]), 0)
        persisted = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(persisted)
        assert persisted is not None
        self.assertEqual(
            persisted.error_code,
            "FENCING_CHECKPOINT_OWNER_REPLACED",
        )

    def test_fresh_abandoned_running_attempt_blocks_retry_before_provider(
        self,
    ) -> None:
        first_provider = _RecordingAtomizationProvider()
        with (
            patch.object(
                first_provider,
                "atomize",
                side_effect=KeyboardInterrupt("simulated process exit"),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-fresh-running-first",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                first_provider,
            )

        retry_provider = _RecordingAtomizationProvider()
        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "attempt is already running",
        ):
            self._execute(
                self._payload(
                    command_id="scope-fresh-running-retry",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                retry_provider,
            )

        self.assertEqual(retry_provider.request_review_node_ids, [])

    def test_spawned_process_cannot_begin_second_running_attempt(self) -> None:
        provider = _RecordingAtomizationProvider()
        with (
            patch.object(
                provider,
                "atomize",
                side_effect=KeyboardInterrupt("simulated process exit"),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-running-multiprocess",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                provider,
            )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        first_attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(first_attempt)
        assert first_attempt is not None
        fields = first_attempt.model_dump(mode="python")
        fields.update(
            {
                "attempt_sequence": 2,
                "attempt_id": stable_atomization_scope_attempt_id(
                    lineage_sha256=first_attempt.lineage_sha256,
                    attempt_sequence=2,
                    command_sha256=first_attempt.command_sha256,
                ),
                "payload_sha256": "0" * 64,
                "started_at_utc": datetime.now(timezone.utc),
                "updated_at_utc": datetime.now(timezone.utc),
            }
        )
        draft = AtomizationScopeAttemptV1.model_construct(**fields)
        fields["payload_sha256"] = canonical_atomization_scope_attempt_sha256(
            draft
        )
        second_attempt = AtomizationScopeAttemptV1.model_validate(fields)

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        worker = context.Process(
            target=_begin_scope_attempt_in_spawned_process,
            args=(
                str(db.DB_PATH),
                second_attempt.model_dump(mode="json"),
                result_queue,
            ),
        )
        worker.start()
        worker.join(timeout=8)
        if worker.is_alive():
            worker.terminate()
            worker.join(timeout=2)
            self.fail("spawned repository process did not terminate")

        self.assertEqual(worker.exitcode, 0)
        self.assertEqual(
            result_queue.get(timeout=2),
            (
                "RequirementEnrichmentConflictError",
                "atomization scope attempt is already running",
            ),
        )

    def test_stale_running_attempt_is_interrupted_before_retry_begins(self) -> None:
        first_provider = _RecordingAtomizationProvider()
        original_store = self.repository.create_or_get_atomization_checkpoint

        def store_then_process_exit(**kwargs):
            original_store(**kwargs)
            raise KeyboardInterrupt("simulated process exit after checkpoint")

        with (
            patch.object(
                self.repository,
                "create_or_get_atomization_checkpoint",
                side_effect=store_then_process_exit,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-stale-running-first",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                first_provider,
            )
        self._age_scope_attempt(attempt_sequence=1)

        retry_provider = _RecordingAtomizationProvider()
        result = self._execute(
            self._payload(
                command_id="scope-stale-running-retry",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            retry_provider,
        )

        self.assertEqual(result.receipt.scope_revision, 1)
        with db.get_conn() as connection:
            rows = connection.execute(
                """
                SELECT status, payload_json
                FROM requirement_atomization_scope_attempts
                ORDER BY attempt_sequence
                """
            ).fetchall()
        self.assertEqual(
            [str(row["status"]) for row in rows],
            ["INTERRUPTED", "SUCCEEDED"],
        )
        interrupted = AtomizationScopeAttemptV1.model_validate_json(
            str(rows[0]["payload_json"])
        )
        self.assertTrue(interrupted.retryable)
        self.assertTrue(interrupted.checkpoint_resumable)
        self.assertEqual(
            interrupted.completed_review_node_ids,
            (self.direct_ids[0],),
        )
        self.assertEqual(interrupted.error_code, "PROCESS_INTERRUPTED")
        self.assertEqual(retry_provider.request_review_node_ids, [])

    def test_undo_restores_previous_active_scope_without_remote_call(self) -> None:
        provider = _RecordingAtomizationProvider()
        first = self._execute(
            self._payload(
                command_id="scope-undo-base-1",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )
        self._execute(
            self._payload(
                command_id="scope-undo-base-2",
                expected_scope_revision=1,
                targets=(self.direct_ids[1],),
            ),
            provider,
        )
        calls_before = tuple(provider.request_review_node_ids)

        undone = self._execute(
            self._payload(
                command_id="scope-undo-1",
                operation="UNDO",
                scope="CURRENT",
                expected_scope_revision=2,
                targets=(),
            ),
            provider,
        )

        self.assertEqual(tuple(provider.request_review_node_ids), calls_before)
        self.assertEqual(
            undone.receipt.cumulative_review_node_ids,
            first.receipt.cumulative_review_node_ids,
        )
        self.assertEqual(undone.receipt.provider_call_count, 0)
        self.assertEqual(
            undone.receipt.pass_through_review_node_ids, (self.direct_ids[1],)
        )

    def test_undo_retry_respects_running_lease_and_reclaims_it_when_stale(
        self,
    ) -> None:
        provider = _RecordingAtomizationProvider()
        applied = self._execute(
            self._payload(
                command_id="scope-undo-running-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )
        provider_calls_before_undo = tuple(provider.request_review_node_ids)

        class ExitBeforeScopeCommit:
            def create_atomization_scope_revision(_, **_kwargs):
                raise KeyboardInterrupt("simulated process exit before scope commit")

            def __getattr__(_, name):
                return getattr(self.repository, name)

        undo_payload = self._payload(
            command_id="scope-undo-running-first",
            operation="UNDO",
            scope="CURRENT",
            expected_scope_revision=applied.receipt.scope_revision,
            targets=(),
        )
        with self.assertRaises(KeyboardInterrupt):
            execute_requirement_atomization_scope(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session=self.session,
                coverage=self.coverage,
                classification_snapshot=self.classification,
                working_tree=self.working_tree,
                payload=undo_payload,
                repository=ExitBeforeScopeCommit(),
                remote_provider=provider,
                max_concurrent_provider_calls=1,
            )

        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "attempt is already running",
        ):
            self._execute(
                self._payload(
                    command_id="scope-undo-running-fresh-retry",
                    operation="UNDO",
                    scope="CURRENT",
                    expected_scope_revision=applied.receipt.scope_revision,
                    targets=(),
                ),
                provider,
            )

        self._age_scope_attempt(attempt_sequence=2)
        undone = self._execute(
            self._payload(
                command_id="scope-undo-running-stale-retry",
                operation="UNDO",
                scope="CURRENT",
                expected_scope_revision=applied.receipt.scope_revision,
                targets=(),
            ),
            provider,
        )

        self.assertEqual(undone.receipt.active_revision, 0)
        self.assertEqual(
            tuple(provider.request_review_node_ids),
            provider_calls_before_undo,
        )

    def test_partial_failure_is_resumable_and_retry_only_calls_missing_target(
        self,
    ) -> None:
        provider = _RecordingAtomizationProvider(fail_review_node_id=self.direct_ids[1])
        payload = self._payload(
            command_id="scope-partial-retry",
            expected_scope_revision=0,
            targets=self.direct_ids,
        )

        with self.assertRaises(RequirementAtomizationScopeExecutionError) as failed:
            self._execute(payload, provider)

        self.assertEqual(
            failed.exception.completed_review_node_ids,
            (self.direct_ids[0],),
        )
        self.assertEqual(
            failed.exception.failed_review_node_ids,
            (self.direct_ids[1],),
        )
        self.assertTrue(failed.exception.retryable)
        self.assertTrue(failed.exception.checkpoint_resumable)
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual(attempt.status.value, "INTERRUPTED")
        self.assertTrue(attempt.retryable)
        self.assertTrue(attempt.checkpoint_resumable)
        self.assertEqual(attempt.completed_review_node_ids, (self.direct_ids[0],))
        self.assertEqual(attempt.failed_review_node_ids, (self.direct_ids[1],))
        failed_state = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            repository=self.repository,
        )
        self.assertEqual(failed_state["last_attempt"]["status"], "INTERRUPTED")
        self.assertEqual(
            failed_state["last_attempt"]["completed_review_node_ids"],
            [self.direct_ids[0]],
        )
        self.assertEqual(
            failed_state["last_attempt"]["failed_review_node_ids"],
            [self.direct_ids[1]],
        )
        self.assertTrue(failed_state["last_attempt"]["retryable"])
        self.assertTrue(failed_state["last_attempt"]["checkpoint_resumable"])
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        with patch.object(
            RUNTIME_STATE,
            "test_requirement_enrichment_repository_factory",
            lambda: self.repository,
        ):
            legacy_state = enrichment_workspace._serialize_requirement_enrichment_state(
                self.session,
                coverage=self.coverage,
            )
        self.assertTrue(legacy_state["atomization_progress"]["resumable"])
        self.assertIsNone(
            self.repository.get_latest_atomization_scope_receipt(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                lineage_sha256=lineage.lineage_sha256,
            )
        )
        retried = self._execute(payload, provider)
        self.assertEqual(
            provider.request_review_node_ids,
            [self.direct_ids[0], self.direct_ids[1], self.direct_ids[1]],
        )
        self.assertEqual(retried.receipt.provider_call_count, 1)
        self.assertEqual(retried.receipt.checkpoint_reuse_count, 1)
        self.assertEqual(retried.receipt.checkpoint_created_count, 1)
        self.assertFalse(retried.receipt.resumable)
        succeeded_attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(succeeded_attempt)
        assert succeeded_attempt is not None
        self.assertEqual(succeeded_attempt.status.value, "SUCCEEDED")

    def test_first_target_failure_is_retryable_without_checkpoint_resume(self) -> None:
        provider = _RecordingAtomizationProvider(fail_review_node_id=self.direct_ids[0])

        with self.assertRaises(RequirementAtomizationScopeExecutionError) as failed:
            self._execute(
                self._payload(
                    command_id="scope-first-target-failure",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                provider,
            )

        self.assertTrue(failed.exception.retryable)
        self.assertFalse(failed.exception.checkpoint_resumable)

    def test_interruption_http_resumable_means_checkpoint_resume(self) -> None:
        from fastapi import Response
        from app.application.extraction import atomization_scope
        from app.application.extraction import enrichment_workspace

        payload = self._payload(
            command_id="scope-http-first-failure",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
        )
        interrupted = RequirementAtomizationScopeExecutionError(
            "provider unavailable",
            requested_review_node_ids=(self.direct_ids[0],),
            executed_review_node_ids=(self.direct_ids[0],),
            skipped_targets=(),
            failed_review_node_ids=(self.direct_ids[0],),
            completed_review_node_ids=(),
            provider_call_count=1,
            checkpoint_created_count=0,
            checkpoint_reuse_count=0,
            retryable=True,
            checkpoint_resumable=False,
        )
        with (
            patch.object(
                enrichment_workspace,
                "_reject_requirement_enrichment_during_structure_optimization",
            ),
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=({}, self.session),
            ),
            patch.object(
                enrichment_workspace,
                "_require_current_requirement_enrichment_coverage",
                return_value=self.coverage,
            ),
            patch.object(
                atomization_scope,
                "load_current_entry_classification_context",
                return_value=(self.working_tree, self.classification),
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_enrichment_repository",
                return_value=self.repository,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_atomization_lineage_lock",
                return_value=nullcontext(),
            ),
            patch.object(enrichment_workspace, "_set_requirement_atomization_active"),
            patch.object(
                atomization_scope,
                "execute_requirement_atomization_scope",
                side_effect=interrupted,
            ),
        ):
            with self.assertRaises(ApplicationError) as raised:
                atomization_scope.create_requirement_atomization_scope_run(
                    self.project["id"],
                    self.document["id"],
                    self.session.session_id,
                    payload,
                    Response(),
                )

        self.assertTrue(raised.exception.detail["retryable"])
        self.assertFalse(raised.exception.detail["checkpoint_resumable"])
        self.assertFalse(raised.exception.detail["resumable"])

    def test_failed_command_id_cannot_be_reused_with_different_target_hash(
        self,
    ) -> None:
        command_id = "scope-failed-command-id"
        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id=command_id,
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                _RecordingAtomizationProvider(fail_review_node_id=self.direct_ids[0]),
            )
        replacement_provider = _RecordingAtomizationProvider()

        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "command_id was already used with different input",
        ):
            self._execute(
                self._payload(
                    command_id=command_id,
                    expected_scope_revision=0,
                    targets=(self.direct_ids[1],),
                ),
                replacement_provider,
            )

        self.assertEqual(replacement_provider.request_review_node_ids, [])

    def test_failed_refresh_is_not_hidden_by_an_older_completed_scope(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-completed-before-refresh",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        failing = _RecordingAtomizationProvider(fail_review_node_id=self.direct_ids[1])

        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id="scope-failed-refresh",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                failing,
            )

        state = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            repository=self.repository,
        )
        self.assertEqual(state["current_scope_revision"], 1)
        self.assertEqual(state["last_attempt"]["status"], "INTERRUPTED")
        self.assertTrue(state["last_attempt"]["retryable"])
        self.assertFalse(state["last_attempt"]["checkpoint_resumable"])
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        with patch.object(
            RUNTIME_STATE,
            "test_requirement_enrichment_repository_factory",
            lambda: self.repository,
        ):
            legacy_state = enrichment_workspace._serialize_requirement_enrichment_state(
                self.session,
                coverage=self.coverage,
            )
        self.assertEqual(legacy_state["atomization_progress"]["state"], "PARTIAL")
        self.assertFalse(legacy_state["atomization_progress"]["resumable"])
        self.assertEqual(
            legacy_state["atomization_progress"]["completed_direct_node_count"],
            0,
        )

    def test_invalid_range_is_never_checkpointed_and_retry_can_refetch(self) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.review_node_id == self.direct_ids[0]
        )
        span = direct.source_spans[0]
        payload = self._payload(
            command_id="scope-invalid-range-retry",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
        )
        invalid = _AtomizationProvider(
            invalid_ranges=(
                RequirementAtomizationRange(
                    segment_index=0,
                    start_offset=0,
                    end_offset=(span.end_offset - span.start_offset) + 10,
                ),
            )
        )

        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(payload, invalid)

        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        self.assertEqual(
            self.repository.count_atomization_checkpoints(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                review_revision=self.session.reviewed_result.revision,
                lineage_sha256=lineage.lineage_sha256,
            ),
            0,
        )
        retried = self._execute(payload, _RecordingAtomizationProvider())
        self.assertEqual(retried.receipt.provider_call_count, 1)

    def test_stored_checkpoint_is_revalidated_before_attempt_marks_complete(
        self,
    ) -> None:
        original_store = self.repository.create_or_get_atomization_checkpoint

        def return_corrupt_stored_checkpoint(**kwargs):
            stored = original_store(**kwargs)
            obligation = stored.obligations[0]
            corrupt = obligation.model_copy(
                update={
                    "source_ranges": (
                        RequirementAtomizationRange(
                            segment_index=0,
                            start_offset=0,
                            end_offset=10_000_000,
                        ),
                    )
                }
            )
            return stored.model_copy(update={"obligations": (corrupt,)})

        with (
            patch.object(
                self.repository,
                "create_or_get_atomization_checkpoint",
                side_effect=return_corrupt_stored_checkpoint,
            ),
            self.assertRaises(RequirementAtomizationScopeExecutionError) as failed,
        ):
            self._execute(
                self._payload(
                    command_id="scope-stored-checkpoint-revalidate",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                _RecordingAtomizationProvider(),
            )

        self.assertEqual(failed.exception.completed_review_node_ids, ())
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual(attempt.checkpoint_created_review_node_ids, ())
        self.assertFalse(attempt.checkpoint_resumable)

    def test_current_target_tampered_checkpoint_envelope_is_deleted_and_refetched(
        self,
    ) -> None:
        self._execute(
            self._payload(
                command_id="scope-envelope-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_atomization_checkpoints
                SET payload_sha256 = ?
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                ("0" * 64, lineage.lineage_sha256, self.direct_ids[0]),
            )
        provider = _RecordingAtomizationProvider()

        refreshed = self._execute(
            self._payload(
                command_id="scope-envelope-refresh",
                expected_scope_revision=1,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )

        self.assertEqual(provider.request_review_node_ids, [self.direct_ids[0]])
        self.assertEqual(refreshed.receipt.checkpoint_created_count, 1)
        attempt = self.repository.get_latest_atomization_scope_attempt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )
        self.assertIsNotNone(attempt)
        assert attempt is not None
        self.assertEqual(
            attempt.checkpoint_invalidated_review_node_ids,
            (self.direct_ids[0],),
        )

    def test_historical_target_missing_checkpoint_fails_closed(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-history-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                DELETE FROM requirement_atomization_checkpoints
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                (lineage.lineage_sha256, self.direct_ids[0]),
            )
        provider = _RecordingAtomizationProvider()

        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id="scope-history-next",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                provider,
            )

        self.assertNotIn(self.direct_ids[0], provider.request_review_node_ids)

    def test_historical_target_invalid_checkpoint_fails_closed_without_remote(
        self,
    ) -> None:
        self._execute(
            self._payload(
                command_id="scope-history-invalid-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM requirement_atomization_checkpoints
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                (lineage.lineage_sha256, self.direct_ids[0]),
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["obligations"][0]["source_ranges"][0]["end_offset"] += 10_000
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE requirement_atomization_checkpoints
                SET payload_json = ?, payload_sha256 = ?
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                (
                    payload_json,
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    lineage.lineage_sha256,
                    self.direct_ids[0],
                ),
            )
        provider = _RecordingAtomizationProvider()

        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id="scope-history-invalid-next",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                provider,
            )

        self.assertEqual(provider.request_review_node_ids, [])

    def test_historical_target_tampered_checkpoint_envelope_fails_closed_remote_zero(
        self,
    ) -> None:
        self._execute(
            self._payload(
                command_id="scope-history-envelope-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_atomization_checkpoints
                SET payload_sha256 = ?
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                ("0" * 64, lineage.lineage_sha256, self.direct_ids[0]),
            )
        provider = _RecordingAtomizationProvider()

        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id="scope-history-envelope-next",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                provider,
            )

        self.assertEqual(provider.request_review_node_ids, [])
        with db.get_conn() as connection:
            checkpoint = connection.execute(
                """
                SELECT payload_sha256 FROM requirement_atomization_checkpoints
                WHERE lineage_sha256 = ? AND review_node_id = ?
                """,
                (lineage.lineage_sha256, self.direct_ids[0]),
            ).fetchone()
        self.assertEqual(str(checkpoint["payload_sha256"]), "0" * 64)

    def test_undo_first_scope_returns_to_zero_without_remote_call(self) -> None:
        provider = _RecordingAtomizationProvider()
        self._execute(
            self._payload(
                command_id="scope-undo-zero-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )
        calls_before = tuple(provider.request_review_node_ids)

        undone = self._execute(
            self._payload(
                command_id="scope-undo-zero",
                operation="UNDO",
                scope="CURRENT",
                expected_scope_revision=1,
                targets=(),
            ),
            provider,
        )

        self.assertEqual(tuple(provider.request_review_node_ids), calls_before)
        self.assertEqual(undone.receipt.active_revision, 0)
        self.assertEqual(undone.receipt.cumulative_review_node_ids, ())
        self.assertEqual(undone.receipt.provider_call_count, 0)

    def test_scope_state_marks_content_equivalent_and_stale_classification(
        self,
    ) -> None:
        provider = _RecordingAtomizationProvider()
        applied = self._execute(
            self._payload(
                command_id="scope-state-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )
        current = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            repository=self.repository,
        )
        self.assertEqual(current["classification_binding_state"], "CURRENT")
        self.assertEqual(current["current_scope_revision"], 1)
        self.assertEqual(current["eligible_review_node_ids"], list(self.direct_ids))
        self.assertEqual(current["eligible_review_node_count"], 2)
        self.assertEqual(
            current["current_atomization_run_id"],
            applied.run.atomization_run_id,
        )
        self.assertEqual(
            current["current_receipt"]["receipt_id"],
            applied.receipt.receipt_id,
        )

        accepted = accept_entry_classification_snapshot(
            self.classification,
            snapshot_id="accepted-scope-1",
            accepted_at="2026-08-07T09:00:00+00:00",
            accepted_by="LOCAL_USER",
        )
        equivalent = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=accepted,
            repository=self.repository,
        )
        self.assertEqual(
            equivalent["classification_binding_state"],
            "CONTENT_EQUIVALENT",
        )

        revised = revise_entry_classification_snapshot(
            self.classification,
            overrides={self.direct_ids[0]: EntryClassificationType.NON_TECHNICAL},
            snapshot_id="working-scope-2",
            created_at="2026-08-07T09:05:00+00:00",
        )
        stale = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=revised,
            repository=self.repository,
        )
        self.assertEqual(stale["classification_binding_state"], "STALE")
        self.assertFalse(stale["capability"]["read_has_side_effects"])

    def test_classification_get_is_bound_to_exact_current_tree_lineage(self) -> None:
        other_tree = self.working_tree.model_copy(
            update={
                "structure_identity": self.working_tree.structure_identity.model_copy(
                    update={
                        "effective_snapshot_id": "effective-structure-other",
                        "effective_tree_sha256": "e" * 64,
                    }
                )
            }
        )
        other_initial = build_initial_entry_classification_snapshot(other_tree)
        other = revise_entry_classification_snapshot(
            other_initial,
            overrides={
                item.workflow_node_id: item.effective_type
                for item in self.classification.entries
            },
            snapshot_id="working-scope-other-tree",
            created_at="2026-08-07T12:00:00+00:00",
        )
        append_entry_classification_snapshot(
            base_snapshot=other_initial,
            next_snapshot=other,
            command_id="scope-test-other-tree-classification",
            command_sha256="7" * 64,
            expected_revision=0,
            expected_snapshot_sha256=other_initial.payload_sha256,
        )
        alternate_lineages = (
            ("semantic-other", self.classification.policy_version, "6" * 64),
            ("", "entry-classification-policy/noncanonical", "5" * 64),
        )
        for index, (semantic_snapshot_id, policy_version, command_sha256) in enumerate(
            alternate_lineages,
            start=1,
        ):
            alternate_initial = build_initial_entry_classification_snapshot(
                self.working_tree,
                semantic_snapshot_id=semantic_snapshot_id,
                policy_version=policy_version,
            )
            alternate = revise_entry_classification_snapshot(
                alternate_initial,
                overrides={
                    item.workflow_node_id: item.effective_type
                    for item in self.classification.entries
                },
                snapshot_id=f"working-scope-alternate-lineage-{index}",
                created_at=f"2026-08-07T1{2 + index}:00:00+00:00",
            )
            append_entry_classification_snapshot(
                base_snapshot=alternate_initial,
                next_snapshot=alternate,
                command_id=f"scope-test-alternate-lineage-{index}",
                command_sha256=command_sha256,
                expected_revision=0,
                expected_snapshot_sha256=alternate_initial.payload_sha256,
            )

        exact = get_latest_entry_classification_snapshot_for_document(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.classification.source_sha256,
            working_tree_id=self.classification.working_tree_id,
            working_tree_sha256=self.classification.working_tree_sha256,
            effective_structure_id=self.classification.effective_structure_id,
            semantic_snapshot_id=self.classification.semantic_snapshot_id,
            policy_version=self.classification.policy_version,
        )

        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.snapshot_id, self.classification.snapshot_id)

    def test_get_state_never_rebuilds_structure_or_writes_scope_rows(self) -> None:
        from fastapi import Response
        from app.application.extraction import atomization_scope
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        table_names = (
            "entry_classification_snapshots",
            "requirement_atomization_runs",
            "requirement_atomization_scope_receipts",
            "requirement_atomization_scope_attempts",
        )

        def counts() -> tuple[int, ...]:
            with db.get_conn() as connection:
                return tuple(
                    int(
                        connection.execute(
                            f"SELECT COUNT(*) AS row_count FROM {table_name}"
                        ).fetchone()["row_count"]
                    )
                    for table_name in table_names
                )

        before = counts()
        with (
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=({}, self.session),
            ),
            patch.object(
                enrichment_workspace,
                "_load_current_requirement_enrichment_coverage",
                return_value=self.coverage,
            ),
            patch.object(
                RUNTIME_STATE,
                "test_requirement_enrichment_repository_factory",
                lambda: self.repository,
            ),
            patch(
                "app.application.extraction.entry_classification."
                "_read_current_requirement_tree",
                side_effect=AssertionError("GET rebuilt Structure"),
            ),
            patch.object(
                atomization_scope,
                "_load_current_requirement_tree_for_state_read",
                return_value=self.working_tree,
                create=True,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_refinement_provider_available",
                return_value=False,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_atomization_provider_available",
                return_value=False,
            ),
            patch(
                "app.application.extraction.document_jobs.start_document_extraction"
            ) as start_extraction,
            patch(
                "app.application.extraction.document_jobs.start_llm_document_extraction"
            ) as start_llm,
            patch(
                "app.application.extraction.structure_optimization_jobs."
                "start_document_structure_optimization"
            ) as start_structure,
        ):
            state = enrichment_workspace.get_requirement_enrichment_state(
                self.project["id"],
                self.document["id"],
                self.session.session_id,
                Response(),
            )

        self.assertEqual(before, counts())
        self.assertEqual(
            state["atomization_scope"]["classification_binding_state"],
            "NOT_STARTED",
        )
        self.assertEqual(
            state["atomization_scope"]["classification_snapshot_id"],
            self.classification.snapshot_id,
        )
        start_extraction.assert_not_called()
        start_llm.assert_not_called()
        start_structure.assert_not_called()

    def test_get_does_not_guess_classification_when_current_tree_is_unavailable(
        self,
    ) -> None:
        from fastapi import Response
        from app.application.extraction import atomization_scope
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        with (
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=(self.document, self.session),
            ),
            patch.object(
                enrichment_workspace,
                "_load_current_requirement_enrichment_coverage",
                return_value=self.coverage,
            ),
            patch.object(
                RUNTIME_STATE,
                "test_requirement_enrichment_repository_factory",
                lambda: self.repository,
            ),
            patch.object(
                atomization_scope,
                "_load_current_requirement_tree_for_state_read",
                return_value=None,
                create=True,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_refinement_provider_available",
                return_value=False,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_atomization_provider_available",
                return_value=False,
            ),
        ):
            state = enrichment_workspace.get_requirement_enrichment_state(
                self.project["id"],
                self.document["id"],
                self.session.session_id,
                Response(),
            )

        self.assertEqual(
            state["atomization_scope"]["classification_binding_state"],
            "CLASSIFICATION_UNAVAILABLE",
        )
        self.assertEqual(
            state["atomization_scope"]["classification_snapshot_id"],
            "",
        )

    def test_get_uses_one_scope_snapshot_for_progress_and_scope_projection(
        self,
    ) -> None:
        from fastapi import Response
        from app.application.extraction import atomization_scope
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        self._execute(
            self._payload(
                command_id="scope-get-snapshot-success",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        with self.assertRaises(RequirementAtomizationScopeExecutionError):
            self._execute(
                self._payload(
                    command_id="scope-get-snapshot-interrupted",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                _RecordingAtomizationProvider(
                    fail_review_node_id=self.direct_ids[1]
                ),
            )
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        with db.get_conn() as connection:
            attempt_rows = connection.execute(
                """
                SELECT payload_json FROM requirement_atomization_scope_attempts
                ORDER BY attempt_sequence
                """
            ).fetchall()
        succeeded = AtomizationScopeAttemptV1.model_validate(
            json.loads(str(attempt_rows[0]["payload_json"]))
        )
        interrupted = AtomizationScopeAttemptV1.model_validate(
            json.loads(str(attempt_rows[1]["payload_json"]))
        )
        receipt = self.repository.get_latest_atomization_scope_receipt(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            lineage_sha256=lineage.lineage_sha256,
        )

        class SnapshotSpy:
            def __init__(spy):
                spy.snapshot_calls = 0
                spy.legacy_attempt_calls = 0

            def get_atomization_scope_state_snapshot(spy, **_kwargs):
                spy.snapshot_calls += 1
                return SimpleNamespace(
                    receipt=receipt,
                    attempt=interrupted,
                    checkpoint_count=1,
                )

            def get_latest_atomization_scope_attempt(spy, **_kwargs):
                spy.legacy_attempt_calls += 1
                return (
                    succeeded
                    if spy.legacy_attempt_calls == 1
                    else interrupted
                )

            def __getattr__(spy, name):
                return getattr(self.repository, name)

        repository = SnapshotSpy()
        with (
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=(self.document, self.session),
            ),
            patch.object(
                enrichment_workspace,
                "_load_current_requirement_enrichment_coverage",
                return_value=self.coverage,
            ),
            patch.object(
                RUNTIME_STATE,
                "test_requirement_enrichment_repository_factory",
                lambda: repository,
            ),
            patch.object(
                atomization_scope,
                "_load_current_requirement_tree_for_state_read",
                return_value=self.working_tree,
                create=True,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_refinement_provider_available",
                return_value=False,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_atomization_provider_available",
                return_value=False,
            ),
        ):
            state = enrichment_workspace.get_requirement_enrichment_state(
                self.project["id"],
                self.document["id"],
                self.session.session_id,
                Response(),
            )

        self.assertEqual(repository.snapshot_calls, 1)
        self.assertEqual(repository.legacy_attempt_calls, 0)
        self.assertEqual(state["atomization_progress"]["state"], "PARTIAL")
        self.assertEqual(
            state["atomization_scope"]["last_attempt"]["status"],
            "INTERRUPTED",
        )

    def test_stale_running_attempt_is_projected_as_retryable_interruption(
        self,
    ) -> None:
        from app.application.extraction import enrichment_workspace
        from app.application.runtime import RUNTIME_STATE

        original_store = self.repository.create_or_get_atomization_checkpoint

        def store_then_process_exit(**kwargs):
            original_store(**kwargs)
            raise KeyboardInterrupt("simulated process exit")

        with (
            patch.object(
                self.repository,
                "create_or_get_atomization_checkpoint",
                side_effect=store_then_process_exit,
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-abandoned-running",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                _RecordingAtomizationProvider(),
            )
        self._age_scope_attempt(attempt_sequence=1)

        scope_state = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            repository=self.repository,
        )

        self.assertEqual(scope_state["last_attempt"]["status"], "INTERRUPTED")
        self.assertEqual(scope_state["last_attempt"]["persisted_status"], "RUNNING")
        self.assertTrue(scope_state["last_attempt"]["retryable"])
        self.assertTrue(scope_state["last_attempt"]["checkpoint_resumable"])
        with patch.object(
            RUNTIME_STATE,
            "test_requirement_enrichment_repository_factory",
            lambda: self.repository,
        ):
            legacy_state = enrichment_workspace._serialize_requirement_enrichment_state(
                self.session,
                coverage=self.coverage,
            )
        self.assertEqual(legacy_state["atomization_progress"]["state"], "PARTIAL")
        self.assertTrue(legacy_state["atomization_progress"]["resumable"])

    def test_stale_running_attempt_ignores_other_target_checkpoint(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-checkpoint-for-a",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        provider = _RecordingAtomizationProvider()
        with (
            patch.object(
                provider,
                "atomize",
                side_effect=KeyboardInterrupt("exit before B checkpoint"),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-abandoned-b-with-old-a",
                    expected_scope_revision=1,
                    targets=(self.direct_ids[1],),
                ),
                provider,
            )
        self._age_scope_attempt(attempt_sequence=2)

        scope_state = serialize_requirement_atomization_scope_state(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            repository=self.repository,
        )

        self.assertEqual(scope_state["last_attempt"]["status"], "INTERRUPTED")
        self.assertEqual(
            scope_state["last_attempt"]["executed_review_node_ids"],
            [self.direct_ids[1]],
        )
        self.assertFalse(scope_state["last_attempt"]["checkpoint_resumable"])

    def test_multi_atom_selected_direct_gets_source_bound_evidence_parent(self) -> None:
        direct = next(
            item
            for item in self.session.reviewed_result.nodes
            if item.review_node_id == self.direct_ids[0]
        )
        span = direct.source_spans[0]
        provider = _AtomizationProvider(
            split_at=(span.end_offset - span.start_offset) // 2
        )

        result = self._execute(
            self._payload(
                command_id="scope-split-parent",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )

        same_source = tuple(
            item
            for item in result.run.nodes
            if item.review_node_id == self.direct_ids[0]
        )
        parent = next(item for item in same_source if item.kind.value == "ROLLUP")
        children = tuple(
            item for item in same_source if item.kind.value == "ATOMIC_OBLIGATION"
        )
        self.assertEqual(len(children), 2)
        self.assertTrue(all(item.parent_node_id == parent.node_id for item in children))
        self.assertEqual(parent.source_spans, direct.source_spans)
        self.assertEqual(
            parent.source_text, "".join(item.source_text for item in children)
        )

    def test_rejects_unknown_target_bad_hash_and_stale_scope(self) -> None:
        provider = _RecordingAtomizationProvider()
        unknown = self._payload(
            command_id="scope-invalid-unknown",
            expected_scope_revision=0,
            targets=("not-in-current-classification",),
        )
        with self.assertRaisesRegex(ValueError, "unknown target"):
            self._execute(unknown, provider)

        bad_hash = self._payload(
            command_id="scope-invalid-hash",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
        ).model_copy(update={"target_scope_sha256": "f" * 64})
        with self.assertRaisesRegex(ValueError, "target scope hash"):
            self._execute(bad_hash, provider)

        self._execute(
            self._payload(
                command_id="scope-stale-base",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            provider,
        )
        with self.assertRaisesRegex(ValueError, "scope revision has changed"):
            self._execute(
                self._payload(
                    command_id="scope-stale-write",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[1],),
                ),
                provider,
            )

    def test_rejects_stale_working_tree_identity_before_provider_call(self) -> None:
        provider = _RecordingAtomizationProvider()
        payload = self._payload(
            command_id="scope-stale-working-tree",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
            expected_working_tree_sha256="f" * 64,
        )

        with self.assertRaisesRegex(ValueError, "working tree identity"):
            self._execute(payload, provider)

        self.assertEqual(provider.request_review_node_ids, [])

    def test_branch_rejects_noncanonical_descendant_range(self) -> None:
        anchor = "tree-structure-main"
        canonical_branch_ids = tuple(
            item.workflow_node_id
            for item in self.classification.entries
            if not item.tree_node_id.startswith("tree-extra-")
        )
        payload = self._payload(
            command_id="scope-branch-incomplete",
            expected_scope_revision=0,
            targets=canonical_branch_ids[:-1],
            scope="BRANCH",
            anchor_tree_node_id=anchor,
        )

        with self.assertRaisesRegex(ValueError, "BRANCH target range"):
            execute_requirement_atomization_scope(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session=self.session,
                coverage=self.coverage,
                classification_snapshot=self.classification,
                working_tree=self.working_tree,
                payload=payload,
                repository=self.repository,
                remote_provider=_RecordingAtomizationProvider(),
                max_concurrent_provider_calls=1,
            )

    def test_all_rejects_incomplete_or_reordered_canonical_range(self) -> None:
        canonical_ids = tuple(
            item.workflow_node_id for item in self.classification.entries
        )
        incomplete = self._payload(
            command_id="scope-all-incomplete",
            expected_scope_revision=0,
            targets=canonical_ids[:-1],
            scope="ALL",
        )
        reordered = self._payload(
            command_id="scope-all-reordered",
            expected_scope_revision=0,
            targets=tuple(reversed(canonical_ids)),
            scope="ALL",
        )

        for payload in (incomplete, reordered):
            with self.assertRaisesRegex(ValueError, "ALL target range"):
                execute_requirement_atomization_scope(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    session=self.session,
                    coverage=self.coverage,
                    classification_snapshot=self.classification,
                    working_tree=self.working_tree,
                    payload=payload,
                    repository=self.repository,
                    remote_provider=_RecordingAtomizationProvider(),
                    max_concurrent_provider_calls=1,
                )

    def test_selected_rejects_reordered_canonical_subset_before_provider(self) -> None:
        provider = _RecordingAtomizationProvider()
        payload = self._payload(
            command_id="scope-selected-reordered",
            expected_scope_revision=0,
            targets=tuple(reversed(self.direct_ids)),
            scope="SELECTED",
        )

        with self.assertRaisesRegex(ValueError, "SELECTED target range"):
            self._execute(payload, provider)

        self.assertEqual(provider.request_review_node_ids, [])

    def test_selected_requires_current_working_tree_before_provider(self) -> None:
        provider = _RecordingAtomizationProvider()
        payload = self._payload(
            command_id="scope-selected-without-tree",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
            scope="SELECTED",
        )

        with self.assertRaisesRegex(
            ValueError,
            "SELECTED scope requires the current working tree",
        ):
            execute_requirement_atomization_scope(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session=self.session,
                coverage=self.coverage,
                classification_snapshot=self.classification,
                working_tree=None,
                payload=payload,
                repository=self.repository,
                remote_provider=provider,
                max_concurrent_provider_calls=1,
            )

        self.assertEqual(provider.request_review_node_ids, [])

    def test_branch_and_all_accept_only_canonical_workflow_order(self) -> None:
        provider = _RecordingAtomizationProvider()
        anchor = "tree-structure-main"
        branch_ids = tuple(
            item.workflow_node_id
            for item in self.classification.entries
            if not item.tree_node_id.startswith("tree-extra-")
        )
        branch = execute_requirement_atomization_scope(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            working_tree=self.working_tree,
            payload=self._payload(
                command_id="scope-branch-canonical",
                expected_scope_revision=0,
                targets=branch_ids,
                scope="BRANCH",
                anchor_tree_node_id=anchor,
            ),
            repository=self.repository,
            remote_provider=provider,
            max_concurrent_provider_calls=1,
        )
        self.assertEqual(branch.receipt.cumulative_review_node_ids, self.direct_ids)
        self.assertEqual(tuple(provider.request_review_node_ids), self.direct_ids)

        all_ids = tuple(item.workflow_node_id for item in self.classification.entries)
        all_result = execute_requirement_atomization_scope(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            working_tree=self.working_tree,
            payload=self._payload(
                command_id="scope-all-canonical",
                expected_scope_revision=1,
                targets=all_ids,
                scope="ALL",
            ),
            repository=self.repository,
            remote_provider=provider,
            max_concurrent_provider_calls=1,
        )
        self.assertEqual(all_result.receipt.executed_review_node_ids, self.direct_ids)
        self.assertEqual(all_result.receipt.provider_call_count, 0)
        self.assertEqual(all_result.receipt.checkpoint_reuse_count, 2)

    def test_current_tree_anchor_recomputes_one_exact_business_target(self) -> None:
        payload = self._payload(
            command_id="scope-current-tree-anchor-mismatch",
            expected_scope_revision=0,
            targets=(self.direct_ids[0],),
            scope="CURRENT",
            anchor_tree_node_id=self._tree_node_id_for_workflow(self.direct_ids[1]),
        )

        with self.assertRaisesRegex(ValueError, "CURRENT target"):
            self._execute(payload, _RecordingAtomizationProvider())

    def test_post_write_identity_race_never_returns_success(self) -> None:
        from dataclasses import replace
        from app.application.extraction.atomization_scope import (
            build_atomization_scope_execution_identity,
        )

        current_identity = build_atomization_scope_execution_identity(
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            working_tree=self.working_tree,
        )
        changed_identity = replace(
            current_identity,
            coverage_sha256="f" * 64,
        )
        reads = 0

        def load_identity():
            nonlocal reads
            reads += 1
            return current_identity if reads == 1 else changed_identity

        with self.assertRaisesRegex(ValueError, "execution identity has changed"):
            execute_requirement_atomization_scope(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session=self.session,
                coverage=self.coverage,
                classification_snapshot=self.classification,
                working_tree=self.working_tree,
                payload=self._payload(
                    command_id="scope-post-write-race",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                repository=self.repository,
                remote_provider=_RecordingAtomizationProvider(),
                max_concurrent_provider_calls=1,
                current_execution_identity_loader=load_identity,
            )

        self.assertEqual(reads, 2)
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        self.assertIsNotNone(
            self.repository.get_latest_atomization_scope_receipt(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                lineage_sha256=lineage.lineage_sha256,
            )
        )

    def test_repository_cas_rejects_superseded_classification_head(self) -> None:
        superseding = revise_entry_classification_snapshot(
            self.classification,
            overrides={self.direct_ids[0]: EntryClassificationType.NON_TECHNICAL},
            snapshot_id="working-scope-race-2",
            created_at="2026-08-07T08:30:00+00:00",
        )
        append_entry_classification_snapshot(
            base_snapshot=self.classification,
            next_snapshot=superseding,
            command_id="scope-test-classification-race",
            command_sha256="8" * 64,
            expected_revision=self.classification.revision,
            expected_snapshot_sha256=self.classification.payload_sha256,
        )
        provider = _RecordingAtomizationProvider()

        with self.assertRaisesRegex(
            RequirementEnrichmentConflictError,
            "classification head has changed",
        ):
            self._execute(
                self._payload(
                    command_id="scope-classification-cas",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                provider,
            )

        self.assertEqual(provider.request_review_node_ids, [self.direct_ids[0]])
        lineage = build_requirement_enrichment_lineage(
            self.session,
            coverage=self.coverage,
        )
        self.assertIsNone(
            self.repository.get_latest_atomization_scope_receipt(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session.session_id,
                lineage_sha256=lineage.lineage_sha256,
            )
        )

    def test_cutover_rejects_atomization_run_revision_column_tamper(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-run-tamper",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_atomization_runs SET atomization_revision = 7"
            )

        with self.assertRaises(CutoverRejected):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_accepts_untampered_scope_artifacts(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-valid",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()

        plan = build_cutover_plan(Path(db.DB_PATH))

        self.assertEqual(plan.table_counts["requirement_atomization_runs"], 1)
        self.assertEqual(plan.table_counts["requirement_atomization_scope_receipts"], 1)
        self.assertEqual(plan.table_counts["requirement_atomization_scope_attempts"], 1)

    def test_cutover_accepts_multiple_succeeded_attempts_for_one_receipt(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-concurrent-replay",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM requirement_atomization_scope_attempts
                WHERE attempt_sequence = 1
                """
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["attempt_sequence"] = 2
            payload["attempt_id"] = stable_atomization_scope_attempt_id(
                lineage_sha256=str(payload["lineage_sha256"]),
                attempt_sequence=2,
                command_sha256=str(payload["command_sha256"]),
            )
            internal_payload = dict(payload)
            internal_payload.pop("payload_sha256")
            payload["payload_sha256"] = hashlib.sha256(
                json.dumps(
                    internal_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            replay_attempt = AtomizationScopeAttemptV1.model_validate(payload)
            replay_payload_json = json.dumps(
                replay_attempt.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO requirement_atomization_scope_attempts (
                    attempt_id, schema_version, project_id, document_id,
                    session_id, review_revision, lineage_sha256,
                    attempt_sequence, command_id, command_sha256,
                    expected_scope_revision, status, payload_sha256,
                    payload_json, started_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    replay_attempt.attempt_id,
                    replay_attempt.schema_version,
                    replay_attempt.project_id,
                    replay_attempt.document_id,
                    replay_attempt.session_id,
                    replay_attempt.review_revision,
                    replay_attempt.lineage_sha256,
                    replay_attempt.attempt_sequence,
                    replay_attempt.command_id,
                    replay_attempt.command_sha256,
                    replay_attempt.expected_scope_revision,
                    replay_attempt.status.value,
                    hashlib.sha256(replay_payload_json.encode("utf-8")).hexdigest(),
                    replay_payload_json,
                    replay_attempt.started_at_utc.isoformat(),
                    replay_attempt.updated_at_utc.isoformat(),
                ),
            )

        plan = build_cutover_plan(Path(db.DB_PATH))

        self.assertEqual(plan.table_counts["requirement_atomization_scope_receipts"], 1)
        self.assertEqual(plan.table_counts["requirement_atomization_scope_attempts"], 2)

    def test_cutover_rejects_duplicate_running_attempts_for_one_scope_revision(
        self,
    ) -> None:
        provider = _RecordingAtomizationProvider()
        with (
            patch.object(
                provider,
                "atomize",
                side_effect=KeyboardInterrupt("simulated process exit"),
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self._execute(
                self._payload(
                    command_id="scope-cutover-running-duplicate",
                    expected_scope_revision=0,
                    targets=(self.direct_ids[0],),
                ),
                provider,
            )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT payload_json FROM requirement_atomization_scope_attempts
                WHERE attempt_sequence = 1
                """
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["attempt_sequence"] = 2
            payload["attempt_id"] = stable_atomization_scope_attempt_id(
                lineage_sha256=str(payload["lineage_sha256"]),
                attempt_sequence=2,
                command_sha256=str(payload["command_sha256"]),
            )
            internal_payload = dict(payload)
            internal_payload.pop("payload_sha256")
            payload["payload_sha256"] = hashlib.sha256(
                json.dumps(
                    internal_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            duplicate = AtomizationScopeAttemptV1.model_validate(payload)
            duplicate_json = json.dumps(
                duplicate.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                INSERT INTO requirement_atomization_scope_attempts (
                    attempt_id, schema_version, project_id, document_id,
                    session_id, review_revision, lineage_sha256,
                    attempt_sequence, command_id, command_sha256,
                    expected_scope_revision, status, payload_sha256,
                    payload_json, started_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    duplicate.attempt_id,
                    duplicate.schema_version,
                    duplicate.project_id,
                    duplicate.document_id,
                    duplicate.session_id,
                    duplicate.review_revision,
                    duplicate.lineage_sha256,
                    duplicate.attempt_sequence,
                    duplicate.command_id,
                    duplicate.command_sha256,
                    duplicate.expected_scope_revision,
                    duplicate.status.value,
                    hashlib.sha256(duplicate_json.encode("utf-8")).hexdigest(),
                    duplicate_json,
                    duplicate.started_at_utc.isoformat(),
                    duplicate.updated_at_utc.isoformat(),
                ),
            )

        with self.assertRaisesRegex(CutoverRejected, "multiple RUNNING"):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_rejects_receipt_without_any_succeeded_attempt(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-orphan-receipt",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            connection.execute("DELETE FROM requirement_atomization_scope_attempts")

        with self.assertRaisesRegex(CutoverRejected, "at least one SUCCEEDED"):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_rejects_scope_receipt_internal_hash_tamper(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-receipt-tamper",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            row = connection.execute(
                "SELECT receipt_id, payload_json FROM requirement_atomization_scope_receipts"
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["provider_call_count"] = int(payload["provider_call_count"]) + 1
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE requirement_atomization_scope_receipts
                SET payload_json = ?, payload_sha256 = ?
                WHERE receipt_id = ?
                """,
                (
                    payload_json,
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    str(row["receipt_id"]),
                ),
            )

        with self.assertRaises(CutoverRejected):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_rejects_scope_attempt_column_binding_tamper(self) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-attempt-tamper",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_atomization_scope_attempts
                SET status = 'INTERRUPTED'
                """
            )

        with self.assertRaises(CutoverRejected):
            build_cutover_plan(Path(db.DB_PATH))

    def test_cutover_rejects_resigned_attempt_pointing_to_other_scope_revision(
        self,
    ) -> None:
        self._execute(
            self._payload(
                command_id="scope-cutover-cross-revision-1",
                expected_scope_revision=0,
                targets=(self.direct_ids[0],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._execute(
            self._payload(
                command_id="scope-cutover-cross-revision-2",
                expected_scope_revision=1,
                targets=(self.direct_ids[1],),
            ),
            _RecordingAtomizationProvider(),
        )
        self._make_cutover_source_identity_valid()
        with db.get_conn() as connection:
            other_receipt = connection.execute(
                """
                SELECT receipt_id FROM requirement_atomization_scope_receipts
                WHERE scope_revision = 2
                """
            ).fetchone()
            first_attempt = connection.execute(
                """
                SELECT attempt_id, payload_json
                FROM requirement_atomization_scope_attempts
                WHERE attempt_sequence = 1
                """
            ).fetchone()
            payload = json.loads(str(first_attempt["payload_json"]))
            payload["receipt_id"] = str(other_receipt["receipt_id"])
            internal_payload = dict(payload)
            internal_payload.pop("payload_sha256")
            payload["payload_sha256"] = hashlib.sha256(
                json.dumps(
                    internal_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE requirement_atomization_scope_attempts
                SET payload_json = ?, payload_sha256 = ?
                WHERE attempt_id = ?
                """,
                (
                    payload_json,
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    str(first_attempt["attempt_id"]),
                ),
            )

        with self.assertRaises(CutoverRejected):
            build_cutover_plan(Path(db.DB_PATH))

    def _make_cutover_source_identity_valid(self) -> None:
        source_path = Path(str(self.document["file_path"]))
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE sor_documents SET source_sha256 = ? WHERE id = ?",
                (source_sha256, self.document["id"]),
            )

    def _execute(self, payload, provider):
        return execute_requirement_atomization_scope(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session=self.session,
            coverage=self.coverage,
            classification_snapshot=self.classification,
            working_tree=self.working_tree,
            payload=payload,
            repository=self.repository,
            remote_provider=provider,
            max_concurrent_provider_calls=1,
        )

    def _interrupt_running_attempt(
        self,
        running: AtomizationScopeAttemptV1,
        *,
        error_code: str,
    ) -> None:
        fields = running.model_dump(mode="python")
        fields.update(
            {
                "payload_sha256": "0" * 64,
                "status": AtomizationScopeAttemptStatus.INTERRUPTED,
                "retryable": True,
                "error_code": error_code,
                "updated_at_utc": datetime.now(timezone.utc),
            }
        )
        draft = AtomizationScopeAttemptV1.model_construct(**fields)
        fields["payload_sha256"] = canonical_atomization_scope_attempt_sha256(
            draft
        )
        interrupted = AtomizationScopeAttemptV1.model_validate(fields)
        self.repository.update_atomization_scope_attempt(
            attempt=interrupted,
            expected_status=AtomizationScopeAttemptStatus.RUNNING,
        )

    def _age_scope_attempt(self, *, attempt_sequence: int) -> None:
        stale_at = datetime.now(timezone.utc) - timedelta(hours=2)
        stale_payload_time = stale_at.isoformat().replace("+00:00", "Z")
        with db.get_conn() as connection:
            row = connection.execute(
                """
                SELECT attempt_id, payload_json
                FROM requirement_atomization_scope_attempts
                WHERE attempt_sequence = ?
                """,
                (attempt_sequence,),
            ).fetchone()
            payload = json.loads(str(row["payload_json"]))
            payload["started_at_utc"] = stale_payload_time
            payload["updated_at_utc"] = stale_payload_time
            internal_payload = dict(payload)
            internal_payload.pop("payload_sha256")
            payload["payload_sha256"] = hashlib.sha256(
                json.dumps(
                    internal_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE requirement_atomization_scope_attempts
                SET payload_json = ?, payload_sha256 = ?,
                    started_at_utc = ?, updated_at_utc = ?
                WHERE attempt_id = ?
                """,
                (
                    payload_json,
                    hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
                    stale_at.isoformat(),
                    stale_at.isoformat(),
                    str(row["attempt_id"]),
                ),
            )

    def _tree_node_id_for_workflow(self, workflow_node_id: str) -> str:
        return next(
            item.tree_node_id
            for item in self.classification.entries
            if item.workflow_node_id == workflow_node_id
        )

    def _payload(
        self,
        *,
        command_id: str,
        expected_scope_revision: int,
        targets: tuple[str, ...],
        operation: str = "APPLY",
        scope: str = "SELECTED",
        expected_working_tree_id: str | None = None,
        expected_working_tree_sha256: str | None = None,
        expected_effective_structure_id: str | None = None,
        anchor_tree_node_id: str | None = None,
    ) -> RequirementAtomizationScopeRunRequest:
        values = {
            "schema_version": "requirement-atomization-run-request/v2",
            "command_id": command_id,
            "operation": operation,
            "scope": scope,
            "expected_review_revision": self.session.reviewed_result.revision,
            "expected_scope_revision": expected_scope_revision,
            "expected_entry_classification_snapshot_id": self.classification.snapshot_id,
            "expected_entry_classification_revision": self.classification.revision,
            "expected_entry_classification_sha256": self.classification.payload_sha256,
            "expected_working_tree_id": (
                expected_working_tree_id or self.classification.working_tree_id
            ),
            "expected_working_tree_sha256": (
                expected_working_tree_sha256 or self.classification.working_tree_sha256
            ),
            "expected_effective_structure_id": (
                expected_effective_structure_id
                or self.classification.effective_structure_id
            ),
            "anchor_tree_node_id": (
                anchor_tree_node_id
                if anchor_tree_node_id is not None
                else self._tree_node_id_for_workflow(targets[0])
                if operation == "APPLY" and scope == "CURRENT"
                else None
            ),
            "target_review_node_ids": targets,
            "target_scope_sha256": "0" * 64,
            "unselected_policy": "SOURCE_BOUND_PASSTHROUGH",
        }
        values["target_scope_sha256"] = stable_atomization_target_scope_sha256(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=self.session.session_id,
            command_id=command_id,
            operation=operation,
            scope=scope,
            expected_review_revision=values["expected_review_revision"],
            expected_scope_revision=expected_scope_revision,
            expected_entry_classification_snapshot_id=self.classification.snapshot_id,
            expected_entry_classification_revision=self.classification.revision,
            expected_entry_classification_sha256=self.classification.payload_sha256,
            expected_working_tree_id=values["expected_working_tree_id"],
            expected_working_tree_sha256=values["expected_working_tree_sha256"],
            expected_effective_structure_id=values["expected_effective_structure_id"],
            anchor_tree_node_id=values["anchor_tree_node_id"],
            target_review_node_ids=targets,
            unselected_policy="SOURCE_BOUND_PASSTHROUGH",
        )
        return RequirementAtomizationScopeRunRequest.model_validate(values)


def _request(review_node_id: str) -> RequirementAtomizationProviderRequest:
    source_text = "The component shall keep this source-bound obligation."
    return RequirementAtomizationProviderRequest(
        lineage_sha256="b" * 64,
        review_node_id=review_node_id,
        requirement_subtype="FUNCTIONAL_REQUIREMENT",
        source_segments=(
            RequirementAtomizationSourceSegment(
                segment_index=0,
                semantic_unit_id="semantic-unit-" + "c" * 32,
                owner_structure_node_id="structure-node-" + "d" * 32,
                source_start_offset=0,
                source_end_offset=len(source_text),
                source_text=source_text,
            ),
        ),
    )


def _classification(
    session: LlmRequirementReviewSession,
    *,
    extra_entries: tuple[tuple[str, EntryClassificationType], ...] = (),
    working_tree: RequirementTreeSnapshotV1,
) -> EntryClassificationSnapshotV1:
    initial = build_initial_entry_classification_snapshot(working_tree)
    overrides = {
        item.review_node_id: EntryClassificationType.TECHNICAL
        for item in session.reviewed_result.nodes
    }
    overrides.update(dict(extra_entries))
    return revise_entry_classification_snapshot(
        initial,
        overrides=overrides,
        snapshot_id="working-scope-1",
        created_at="2026-08-07T08:00:00+00:00",
    )


def _working_tree_for_classification(
    session: LlmRequirementReviewSession,
    *,
    extra_entries: tuple[tuple[str, EntryClassificationType], ...] = (),
) -> RequirementTreeSnapshotV1:
    reviewed = session.reviewed_result.nodes
    root_id = "tree-0"
    structure_root_id = "tree-structure-main"
    structure_root = RequirementTreeNodeV1(
        id=structure_root_id,
        kind="SECTION",
        depth=0,
        source_order=0,
        label="Main requirements",
        child_ids=(root_id,),
        projection_role="STRUCTURE",
    )
    reviewed_nodes = tuple(
        RequirementTreeNodeV1(
            id=f"tree-{index}",
            parent_id=structure_root_id if index == 0 else root_id,
            kind="CLAUSE",
            depth=1 if index == 0 else 2,
            source_order=index + 1,
            label=f"Entry {index}",
            child_ids=(
                tuple(f"tree-{child}" for child in range(1, len(reviewed)))
                if index == 0
                else ()
            ),
            workflow_node_id=item.review_node_id,
            projection_role="REQUIREMENT",
        )
        for index, item in enumerate(reviewed)
    )
    extra_nodes = tuple(
        RequirementTreeNodeV1(
            id=f"tree-extra-{index}",
            kind="CLAUSE",
            depth=0,
            source_order=len(reviewed) + index + 1,
            label=f"Extra {index}",
            workflow_node_id=workflow_node_id,
            projection_role="REQUIREMENT",
        )
        for index, (workflow_node_id, _entry_type) in enumerate(extra_entries)
    )
    return RequirementTreeSnapshotV1(
        source_identity=RequirementTreeSourceIdentity(
            project_id=session.project_id,
            document_id=session.document_id,
            source_ir_id="source-ir-scope-1",
            source_sha256=session.prediction.source_sha256,
        ),
        structure_identity=RequirementTreeStructureIdentity(
            base_snapshot_id=session.prediction.structure_snapshot_id,
            effective_snapshot_id=session.prediction.structure_snapshot_id,
            effective_tree_sha256="d" * 64,
            revision=1,
        ),
        mode=RequirementTreeMode.WORKING,
        state=RequirementTreeState.READY,
        root_node_ids=(structure_root_id, *(item.id for item in extra_nodes)),
        nodes=(structure_root, *reviewed_nodes, *extra_nodes),
    )


if __name__ == "__main__":
    unittest.main()
