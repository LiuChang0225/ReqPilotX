from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app.adapters.persistence.requirement_matrix_states import (
    list_requirement_matrix_node_states,
    set_requirement_matrix_node_disposition,
)
from app.adapters.persistence.sqlite_requirement_enrichment import (
    SQLiteRequirementEnrichmentRepository,
)
from app.api.errors import (
    application_error_handler,
    requirement_set_mutation_blocked_handler,
)
from app.api.routers.review import build as build_review_router
from app.application.errors import ApplicationError
from app.application.requirements import (
    activate_document_requirement_set,
)
from app.application.requirements_contracts import (
    ActivateRequirementSetRequest,
)
from app.domain.extraction.requirement_tree import RequirementTreeState


class RequirementSetActivationPublicationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary_directory.name)
        self.database_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)
        db.init_db()
        self.enrichment_repository = SQLiteRequirementEnrichmentRepository(
            db.DB_PATH
        )
        self.project = db.create_project("Activation gate", "Customer")
        source_path = self.root / "source.pdf"
        source_path.write_bytes(b"source")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="source.pdf",
            file_type="pdf",
            file_path=str(source_path),
            extracted_text="",
            source_sha256="a" * 64,
        )
        self.baseline = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        db.activate_requirement_set(
            self.project["id"], self.document["id"], self.baseline["id"]
        )

    def _publication_target(self, *, state: str) -> dict:
        target = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                SET publication_key = ?, publication_state = ?
                WHERE id = ?
                """,
                ("b" * 64, state, target["id"]),
            )
        return db.get_requirement_set(target["id"]) or {}

    def _assert_baseline_remains_active(self) -> None:
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            self.baseline["id"],
        )

    def _attach_fake_tree_identity(self, requirement_set_id: str) -> None:
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                SET requirement_tree_object_key = ?,
                    requirement_tree_sha256 = ?,
                    requirement_tree_schema_version = ?,
                    structure_snapshot_id = ?,
                    structure_tree_sha256 = ?
                WHERE id = ?
                """,
                (
                    "requirement-trees/fake.json",
                    "c" * 64,
                    "requirement-tree/v1",
                    "effective-1",
                    "d" * 64,
                    requirement_set_id,
                ),
            )

    def _ready_enrichment_target(self) -> tuple[dict, str, str]:
        target = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        self._attach_fake_tree_identity(target["id"])
        binding_preview_id = f"binding-preview-{target['id']}"
        binding_id = f"binding-{target['id']}"
        binding_sha256 = "e" * 64
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO requirement_enrichment_publication_binding_previews (
                    binding_preview_id, schema_version, project_id, document_id,
                    session_id, review_revision,
                    base_active_requirement_set_id,
                    accepted_classification_snapshot_id,
                    accepted_classification_sha256, atomization_state,
                    projection_sha256, payload_sha256, payload_json,
                    created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_preview_id,
                    "requirement-enrichment-publication-binding-preview/v2",
                    self.project["id"],
                    self.document["id"],
                    "session-1",
                    1,
                    self.baseline["id"],
                    "classification-1",
                    "a" * 64,
                    "NOT_RUN",
                    "b" * 64,
                    "c" * 64,
                    "{}",
                    "2026-08-07T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                INSERT INTO requirement_enrichment_publication_bindings (
                    binding_id, binding_preview_id, project_id, document_id,
                    requirement_set_id, projection_sha256, publication_state,
                    payload_sha256, payload_json, created_at_utc, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?, ?)
                """,
                (
                    binding_id,
                    binding_preview_id,
                    self.project["id"],
                    self.document["id"],
                    target["id"],
                    "b" * 64,
                    binding_sha256,
                    "{}",
                    "2026-08-07T00:00:00+00:00",
                    "2026-08-07T00:00:00+00:00",
                ),
            )
            connection.execute(
                """
                UPDATE requirement_sets
                SET publication_state = 'READY',
                    enrichment_publication_binding_id = ?,
                    enrichment_publication_binding_sha256 = ?
                WHERE id = ?
                """,
                (binding_id, binding_sha256, target["id"]),
            )
        return db.get_requirement_set(target["id"]) or {}, binding_id, binding_sha256

    def _semantic_activation_request(
        self,
        *,
        binding_id: str,
        binding_sha256: str,
        active_requirement_set_id: str | None = None,
    ) -> ActivateRequirementSetRequest:
        return ActivateRequirementSetRequest(
            schema_version="requirement-set-semantic-activation-request/v1",
            expected_active_requirement_set_id=(
                self.baseline["id"]
                if active_requirement_set_id is None
                else active_requirement_set_id
            ),
            expected_enrichment_publication_binding_id=binding_id,
            expected_enrichment_publication_binding_sha256=binding_sha256,
        )

    @staticmethod
    def _route_app() -> FastAPI:
        app = FastAPI()
        app.add_exception_handler(ApplicationError, application_error_handler)
        app.add_exception_handler(
            db.RequirementSetMutationBlocked,
            requirement_set_mutation_blocked_handler,
        )
        app.include_router(build_review_router())
        return app

    def _activation_url(self, requirement_set_id: str, *, action: str = "activate") -> str:
        return (
            f"/api/projects/{self.project['id']}"
            f"/sor-documents/{self.document['id']}"
            f"/requirement-sets/{requirement_set_id}/{action}"
        )

    def test_database_rejects_publishing_requirement_set(self) -> None:
        target = self._publication_target(state="PUBLISHING")

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.activate_requirement_set(
                self.project["id"], self.document["id"], target["id"]
            )

        self._assert_baseline_remains_active()

    def test_database_rejects_ready_publication_without_tree_metadata(
        self,
    ) -> None:
        target = self._publication_target(state="READY")

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.activate_requirement_set(
                self.project["id"], self.document["id"], target["id"]
            )

        self._assert_baseline_remains_active()

    def test_database_requires_ready_state_even_with_complete_tree_identity(
        self,
    ) -> None:
        target = self._publication_target(state="")
        self._attach_fake_tree_identity(target["id"])

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.activate_requirement_set(
                self.project["id"], self.document["id"], target["id"]
            )

        self._assert_baseline_remains_active()

    def test_application_reports_publication_still_in_progress(self) -> None:
        target = self._publication_target(state="PUBLISHING")

        with self.assertRaises(ApplicationError) as blocked:
            activate_document_requirement_set(
                self.project["id"], self.document["id"], target["id"]
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "requirement_set_publication_in_progress",
        )
        self._assert_baseline_remains_active()

    def test_application_reports_ready_publication_missing_tree(self) -> None:
        target = self._publication_target(state="READY")

        with self.assertRaises(ApplicationError) as blocked:
            activate_document_requirement_set(
                self.project["id"], self.document["id"], target["id"]
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "requirement_set_tree_not_persisted",
        )
        self._assert_baseline_remains_active()

    def test_exact_working_tree_publication_skips_derivation_matrix_clone(
        self,
    ) -> None:
        target = self._publication_target(state="READY")
        self._attach_fake_tree_identity(target["id"])
        self._attach_fake_tree_identity(self.baseline["id"])
        target = db.get_requirement_set(target["id"]) or {}
        set_requirement_matrix_node_disposition(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.baseline["id"],
            node_id="source-only-node",
            disposition="ANALYZE",
            reason="人工选择旧版本条目。",
            expected_revision=0,
        )
        source_states = list_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=self.baseline["id"],
        )
        self.assertEqual(
            list_requirement_matrix_node_states(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=target["id"],
            ),
            [],
        )
        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "_working_tree_publication_key",
                return_value=target["publication_key"],
            ),
            patch(
                "app.application.requirement_set_impact_service."
                "reconcile_requirement_set_impact_for_draft",
                return_value={"receipt_id": "impact-receipt"},
            ) as reconcile,
            patch(
                "app.adapters.persistence.requirement_set_impacts."
                "finalize_staged_requirement_set_activation",
            ) as finalize,
            patch.object(
                db,
                "activate_requirement_set",
                wraps=db.activate_requirement_set,
            ) as activate,
        ):
            response = activate_document_requirement_set(
                self.project["id"],
                self.document["id"],
                target["id"],
            )

        self.assertEqual(response["active_requirement_set"]["id"], target["id"])
        reconcile.assert_not_called()
        finalize.assert_not_called()
        self.assertEqual(
            activate.call_args.kwargs["expected_active_requirement_set_id"],
            self.baseline["id"],
        )
        self.assertEqual(
            list_requirement_matrix_node_states(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=self.baseline["id"],
            ),
            source_states,
        )
        self.assertEqual(
            list_requirement_matrix_node_states(
                project_id=self.project["id"],
                document_id=self.document["id"],
                requirement_set_id=target["id"],
            ),
            [],
        )

    def test_exact_working_tree_publication_rejects_stale_source_lineage(
        self,
    ) -> None:
        target = self._publication_target(state="READY")
        self._attach_fake_tree_identity(target["id"])
        target = db.get_requirement_set(target["id"]) or {}
        replacement = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        db.activate_requirement_set(
            self.project["id"], self.document["id"], replacement["id"]
        )

        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            patch(
                "app.application.extraction.working_tree_publication."
                "_working_tree_publication_key",
                return_value=target["publication_key"],
            ),
            self.assertRaises(ApplicationError) as blocked,
        ):
            activate_document_requirement_set(
                self.project["id"],
                self.document["id"],
                target["id"],
            )

        self.assertEqual(blocked.exception.status_code, 409)
        self.assertEqual(
            blocked.exception.detail["code"],
            "requirement_set_activation_source_changed",
        )
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            replacement["id"],
        )

    def test_database_strict_activation_compares_active_set_and_ready_binding(
        self,
    ) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()

        activated = db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            target["id"],
            expected_active_requirement_set_id=self.baseline["id"],
            expected_enrichment_publication_binding_id=binding_id,
            expected_enrichment_publication_binding_sha256=binding_sha256,
        )

        self.assertEqual(activated["id"], target["id"])
        self.assertEqual(
            activated["enrichment_publication_binding_id"], binding_id
        )
        self.assertEqual(
            activated["enrichment_publication_binding_sha256"], binding_sha256
        )

    def test_database_strict_activation_rejects_active_set_drift_before_switch(
        self,
    ) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.activate_requirement_set(
                self.project["id"],
                self.document["id"],
                target["id"],
                expected_active_requirement_set_id="stale-active-set",
                expected_enrichment_publication_binding_id=binding_id,
                expected_enrichment_publication_binding_sha256=binding_sha256,
                table_assets=[{"table_id": "must-not-be-written"}],
            )

        self._assert_baseline_remains_active()
        self.assertFalse((db.get_requirement_set(target["id"]) or {})["is_active"])
        self.assertEqual(
            db.list_document_table_assets(
                self.project["id"],
                self.document["id"],
                requirement_set_id=target["id"],
            ),
            [],
        )

    def test_database_strict_activation_rejects_binding_identity_drift(
        self,
    ) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()

        for expected_binding_id, expected_binding_sha256 in (
            ("stale-binding", binding_sha256),
            (binding_id, "f" * 64),
        ):
            with self.subTest(
                binding_id=expected_binding_id,
                binding_sha256=expected_binding_sha256,
            ):
                with self.assertRaises(db.RequirementSetMutationBlocked):
                    db.activate_requirement_set(
                        self.project["id"],
                        self.document["id"],
                        target["id"],
                        expected_active_requirement_set_id=self.baseline["id"],
                        expected_enrichment_publication_binding_id=(
                            expected_binding_id
                        ),
                        expected_enrichment_publication_binding_sha256=(
                            expected_binding_sha256
                        ),
                    )
                self._assert_baseline_remains_active()

    def test_application_forwards_semantic_activation_preconditions(
        self,
    ) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()
        request = self._semantic_activation_request(
            binding_id=binding_id,
            binding_sha256=binding_sha256,
        )

        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            patch.object(
                db,
                "activate_requirement_set",
                wraps=db.activate_requirement_set,
            ) as activate,
        ):
            response = activate_document_requirement_set(
                self.project["id"],
                self.document["id"],
                target["id"],
                request,
            )

        self.assertEqual(response["project_id"], self.project["id"])
        self.assertEqual(response["document_id"], self.document["id"])
        self.assertEqual(
            response["active_requirement_set"][
                "enrichment_publication_binding_id"
            ],
            binding_id,
        )
        self.assertEqual(
            activate.call_args.kwargs["expected_active_requirement_set_id"],
            self.baseline["id"],
        )
        self.assertEqual(
            activate.call_args.kwargs[
                "expected_enrichment_publication_binding_id"
            ],
            binding_id,
        )
        self.assertEqual(
            activate.call_args.kwargs[
                "expected_enrichment_publication_binding_sha256"
            ],
            binding_sha256,
        )

    def test_route_returns_409_without_switching_when_semantic_cas_is_stale(
        self,
    ) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()
        payload = self._semantic_activation_request(
            binding_id=binding_id,
            binding_sha256=binding_sha256,
            active_requirement_set_id="stale-active-set",
        ).model_dump(mode="json")

        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            TestClient(self._route_app()) as client,
        ):
            response = client.post(
                self._activation_url(target["id"]),
                json=payload,
            )

        self.assertEqual(response.status_code, 409)
        self._assert_baseline_remains_active()

    def test_route_rejects_partial_semantic_preconditions(self) -> None:
        target = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )

        with TestClient(self._route_app()) as client:
            response = client.post(
                self._activation_url(target["id"]),
                json={
                    "schema_version": (
                        "requirement-set-semantic-activation-request/v1"
                    ),
                    "expected_enrichment_publication_binding_id": "binding-1",
                    "expected_enrichment_publication_binding_sha256": "e" * 64,
                },
            )

        self.assertEqual(response.status_code, 422)
        self._assert_baseline_remains_active()

    def test_route_semantic_activation_returns_verifiable_binding(self) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()
        payload = self._semantic_activation_request(
            binding_id=binding_id,
            binding_sha256=binding_sha256,
        ).model_dump(mode="json")

        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            TestClient(self._route_app()) as client,
        ):
            response = client.post(
                self._activation_url(target["id"]),
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["project_id"], self.project["id"])
        self.assertEqual(body["document_id"], self.document["id"])
        self.assertEqual(body["active_requirement_set"]["id"], target["id"])
        self.assertEqual(body["document"]["id"], self.document["id"])
        self.assertEqual(body["document"]["project_id"], self.project["id"])
        self.assertEqual(
            body["document"]["active_requirement_set_id"], target["id"]
        )
        self.assertEqual(
            body["active_requirement_set"][
                "enrichment_publication_binding_id"
            ],
            binding_id,
        )
        self.assertEqual(
            body["active_requirement_set"][
                "enrichment_publication_binding_sha256"
            ],
            binding_sha256,
        )

    def test_route_semantic_activation_accepts_exact_no_active_state(self) -> None:
        target, binding_id, binding_sha256 = self._ready_enrichment_target()
        with db.get_conn() as connection:
            connection.execute(
                "UPDATE requirement_sets SET is_active = 0 "
                "WHERE sor_document_id = ?",
                (self.document["id"],),
            )
            connection.execute(
                "UPDATE sor_documents SET active_requirement_set_id = '' "
                "WHERE id = ?",
                (self.document["id"],),
            )
        payload = self._semantic_activation_request(
            binding_id=binding_id,
            binding_sha256=binding_sha256,
            active_requirement_set_id="",
        ).model_dump(mode="json")

        with (
            patch(
                "app.application.extraction.requirement_tree_snapshots."
                "load_requirement_set_tree_snapshot",
                return_value=SimpleNamespace(state=RequirementTreeState.READY),
            ),
            TestClient(self._route_app()) as client,
        ):
            response = client.post(
                self._activation_url(target["id"]),
                json=payload,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["active_requirement_set"]["id"], target["id"]
        )

    def test_activate_and_restore_routes_keep_empty_body_compatible(self) -> None:
        for action, request_kwargs in (
            ("activate", {"json": {}}),
            ("restore", {}),
        ):
            with self.subTest(action=action):
                target = db.create_requirement_set(
                    self.project["id"], self.document["id"]
                )
                with (
                    patch(
                        "app.application.extraction.requirement_tree_snapshots."
                        "load_requirement_set_tree_snapshot",
                        return_value=SimpleNamespace(
                            state=RequirementTreeState.READY
                        ),
                    ),
                    TestClient(self._route_app()) as client,
                ):
                    response = client.post(
                        self._activation_url(target["id"], action=action),
                        **request_kwargs,
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.json()["active_requirement_set"]["id"],
                    target["id"],
                )

    def test_discard_never_removes_an_active_set_or_its_requirements(
        self,
    ) -> None:
        target = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        requirement = db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=target["id"],
            req_code="REQ-ACTIVE",
            source_page=1,
            source_anchor="source-active",
            requirement_text="系统应保留已激活版本。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        activated = db.activate_requirement_set(
            self.project["id"], self.document["id"], target["id"]
        )
        self.assertEqual(activated["id"], target["id"])

        self.assertFalse(
            db.discard_inactive_requirement_set(
                self.document["id"],
                target["id"],
                project_id=self.project["id"],
            )
        )
        retained = db.get_requirement_set(target["id"]) or {}
        self.assertTrue(retained.get("is_active"))
        self.assertEqual(
            [
                row["id"]
                for row in db.list_requirements(
                    self.project["id"],
                    requirement_set_id=target["id"],
                )
            ],
            [requirement["id"]],
        )

    def test_activation_and_discard_serialize_on_the_document_row(self) -> None:
        for attempt in range(8):
            with self.subTest(attempt=attempt):
                target = db.create_requirement_set(
                    self.project["id"], self.document["id"]
                )
                barrier = Barrier(2)

                def activate() -> dict:
                    barrier.wait()
                    return db.activate_requirement_set(
                        self.project["id"],
                        self.document["id"],
                        target["id"],
                    )

                def discard() -> bool:
                    barrier.wait()
                    return db.discard_inactive_requirement_set(
                        self.document["id"],
                        target["id"],
                        project_id=self.project["id"],
                    )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    activated_future = executor.submit(activate)
                    discarded_future = executor.submit(discard)
                    activated = activated_future.result()
                    discarded = discarded_future.result()

                retained = db.get_requirement_set(target["id"])
                if discarded:
                    self.assertEqual(activated, {})
                    self.assertIsNone(retained)
                else:
                    self.assertEqual(activated["id"], target["id"])
                    self.assertIsNotNone(retained)
                    self.assertTrue(retained["is_active"])

    def test_discard_preserves_a_previously_activated_historical_set(
        self,
    ) -> None:
        target = db.create_requirement_set(
            self.project["id"], self.document["id"]
        )
        requirement = db.insert_requirement(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=target["id"],
            req_code="REQ-HISTORY",
            source_page=1,
            source_anchor="source-history",
            requirement_text="系统应保留曾经激活的历史版本。",
            requirement_type="FUNCTIONAL",
            priority="MEDIUM",
        )
        activated = db.activate_requirement_set(
            self.project["id"], self.document["id"], target["id"]
        )
        self.assertEqual(activated["id"], target["id"])
        restored = db.activate_requirement_set(
            self.project["id"], self.document["id"], self.baseline["id"]
        )
        self.assertEqual(restored["id"], self.baseline["id"])
        historical = db.get_requirement_set(target["id"]) or {}
        self.assertFalse(historical.get("is_active"))
        self.assertTrue(str(historical.get("activated_at") or ""))

        self.assertFalse(
            db.discard_inactive_requirement_set(
                self.document["id"],
                target["id"],
                project_id=self.project["id"],
            )
        )
        retained = db.get_requirement_set(target["id"]) or {}
        self.assertEqual(retained["id"], target["id"])
        self.assertEqual(
            [
                row["id"]
                for row in db.list_requirements(
                    self.project["id"],
                    requirement_set_id=target["id"],
                )
            ],
            [requirement["id"]],
        )


if __name__ == "__main__":
    unittest.main()
