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
import threading
import unittest
from io import BytesIO
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import db
from app.application.errors import ApplicationError
from app.adapters.persistence.sqlite_requirement_enrichment import (
    SQLiteRequirementEnrichmentRepository,
)
from app.application.extraction.requirement_enrichment_publication_v2 import (
    build_requirement_enrichment_publication_builder_entries_v2,
    build_requirement_enrichment_publication_projection_v2,
    overlay_requirement_enrichment_publication_tree_v2,
)
from app.application.extraction.entry_classification_repository import (
    append_entry_classification_snapshot,
)
from app.application.extraction.contracts import (
    RequirementPublicationPreviewRequestV2,
    RequirementPublishDraftRequestV2,
)
from app.application.extraction.requirement_enrichment_publication_transport_v2 import (
    _recompute_current_projection_v2,
    publish_requirement_enrichment_draft_v2,
)
from app.application.extraction.atomization_scope import (
    _build_scope_attempt,
    _copy_scope_run_revision,
)
from app.application.extraction import enrichment_workspace
from app.application.cutover.sqlite_to_postgres import (
    CutoverRejected,
    _validate_requirement_enrichment_publication_relationships,
)
from app.domain.entry_classification import (
    EntryClassificationSnapshotV1,
    EntryClassificationType,
    accept_entry_classification_snapshot,
    build_initial_entry_classification_snapshot,
    canonical_entry_classification_payload_sha256,
    revise_entry_classification_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    RequirementTreeSnapshotError,
    _tree_object_key,
    clear_verified_requirement_tree_snapshot_cache,
    load_requirement_set_tree_snapshot,
    persist_requirement_tree_snapshot,
)
from app.application.runtime import RUNTIME_STATE
from app.application.requirements import derive_document_requirement_set
from app.adapters.storage.local_object_store import LocalObjectStore
from app.domain.requirement_enrichment_publication_v2 import (
    RequirementEnrichmentAtomizationStateV2,
    RequirementEnrichmentPublicationBindingV2,
    build_requirement_enrichment_publication_binding_preview_v2,
    build_requirement_enrichment_publication_binding_v2,
)
from app.domain.atomization_scope import (
    AtomizationScopeAttemptStatus,
    AtomizationScopeOperation,
    AtomizationTargetScope,
    stable_effective_entry_classification_sha256,
)
from app.domain.extraction.requirement_tree import (
    canonical_requirement_tree_bytes,
    canonical_requirement_tree_sha256,
)
from tests.test_requirement_enrichment_publication_v2 import (
    _SECOND_TECHNICAL_REVIEW_ID,
    _TECHNICAL_REVIEW_ID,
    _accepted_classification,
    _accepted_working_tree,
    _accepted_working_tree_with_second_technical,
    _applied_publication_inputs,
    _builder_like_tree,
    _receipt_with_updates,
)


class RequirementEnrichmentPublicationTransportV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"source")
        self.source_sha256 = "a" * 64
        self.project = db.create_project(name="W4", customer_name="Customer")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=source.name,
            file_type="pdf",
            file_path=str(source),
            extracted_text="",
            source_sha256=self.source_sha256,
            source_size=source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )
        active = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=self.source_sha256,
        )
        self.active = db.activate_requirement_set(
            self.project["id"], self.document["id"], active["id"]
        )
        self.repository = SQLiteRequirementEnrichmentRepository(db.DB_PATH)

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def _preview(self):
        working_tree, initial_classification, working_classification, accepted_classification = (
            self._working_tree_and_classification()
        )
        append_entry_classification_snapshot(
            base_snapshot=initial_classification,
            next_snapshot=working_classification,
            command_id="publication-v2-classify",
            command_sha256="d" * 64,
            expected_revision=initial_classification.revision,
            expected_snapshot_sha256=initial_classification.payload_sha256,
        )
        append_entry_classification_snapshot(
            base_snapshot=working_classification,
            next_snapshot=accepted_classification,
            command_id="publication-v2-accept",
            command_sha256="e" * 64,
            expected_revision=working_classification.revision,
            expected_snapshot_sha256=working_classification.payload_sha256,
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=accepted_classification,
            atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
        )
        return build_requirement_enrichment_publication_binding_preview_v2(
            session_id="llm-review-" + "8" * 32,
            review_revision=3,
            base_active_requirement_set_id=self.active["id"],
            projection=projection,
        )

    def _working_tree_and_classification(self, fixture_tree=None):
        fixture_tree = fixture_tree or _accepted_working_tree()
        working_tree = fixture_tree.model_copy(
            update={
                "source_identity": fixture_tree.source_identity.model_copy(
                    update={
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                    }
                )
            }
        )
        initial_classification = build_initial_entry_classification_snapshot(
            working_tree
        )
        distinct_working_tree_id = "accepted-working-tree-artifact-v2"
        initial_draft = initial_classification.model_copy(
            update={
                "working_tree_id": distinct_working_tree_id,
                "payload_sha256": "0" * 64,
            }
        )
        initial_classification = EntryClassificationSnapshotV1.model_validate(
            initial_draft.model_copy(
                update={
                    "payload_sha256": (
                        canonical_entry_classification_payload_sha256(
                            initial_draft
                        )
                    )
                }
            ).model_dump(mode="json")
        )
        default_accepted = _accepted_classification(working_tree)
        working_classification = revise_entry_classification_snapshot(
            initial_classification,
            overrides={
                entry.workflow_node_id: entry.effective_type
                for entry in default_accepted.entries
                if entry.effective_type is not None
            },
            snapshot_id="classification-working-publication-v2",
            created_at="2026-08-07T08:00:00+00:00",
        )
        accepted_classification = accept_entry_classification_snapshot(
            working_classification,
            snapshot_id="classification-accepted-publication-v2",
            accepted_at="2026-08-07T08:01:00+00:00",
            accepted_by="publication-v2-test",
        )
        return (
            working_tree,
            initial_classification,
            working_classification,
            accepted_classification,
        )

    def test_not_run_binding_preview_is_hashed_and_persisted_without_sidecars(self) -> None:
        preview = self._preview()

        stored = self.repository.create_or_get_publication_binding_preview_v2(preview)
        loaded = self.repository.get_publication_binding_preview_v2(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id=preview.session_id,
            binding_preview_id=preview.binding_preview_id,
        )

        self.assertEqual(stored, preview)
        self.assertEqual(loaded, preview)
        self.assertEqual(preview.atomization_state.value, "NOT_RUN")
        self.assertEqual(preview.projection.atomization_scope_receipt_id, "")
        self.assertEqual(preview.projection.atomization_run_id, "")
        self.assertEqual(preview.projection.publication_preview_id, "")

    def test_same_projection_can_have_distinct_preview_contexts(self) -> None:
        first = self._preview()
        second = build_requirement_enrichment_publication_binding_preview_v2(
            session_id="llm-review-" + "9" * 32,
            review_revision=first.review_revision + 1,
            base_active_requirement_set_id=first.base_active_requirement_set_id,
            projection=first.projection,
        )

        self.repository.create_or_get_publication_binding_preview_v2(first)
        self.repository.create_or_get_publication_binding_preview_v2(second)

        self.assertNotEqual(first.binding_preview_id, second.binding_preview_id)
        with sqlite3.connect(db.DB_PATH) as connection:
            count = connection.execute(
                """
                SELECT COUNT(*)
                FROM requirement_enrichment_publication_binding_previews
                WHERE projection_sha256 = ?
                """,
                (first.projection.projection_sha256,),
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_v2_requests_are_explicit_and_not_run_rejects_scope_fields(self) -> None:
        preview = self._preview()
        parsed = RequirementPublicationPreviewRequestV2.model_validate(
            {
                "schema_version": (
                    "requirement-enrichment-publication-preview-request/v2"
                ),
                "expected_review_revision": preview.review_revision,
                "accepted_entry_classification_snapshot_id": (
                    preview.projection.entry_classification_snapshot_id
                ),
                "accepted_entry_classification_revision": (
                    preview.projection.entry_classification_revision
                ),
                "accepted_entry_classification_sha256": (
                    preview.projection.entry_classification_sha256
                ),
                "atomization_state": "NOT_RUN",
            }
        )
        publish = RequirementPublishDraftRequestV2.model_validate(
            {
                "schema_version": "requirement-enrichment-publish-draft-request/v2",
                "expected_review_revision": preview.review_revision,
                "binding_preview_id": preview.binding_preview_id,
                "binding_preview_sha256": preview.payload_sha256,
            }
        )

        self.assertEqual(parsed.atomization_state, "NOT_RUN")
        self.assertEqual(publish.binding_preview_id, preview.binding_preview_id)
        with self.assertRaisesRegex(ValueError, "cannot bind atomization scope"):
            RequirementPublicationPreviewRequestV2.model_validate(
                {
                    **parsed.model_dump(mode="json"),
                    "atomization_scope_revision": 1,
                }
            )

    def test_not_run_projection_does_not_touch_scope_run_preview_or_provider(self) -> None:
        fixture_tree = _accepted_working_tree()
        working_tree = fixture_tree.model_copy(
            update={
                "source_identity": fixture_tree.source_identity.model_copy(
                    update={
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                    }
                )
            }
        )
        classification = _accepted_classification(working_tree)

        with patch(
            "app.application.extraction.atomization_scope."
            "load_current_entry_classification_context",
            return_value=(working_tree, classification),
        ):
            projection, returned_tree = _recompute_current_projection_v2(
                project_id=self.project["id"],
                document_id=self.document["id"],
                document={"active_requirement_set_id": self.active["id"]},
                session=SimpleNamespace(),
                repository=object(),
                accepted_snapshot_id=classification.snapshot_id,
                accepted_revision=classification.revision,
                accepted_sha256=classification.payload_sha256,
                atomization_state=RequirementEnrichmentAtomizationStateV2.NOT_RUN,
                atomization_scope_receipt_id="",
                atomization_scope_receipt_sha256="",
                atomization_scope_revision=0,
                base_active_requirement_set_id=self.active["id"],
            )

        self.assertEqual(returned_tree, working_tree)
        self.assertEqual(projection.atomization_state.value, "NOT_RUN")
        self.assertEqual(projection.atomization_scope_receipt_id, "")
        self.assertEqual(projection.atomization_run_id, "")
        self.assertEqual(projection.publication_preview_id, "")

    def test_applied_api_uses_current_receipt_and_content_equivalent_acceptance_without_provider_calls(
        self,
    ) -> None:
        (
            working_tree,
            accepted_classification,
            receipt,
            run,
            core_preview,
        ) = self._prepare_applied_artifacts()
        current_document = db.get_sor_document(self.document["id"])
        assert current_document is not None
        session = SimpleNamespace(
            session_id=run.lineage.session_id,
            reviewed_result=SimpleNamespace(revision=run.lineage.review_revision),
            updated_at_utc=run.created_at_utc,
        )
        coverage = SimpleNamespace()
        repository_spy = Mock(wraps=self.repository)
        response = SimpleNamespace(headers={})
        object_store = LocalObjectStore(self.root / "applied-objects")
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )

        def persist_applied_tree(**kwargs):
            builder_tree = _builder_like_tree(
                working_tree=kwargs["accepted_working_tree"],
                projection=kwargs["projection"],
                requirement_entry_ids=kwargs["requirement_entry_ids"],
            )
            builder_tree = builder_tree.model_copy(
                update={
                    "source_identity": builder_tree.source_identity.model_copy(
                        update={
                            "requirement_set_id": kwargs["requirement_set_id"]
                        }
                    )
                }
            )
            tree = overlay_requirement_enrichment_publication_tree_v2(
                accepted_working_tree=kwargs["accepted_working_tree"],
                builder_tree=builder_tree,
                projection=kwargs["projection"],
                materialized_entries=kwargs["materialized_entries"],
            )
            persist_requirement_tree_snapshot(
                requirement_set_id=kwargs["requirement_set_id"],
                snapshot=tree,
            )
            return tree

        with (
            patch(
                "app.application.extraction.atomization_scope."
                "load_current_entry_classification_context",
                return_value=(working_tree, accepted_classification),
            ),
            patch.object(
                enrichment_workspace,
                "_reject_requirement_enrichment_during_structure_optimization",
            ),
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=(current_document, session),
            ),
            patch.object(
                enrichment_workspace,
                "_require_current_requirement_enrichment_coverage",
                return_value=coverage,
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_enrichment_repository",
                return_value=repository_spy,
            ),
            patch.object(
                enrichment_workspace,
                "_build_requirement_atomization_provider",
            ) as provider_factory,
            patch(
                "app.application.extraction."
                "requirement_enrichment_publication_transport_v2."
                "build_requirement_enrichment_lineage",
                return_value=run.lineage,
            ),
            patch(
                "app.application.extraction."
                "requirement_enrichment_publication_transport_v2."
                "build_requirement_publication_preview",
                return_value=core_preview,
            ),
            patch(
                "app.application.extraction."
                "requirement_enrichment_publication_transport_v2."
                "_build_and_persist_publication_tree_v2",
                side_effect=persist_applied_tree,
            ),
            patch.object(RUNTIME_STATE, "container", container),
        ):
            preview_response = (
                enrichment_workspace.create_requirement_publication_preview(
                    self.project["id"],
                    self.document["id"],
                    session.session_id,
                    RequirementPublicationPreviewRequestV2(
                        expected_review_revision=run.lineage.review_revision,
                        accepted_entry_classification_snapshot_id=(
                            accepted_classification.snapshot_id
                        ),
                        accepted_entry_classification_revision=(
                            accepted_classification.revision
                        ),
                        accepted_entry_classification_sha256=(
                            accepted_classification.payload_sha256
                        ),
                        atomization_state="APPLIED",
                        atomization_scope_receipt_id=receipt.receipt_id,
                        atomization_scope_receipt_sha256=receipt.payload_sha256,
                        atomization_scope_revision=receipt.scope_revision,
                    ),
                    response,
                )
            )
            binding_preview = (
                self.repository.get_publication_binding_preview_v2(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    session_id=session.session_id,
                    binding_preview_id=preview_response["binding_preview"][
                        "binding_preview_id"
                    ],
                )
            )
            assert binding_preview is not None
            publish_response = enrichment_workspace.publish_requirement_enrichment_draft(
                self.project["id"],
                self.document["id"],
                session.session_id,
                RequirementPublishDraftRequestV2(
                    expected_review_revision=run.lineage.review_revision,
                    binding_preview_id=binding_preview.binding_preview_id,
                    binding_preview_sha256=binding_preview.payload_sha256,
                ),
                response,
            )
            loaded = load_requirement_set_tree_snapshot(
                db.get_requirement_set(
                    publish_response["publication"]["requirement_set_id"]
                )
            )

        projection = binding_preview.projection
        expected_counts = (
            enrichment_workspace._requirement_publication_projection_v2_counts(
                projection
            )
        )
        selected_rows = tuple(
            item
            for item in projection.rows
            if item.workflow_node_id == _TECHNICAL_REVIEW_ID
        )
        pass_through_rows = tuple(
            item
            for item in projection.rows
            if item.workflow_node_id == _SECOND_TECHNICAL_REVIEW_ID
        )

        self.assertEqual(projection.atomization_state.value, "APPLIED")
        self.assertEqual(projection.applied_review_node_ids, (_TECHNICAL_REVIEW_ID,))
        self.assertEqual(len(selected_rows), 4)
        self.assertTrue(all(item.atomization_node_id for item in selected_rows))
        self.assertEqual(len(pass_through_rows), 1)
        self.assertIsNone(pass_through_rows[0].atomization_node_id)
        self.assertEqual(
            receipt.entry_classification_content_sha256,
            projection.entry_classification_content_sha256,
        )
        self.assertNotEqual(
            receipt.entry_classification_snapshot_id,
            projection.entry_classification_snapshot_id,
        )
        self.assertEqual(preview_response["counts"], expected_counts)
        self.assertEqual(publish_response["counts"], expected_counts)
        self.assertFalse(preview_response["analysis_started"])
        self.assertFalse(preview_response["atomization_invoked"])
        self.assertFalse(publish_response["analysis_started"])
        self.assertTrue(publish_response["requirement_set_created"])
        self.assertFalse(publish_response["requirement_set_activated"])
        self.assertEqual(
            publish_response["classification"]["snapshot_id"],
            accepted_classification.snapshot_id,
        )
        self.assertEqual(
            loaded.model_dump(mode="json"),
            publish_response["requirement_tree"],
        )
        provider_factory.assert_not_called()
        self.assertGreaterEqual(
            repository_spy.get_latest_atomization_scope_receipt.call_count,
            3,
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            persisted_receipt_envelope_sha = connection.execute(
                """
                SELECT payload_sha256
                FROM requirement_atomization_scope_receipts
                WHERE receipt_id = ?
                """,
                (receipt.receipt_id,),
            ).fetchone()[0]
        self.assertNotEqual(persisted_receipt_envelope_sha, receipt.payload_sha256)

    def test_enrichment_state_declares_semantic_publication_v2_capability(self) -> None:
        session = SimpleNamespace(
            project_id=self.project["id"],
            document_id=self.document["id"],
            session_id="llm-review-" + "1" * 32,
        )
        response = SimpleNamespace(headers={})
        with (
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=(db.get_sor_document(self.document["id"]), session),
            ),
            patch.object(
                enrichment_workspace,
                "_load_current_requirement_enrichment_coverage",
                return_value=None,
            ),
            patch.object(
                enrichment_workspace,
                "_serialize_requirement_enrichment_state",
                return_value={"schema_version": "requirement-enrichment-state/v1"},
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_enrichment_repository",
                return_value=object(),
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
                "app.application.extraction.atomization_scope."
                "_load_current_requirement_tree_for_state_read",
                return_value=None,
            ),
            patch(
                "app.application.extraction.atomization_scope."
                "serialize_requirement_atomization_scope_state",
                return_value={},
            ),
        ):
            state = enrichment_workspace.get_requirement_enrichment_state(
                self.project["id"],
                self.document["id"],
                session.session_id,
                response,
            )

        self.assertEqual(
            state["semantic_publication_capability"],
            {
                "schema_version": (
                    "requirement-enrichment-semantic-publication-capability/v2"
                ),
                "preview_request_schema_version": (
                    "requirement-enrichment-publication-preview-request/v2"
                ),
                "preview_response_schema_version": (
                    "requirement-enrichment-publication-preview-response/v2"
                ),
                "publish_request_schema_version": (
                    "requirement-enrichment-publish-draft-request/v2"
                ),
                "publish_response_schema_version": (
                    "requirement-enrichment-publish-draft-response/v2"
                ),
                "atomization_states": ["NOT_RUN", "APPLIED"],
                "not_run_preview_invokes_atomization": False,
                "publish_starts_analysis": False,
                "default_dispositions": {
                    "TECHNICAL": "ANALYZE",
                    "NON_TECHNICAL": "RETAIN",
                    "INFORMATION": "REFERENCE",
                },
            },
        )

    def test_v2_publish_tree_failure_has_stable_error_and_removes_partial_draft_and_object(
        self,
    ) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        working_tree, *_working, accepted_classification = (
            self._working_tree_and_classification()
        )
        current_document = db.get_sor_document(self.document["id"])
        assert current_document is not None
        session = SimpleNamespace(
            session_id=preview.session_id,
            reviewed_result=SimpleNamespace(revision=preview.review_revision),
        )
        response = SimpleNamespace(headers={})
        object_store = LocalObjectStore(self.root / "failure-objects")
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )
        partial: dict[str, str] = {}

        def persist_then_fail(**kwargs):
            partial["requirement_set_id"] = kwargs["requirement_set_id"]
            builder_tree = _builder_like_tree(
                working_tree=kwargs["accepted_working_tree"],
                projection=kwargs["projection"],
                requirement_entry_ids=kwargs["requirement_entry_ids"],
            )
            builder_tree = builder_tree.model_copy(
                update={
                    "source_identity": builder_tree.source_identity.model_copy(
                        update={
                            "requirement_set_id": kwargs["requirement_set_id"]
                        }
                    )
                }
            )
            tree = overlay_requirement_enrichment_publication_tree_v2(
                accepted_working_tree=kwargs["accepted_working_tree"],
                builder_tree=builder_tree,
                projection=kwargs["projection"],
                materialized_entries=kwargs["materialized_entries"],
            )
            persisted = persist_requirement_tree_snapshot(
                requirement_set_id=kwargs["requirement_set_id"],
                snapshot=tree,
            )
            partial["object_key"] = persisted["requirement_tree_object_key"]
            raise RequirementTreeSnapshotError("synthetic tree verification failure")

        with (
            patch(
                "app.application.extraction.atomization_scope."
                "load_current_entry_classification_context",
                return_value=(working_tree, accepted_classification),
            ),
            patch.object(
                enrichment_workspace,
                "_reject_requirement_enrichment_during_structure_optimization",
            ),
            patch.object(
                enrichment_workspace,
                "_require_requirement_enrichment_session",
                return_value=(current_document, session),
            ),
            patch.object(
                enrichment_workspace,
                "_requirement_enrichment_repository",
                return_value=self.repository,
            ),
            patch(
                "app.application.extraction."
                "requirement_enrichment_publication_transport_v2."
                "_build_and_persist_publication_tree_v2",
                side_effect=persist_then_fail,
            ),
            patch.object(RUNTIME_STATE, "container", container),
            self.assertRaises(ApplicationError) as raised,
        ):
            enrichment_workspace.publish_requirement_enrichment_draft(
                self.project["id"],
                self.document["id"],
                session.session_id,
                RequirementPublishDraftRequestV2(
                    expected_review_revision=preview.review_revision,
                    binding_preview_id=preview.binding_preview_id,
                    binding_preview_sha256=preview.payload_sha256,
                ),
                response,
            )

        self.assertEqual(raised.exception.status_code, 500)
        self.assertEqual(
            raised.exception.detail["code"],
            "requirement_publication_binding_failed",
        )
        requirement_set_id = partial["requirement_set_id"]
        self.assertIsNone(db.get_requirement_set(requirement_set_id))
        self.assertIsNone(object_store.stat(partial["object_key"]))
        with sqlite3.connect(db.DB_PATH) as connection:
            requirement_count = connection.execute(
                "SELECT COUNT(*) FROM requirements WHERE requirement_set_id = ?",
                (requirement_set_id,),
            ).fetchone()[0]
            binding_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM requirement_enrichment_publication_bindings
                WHERE requirement_set_id = ?
                """,
                (requirement_set_id,),
            ).fetchone()[0]
        self.assertEqual(requirement_count, 0)
        self.assertEqual(binding_count, 0)

    def test_two_phase_publish_keeps_w3_key_empty_and_finalizes_exact_binding(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )

        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        requirement_set = db.get_requirement_set(started.requirement_set_id)
        self.assertEqual(requirement_set["publication_key"], "")
        self.assertEqual(requirement_set["publication_state"], "PUBLISHING")
        self.assertEqual(requirement_set["enrichment_publication_binding_id"], "")
        self.assertEqual(
            requirement_set["enrichment_publication_binding_sha256"], ""
        )
        self.assertEqual(len(started.row_bindings), len(preview.projection.rows))

        tree_sha256 = "b" * 64
        tree_object_key = "requirement-trees/w4.json"
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                   SET requirement_tree_object_key = ?,
                       requirement_tree_sha256 = ?,
                       requirement_tree_schema_version = ?,
                       structure_snapshot_id = ?,
                       structure_tree_sha256 = ?, structure_revision = 1
                 WHERE id = ?
                """,
                (
                    tree_object_key,
                    tree_sha256,
                    "requirement-tree/v1",
                    preview.projection.effective_structure_id,
                    "c" * 64,
                    started.requirement_set_id,
                ),
            )
        binding = build_requirement_enrichment_publication_binding_v2(
            started=started,
            requirement_tree_object_key=tree_object_key,
            requirement_tree_sha256=tree_sha256,
            requirement_tree_schema_version="requirement-tree/v1",
        )
        ready = self.repository.finalize_publication_binding_v2(binding)

        self.assertIsInstance(ready, RequirementEnrichmentPublicationBindingV2)
        requirement_set = db.get_requirement_set(started.requirement_set_id)
        self.assertEqual(requirement_set["publication_state"], "READY")
        self.assertEqual(
            requirement_set["enrichment_publication_binding_id"], binding.binding_id
        )
        self.assertEqual(
            requirement_set["enrichment_publication_binding_sha256"],
            binding.payload_sha256,
        )
        self.assertEqual(requirement_set["publication_key"], "")
        self.assertEqual(
            self.repository.get_publication_binding_v2(
                requirement_set_id=started.requirement_set_id,
                binding_id=binding.binding_id,
            ),
            binding,
        )

    def test_finalize_revalidates_active_set_inside_its_transaction(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        binding = self._binding_after_synthetic_tree(preview, started)
        replacement = db.create_requirement_set(
            self.project["id"],
            self.document["id"],
            source_hash=self.source_sha256,
        )
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            replacement["id"],
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "active Requirement Set changed",
        ):
            self.repository.finalize_publication_binding_v2(binding)

        requirement_set = db.get_requirement_set(started.requirement_set_id)
        self.assertEqual(requirement_set["publication_state"], "PUBLISHING")
        self.assertEqual(requirement_set["enrichment_publication_binding_id"], "")

    def test_begin_rejects_not_run_preview_after_accepted_classification_head_changes(
        self,
    ) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        _tree, _initial, _working, accepted = (
            self._working_tree_and_classification()
        )
        self._append_changed_accepted_classification(
            accepted,
            suffix="before-begin",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "current accepted classification head",
        ):
            self.repository.begin_publication_binding_v2(
                preview=preview,
                current_projection=preview.projection,
            )

    def test_finalize_rejects_not_run_projection_after_classification_toctou(
        self,
    ) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        binding = self._binding_after_synthetic_tree(preview, started)
        _tree, _initial, _working, accepted = (
            self._working_tree_and_classification()
        )
        self._append_changed_accepted_classification(
            accepted,
            suffix="before-finalize",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "current accepted classification head",
        ):
            self.repository.finalize_publication_binding_v2(binding)

        requirement_set = db.get_requirement_set(started.requirement_set_id)
        self.assertEqual(requirement_set["publication_state"], "PUBLISHING")
        with self.assertRaisesRegex(PermissionError, "not READY"):
            db.activate_requirement_set(
                self.project["id"],
                self.document["id"],
                started.requirement_set_id,
            )

    def test_begin_rejects_applied_preview_after_scope_receipt_head_changes(
        self,
    ) -> None:
        preview, _tree, accepted, receipt, run = self._applied_binding_preview()
        self._append_undo_scope_revision(
            receipt=receipt,
            run=run,
            accepted_classification=accepted,
            command_suffix="before-begin",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "current atomization scope receipt head",
        ):
            self.repository.begin_publication_binding_v2(
                preview=preview,
                current_projection=preview.projection,
            )

    def test_finalize_rejects_applied_projection_after_scope_receipt_toctou(
        self,
    ) -> None:
        preview, _tree, accepted, receipt, run = self._applied_binding_preview()
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        binding = self._binding_after_synthetic_tree(preview, started)
        self._append_undo_scope_revision(
            receipt=receipt,
            run=run,
            accepted_classification=accepted,
            command_suffix="before-finalize",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "current atomization scope receipt head",
        ):
            self.repository.finalize_publication_binding_v2(binding)

        requirement_set = db.get_requirement_set(started.requirement_set_id)
        self.assertEqual(requirement_set["publication_state"], "PUBLISHING")
        with self.assertRaisesRegex(PermissionError, "not READY"):
            db.activate_requirement_set(
                self.project["id"],
                self.document["id"],
                started.requirement_set_id,
            )

    def test_finalize_rejects_tampered_materialized_decision_trace(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        binding = self._binding_after_synthetic_tree(preview, started)
        with sqlite3.connect(db.DB_PATH) as connection:
            requirement = connection.execute(
                """
                SELECT requirement_entry_id
                FROM requirement_enrichment_publication_row_bindings
                WHERE binding_id = ? ORDER BY publication_order LIMIT 1
                """,
                (started.binding_id,),
            ).fetchone()
            trace_json = connection.execute(
                "SELECT decision_reference_json FROM requirements WHERE id = ?",
                (requirement[0],),
            ).fetchone()[0]
            trace = json.loads(trace_json)
            connection.execute(
                "UPDATE requirements SET decision_reference_json = ? WHERE id = ?",
                (
                    json.dumps(
                        {**trace, "workflow_node_id": "review-tampered"},
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    requirement[0],
                ),
            )

        with self.assertRaisesRegex(
            RuntimeError,
            "materialized requirement rows",
        ):
            self.repository.finalize_publication_binding_v2(binding)

        self.assertEqual(
            db.get_requirement_set(started.requirement_set_id)["publication_state"],
            "PUBLISHING",
        )

    def test_only_creator_can_abort_exact_publishing_draft(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        creator = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        retry = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )

        self.assertTrue(creator.created)
        self.assertFalse(retry.created)
        self.assertFalse(self.repository.abort_publication_binding_v2(retry))
        self.assertIsNotNone(db.get_requirement_set(creator.requirement_set_id))
        self.assertTrue(self.repository.abort_publication_binding_v2(creator))
        self.assertIsNone(db.get_requirement_set(creator.requirement_set_id))

    def test_retry_persist_after_creator_abort_leaves_no_owned_orphan_object(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        creator = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        retry = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        self.assertTrue(creator.created)
        self.assertFalse(retry.created)
        working_tree, *_classification = self._working_tree_and_classification()
        requirement_entry_ids = {
            item.requirement_tree_node_id: item.requirement_entry_id
            for item in retry.row_bindings
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=preview.projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        builder_tree = builder_tree.model_copy(
            update={
                "source_identity": builder_tree.source_identity.model_copy(
                    update={"requirement_set_id": retry.requirement_set_id}
                )
            }
        )
        tree = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=preview.projection,
            materialized_entries=(
                build_requirement_enrichment_publication_builder_entries_v2(
                    preview.projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
            ),
        )
        object_store = LocalObjectStore(self.root / "retry-race-objects")
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )
        history_key = "requirement-trees/history/ready-tree.json"
        history_payload = b"historical-ready-tree"
        history_sha = hashlib.sha256(history_payload).hexdigest()
        object_store.put(
            key=history_key,
            source=BytesIO(history_payload),
            expected_sha256=history_sha,
            media_type="application/json",
        )

        self.assertTrue(self.repository.abort_publication_binding_v2(creator))
        target_key = _tree_object_key(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=retry.requirement_set_id,
        )
        with (
            patch.object(RUNTIME_STATE, "container", container),
            self.assertRaisesRegex(
                RequirementTreeSnapshotError,
                "disappeared|no longer exists",
            ),
        ):
            persist_requirement_tree_snapshot(
                requirement_set_id=retry.requirement_set_id,
                snapshot=tree,
            )

        self.assertIsNone(db.get_requirement_set(retry.requirement_set_id))
        self.assertIsNone(object_store.stat(target_key))
        self.assertEqual(object_store.stat(history_key).sha256, history_sha)

    def test_stale_retry_cleanup_does_not_delete_recreated_ready_tree(self) -> None:
        (
            preview,
            _creator,
            retry,
            tree,
            object_store,
            container,
            target_key,
            tree_sha,
        ) = self._aborted_retry_tree(object_root="ready-reclaim-objects")
        original_attach = db.attach_requirement_tree_snapshot

        def attach_after_recreate(requirement_set_id: str, **candidate):
            self.assertEqual(
                original_attach(requirement_set_id, **candidate),
                {},
            )
            rebuilt = self.repository.begin_publication_binding_v2(
                preview=preview,
                current_projection=preview.projection,
            )
            self.assertTrue(rebuilt.created)
            attached = original_attach(requirement_set_id, **candidate)
            binding = build_requirement_enrichment_publication_binding_v2(
                started=rebuilt,
                requirement_tree_object_key=attached[
                    "requirement_tree_object_key"
                ],
                requirement_tree_sha256=tree_sha,
                requirement_tree_schema_version=tree.schema_version,
            )
            self.repository.finalize_publication_binding_v2(binding)
            return {}

        with (
            patch.object(RUNTIME_STATE, "container", container),
            patch.object(
                db,
                "attach_requirement_tree_snapshot",
                side_effect=attach_after_recreate,
            ),
            self.assertRaises(RequirementTreeSnapshotError),
        ):
            persist_requirement_tree_snapshot(
                requirement_set_id=retry.requirement_set_id,
                snapshot=tree,
            )

        ready = db.get_requirement_set(retry.requirement_set_id)
        self.assertEqual(ready["publication_state"], "READY")
        self.assertEqual(ready["requirement_tree_object_key"], target_key)
        self.assertEqual(ready["requirement_tree_sha256"], tree_sha)
        self.assertEqual(object_store.stat(target_key).sha256, tree_sha)

    def test_stale_retry_cleanup_does_not_delete_recreated_tree_claim(self) -> None:
        (
            preview,
            _creator,
            retry,
            tree,
            object_store,
            container,
            target_key,
            tree_sha,
        ) = self._aborted_retry_tree(object_root="publishing-reclaim-objects")
        original_attach = db.attach_requirement_tree_snapshot
        rebuilt_starts = []

        def attach_after_recreate(requirement_set_id: str, **candidate):
            self.assertEqual(
                original_attach(requirement_set_id, **candidate),
                {},
            )
            rebuilt = self.repository.begin_publication_binding_v2(
                preview=preview,
                current_projection=preview.projection,
            )
            self.assertTrue(rebuilt.created)
            rebuilt_starts.append(rebuilt)
            return {}

        with (
            patch.object(RUNTIME_STATE, "container", container),
            patch.object(
                db,
                "attach_requirement_tree_snapshot",
                side_effect=attach_after_recreate,
            ),
            self.assertRaises(RequirementTreeSnapshotError),
        ):
            persist_requirement_tree_snapshot(
                requirement_set_id=retry.requirement_set_id,
                snapshot=tree,
            )

        publishing = db.get_requirement_set(retry.requirement_set_id)
        self.assertEqual(publishing["publication_state"], "PUBLISHING")
        self.assertEqual(object_store.stat(target_key).sha256, tree_sha)

        attached = original_attach(
            retry.requirement_set_id,
            object_key=target_key,
            tree_sha256=tree_sha,
            schema_version=tree.schema_version,
            structure_snapshot_id=tree.structure_identity.effective_snapshot_id,
            structure_tree_sha256=(
                tree.structure_identity.effective_tree_sha256
            ),
            structure_revision=tree.structure_identity.revision,
        )
        binding = build_requirement_enrichment_publication_binding_v2(
            started=rebuilt_starts[0],
            requirement_tree_object_key=attached[
                "requirement_tree_object_key"
            ],
            requirement_tree_sha256=tree_sha,
            requirement_tree_schema_version=tree.schema_version,
        )
        self.repository.finalize_publication_binding_v2(binding)
        self.assertEqual(
            db.get_requirement_set(retry.requirement_set_id)[
                "publication_state"
            ],
            "READY",
        )
        self.assertEqual(object_store.stat(target_key).sha256, tree_sha)

    def test_orphan_cleanup_guard_serializes_recreated_claim(self) -> None:
        (
            preview,
            _creator,
            retry,
            _tree,
            _object_store,
            _container,
            _target_key,
            _tree_sha,
        ) = self._aborted_retry_tree(object_root="guarded-reclaim-objects")
        begin_entered = threading.Event()
        begin_finished = threading.Event()
        rebuilt_starts = []
        failures = []

        def recreate() -> None:
            begin_entered.set()
            try:
                rebuilt_starts.append(
                    self.repository.begin_publication_binding_v2(
                        preview=preview,
                        current_projection=preview.projection,
                    )
                )
            except BaseException as exc:
                failures.append(exc)
            finally:
                begin_finished.set()

        with db.guard_unowned_requirement_tree_object_cleanup(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            requirement_set_id=retry.requirement_set_id,
        ) as cleanup_allowed:
            self.assertTrue(cleanup_allowed)
            worker = threading.Thread(target=recreate)
            worker.start()
            self.assertTrue(begin_entered.wait(timeout=1))
            self.assertFalse(begin_finished.wait(timeout=0.25))

        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(rebuilt_starts), 1)
        self.assertTrue(rebuilt_starts[0].created)
        self.assertEqual(
            db.get_requirement_set(retry.requirement_set_id)[
                "publication_state"
            ],
            "PUBLISHING",
        )

    def test_missing_set_does_not_delete_preexisting_exact_tree_object(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        creator = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        working_tree, *_classification = self._working_tree_and_classification()
        requirement_entry_ids = {
            item.requirement_tree_node_id: item.requirement_entry_id
            for item in creator.row_bindings
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=preview.projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        builder_tree = builder_tree.model_copy(
            update={
                "source_identity": builder_tree.source_identity.model_copy(
                    update={"requirement_set_id": creator.requirement_set_id}
                )
            }
        )
        tree = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=preview.projection,
            materialized_entries=(
                build_requirement_enrichment_publication_builder_entries_v2(
                    preview.projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
            ),
        )
        object_store = LocalObjectStore(self.root / "preexisting-race-objects")
        target_key = _tree_object_key(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=creator.requirement_set_id,
        )
        tree_payload = canonical_requirement_tree_bytes(tree)
        tree_sha = canonical_requirement_tree_sha256(tree)
        object_store.put(
            key=target_key,
            source=BytesIO(tree_payload),
            expected_sha256=tree_sha,
            media_type="application/json",
        )
        self.assertTrue(self.repository.abort_publication_binding_v2(creator))
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )

        with (
            patch.object(RUNTIME_STATE, "container", container),
            self.assertRaises(RequirementTreeSnapshotError),
        ):
            persist_requirement_tree_snapshot(
                requirement_set_id=creator.requirement_set_id,
                snapshot=tree,
            )

        self.assertEqual(object_store.stat(target_key).sha256, tree_sha)

    def test_publishing_cannot_activate_and_direct_v2_fork_requires_proof(self) -> None:
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        with self.assertRaisesRegex(PermissionError, "not READY"):
            db.activate_requirement_set(
                self.project["id"],
                self.document["id"],
                started.requirement_set_id,
            )

        binding = self._binding_after_synthetic_tree(preview, started)
        self.repository.finalize_publication_binding_v2(binding)
        activated = db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            started.requirement_set_id,
        )
        self.assertTrue(activated["is_active"])
        with self.assertRaisesRegex(PermissionError, "enrichment binding changed"):
            db.fork_requirement_set_for_edit_with_mapping(
                self.project["id"],
                self.document["id"],
                expected_source_requirement_set_id=started.requirement_set_id,
            )

    def test_ready_v2_publication_derives_a_verifiable_editable_draft(self) -> None:
        preview, started, tree, container = self._ready_not_run_publication()
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            started.requirement_set_id,
        )
        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=started.requirement_set_id,
        )

        with patch.object(RUNTIME_STATE, "container", container):
            result = derive_document_requirement_set(
                self.project["id"],
                self.document["id"],
                started.requirement_set_id,
            )
            draft = result["requirement_set"]
            loaded = load_requirement_set_tree_snapshot(draft)

        self.assertTrue(result["created"])
        self.assertTrue(draft["is_active"])
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["supersedes_id"], started.requirement_set_id)
        self.assertEqual(draft["publication_state"], "READY")
        self.assertEqual(len(draft["publication_key"]), 64)
        self.assertEqual(draft["enrichment_publication_binding_id"], "")
        self.assertEqual(
            draft["entry_classification_snapshot_id"],
            preview.projection.entry_classification_snapshot_id,
        )
        self.assertEqual(
            draft["entry_classification_snapshot_sha256"],
            preview.projection.entry_classification_sha256,
        )
        self.assertEqual(
            loaded.source_identity.requirement_set_id,
            draft["id"],
        )
        self.assertEqual(
            {node.id for node in loaded.nodes},
            {node.id for node in tree.nodes},
        )

    def test_ready_not_run_publication_loads_with_distinct_working_tree_id(self) -> None:
        preview, started, tree, container = self._ready_not_run_publication()
        repository_spy = Mock(wraps=self.repository)
        container.requirement_enrichment_repository = repository_spy
        with patch.object(RUNTIME_STATE, "container", container):
            requirement_set = db.get_requirement_set(started.requirement_set_id)
            loaded = load_requirement_set_tree_snapshot(requirement_set)

        self.assertEqual(loaded, tree)
        repository_spy.get_atomization.assert_not_called()
        repository_spy.get_atomization_scope_receipt_revision.assert_not_called()
        repository_spy.get_publication_preview.assert_not_called()
        self.assertEqual(
            preview.projection.accepted_working_tree_id,
            "accepted-working-tree-artifact-v2",
        )
        self.assertNotEqual(
            preview.projection.accepted_working_tree_id,
            preview.projection.accepted_working_tree_sha256,
        )

    def test_loader_rejects_tampered_database_row_trace(self) -> None:
        _preview, started, _tree, container = self._ready_not_run_publication()
        with sqlite3.connect(db.DB_PATH) as connection:
            requirement_id = connection.execute(
                """
                SELECT requirement_entry_id
                FROM requirement_enrichment_publication_row_bindings
                WHERE binding_id = ? ORDER BY publication_order LIMIT 1
                """,
                (started.binding_id,),
            ).fetchone()[0]
            connection.execute(
                """
                UPDATE requirements SET requirement_text = ? WHERE id = ?
                """,
                ("tampered publication text", requirement_id),
            )
        with (
            patch.object(RUNTIME_STATE, "container", container),
            self.assertRaisesRegex(
                RuntimeError,
                "database row or trace does not match",
            ),
        ):
            load_requirement_set_tree_snapshot(
                db.get_requirement_set(started.requirement_set_id)
            )

    def test_loader_rejects_every_tampered_canonical_decision_trace_field(self) -> None:
        _preview, started, _tree, container = self._ready_not_run_publication()
        with sqlite3.connect(db.DB_PATH) as connection:
            row = connection.execute(
                """
                SELECT requirement_entry_id
                FROM requirement_enrichment_publication_row_bindings
                WHERE binding_id = ? ORDER BY publication_order LIMIT 1
                """,
                (started.binding_id,),
            ).fetchone()
            requirement_id = row[0]
            original_json = connection.execute(
                "SELECT decision_reference_json FROM requirements WHERE id = ?",
                (requirement_id,),
            ).fetchone()[0]
        original = json.loads(original_json)
        self.assertEqual(
            set(original),
            {
                "schema_version",
                "binding_id",
                "binding_preview_id",
                "binding_preview_sha256",
                "projection_sha256",
                "projection_row_sha256",
                "decision_trace_sha256",
                "publication_order",
                "requirement_tree_node_id",
                "source_working_tree_node_id",
                "workflow_node_id",
                "atomization_node_id",
                "atomic_requirement_id",
                "decision_mode",
                "intrinsic_analysis_eligible",
                "default_disposition",
                "source_spans",
            },
        )
        mutations = {
            "schema_version": "tampered-trace/v9",
            "workflow_node_id": "review-tampered",
            "atomization_node_id": "atomization-node-" + "f" * 32,
            "atomic_requirement_id": "atomic-obligation-" + "f" * 32,
            "decision_mode": "ROLLUP",
            "intrinsic_analysis_eligible": (not bool(
                original["intrinsic_analysis_eligible"]
            )),
            "default_disposition": (
                "ANALYZE"
                if original["default_disposition"] != "ANALYZE"
                else "RETAIN"
            ),
            "source_spans": [
                {
                    "semantic_unit_id": "semantic-unit-" + "f" * 32,
                    "owner_structure_node_id": "structure-node-" + "e" * 32,
                    "start_offset": 0,
                    "end_offset": 1,
                }
            ],
        }
        requirement_set = db.get_requirement_set(started.requirement_set_id)
        with patch.object(RUNTIME_STATE, "container", container):
            for field, value in mutations.items():
                with self.subTest(field=field):
                    tampered = {**original, field: value}
                    with sqlite3.connect(db.DB_PATH) as connection:
                        connection.execute(
                            """
                            UPDATE requirements SET decision_reference_json = ?
                            WHERE id = ?
                            """,
                            (
                                json.dumps(
                                    tampered,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ),
                                requirement_id,
                            ),
                        )
                    clear_verified_requirement_tree_snapshot_cache()
                    with self.assertRaisesRegex(
                        RequirementTreeSnapshotError,
                        "database row or trace",
                    ):
                        load_requirement_set_tree_snapshot(requirement_set)
                    with sqlite3.connect(db.DB_PATH) as connection:
                        connection.execute(
                            """
                            UPDATE requirements SET decision_reference_json = ?
                            WHERE id = ?
                            """,
                            (original_json, requirement_id),
                        )
            extra = {**original, "untrusted_extra": True}
            with sqlite3.connect(db.DB_PATH) as connection:
                connection.execute(
                    "UPDATE requirements SET decision_reference_json = ? WHERE id = ?",
                    (
                        json.dumps(
                            extra,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        requirement_id,
                    ),
                )
            clear_verified_requirement_tree_snapshot_cache()
            with self.assertRaisesRegex(
                RequirementTreeSnapshotError,
                "database row or trace",
            ):
                load_requirement_set_tree_snapshot(requirement_set)

    def test_ready_retry_reuses_exact_set_before_and_after_activation(self) -> None:
        preview, started, tree, container = self._ready_not_run_publication()
        payload = RequirementPublishDraftRequestV2(
            expected_review_revision=preview.review_revision,
            binding_preview_id=preview.binding_preview_id,
            binding_preview_sha256=preview.payload_sha256,
        )
        session = SimpleNamespace(
            session_id=preview.session_id,
            reviewed_result=SimpleNamespace(revision=preview.review_revision),
        )
        with (
            patch.object(RUNTIME_STATE, "container", container),
            patch(
                "app.application.extraction."
                "requirement_enrichment_publication_transport_v2."
                "_recompute_projection_from_preview_v2",
                side_effect=AssertionError("READY replay must not recompute"),
            ),
        ):
            inactive_binding, inactive_tree, inactive_created = (
                publish_requirement_enrichment_draft_v2(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    document=db.get_sor_document(self.document["id"]),
                    session=session,
                    payload=payload,
                    repository=self.repository,
                )
            )
            db.activate_requirement_set(
                self.project["id"],
                self.document["id"],
                started.requirement_set_id,
            )
            active_binding, active_tree, active_created = (
                publish_requirement_enrichment_draft_v2(
                    project_id=self.project["id"],
                    document_id=self.document["id"],
                    document=db.get_sor_document(self.document["id"]),
                    session=session,
                    payload=payload,
                    repository=self.repository,
                )
            )

        self.assertFalse(inactive_created)
        self.assertFalse(active_created)
        self.assertEqual(inactive_binding, active_binding)
        self.assertEqual(inactive_binding.requirement_set_id, started.requirement_set_id)
        self.assertEqual(inactive_tree, tree)
        self.assertEqual(active_tree, tree)

    def test_postgresql_0015_and_cutover_preserve_binding_relationships(self) -> None:
        _preview, started, _tree, _container = self._ready_not_run_publication()
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0015_requirement_enrichment_publication_v2.sql"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "requirement_enrichment_publication_binding_previews",
            migration,
        )
        self.assertIn("requirement_enrichment_publication_row_bindings", migration)
        self.assertNotIn("projection_sha256 TEXT NOT NULL UNIQUE", migration)

        tables = (
            "requirement_sets",
            "requirements",
            "entry_classification_snapshots",
            "requirement_atomization_runs",
            "requirement_atomization_scope_receipts",
            "requirement_enrichment_publication_previews",
            "requirement_enrichment_publication_binding_previews",
            "requirement_enrichment_publication_bindings",
            "requirement_enrichment_publication_row_bindings",
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.row_factory = sqlite3.Row
            rows = {
                table: tuple(
                    dict(item)
                    for item in connection.execute(f'SELECT * FROM "{table}"')
                )
                for table in tables
            }
        _validate_requirement_enrichment_publication_relationships(rows)
        tampered_sets = tuple(
            {
                **row,
                "enrichment_publication_binding_sha256": "f" * 64,
            }
            if row["id"] == started.requirement_set_id
            else row
            for row in rows["requirement_sets"]
        )
        with self.assertRaisesRegex(CutoverRejected, "READY enrichment binding"):
            _validate_requirement_enrichment_publication_relationships(
                {**rows, "requirement_sets": tampered_sets}
            )
        tampered_requirements = tuple(
            {
                **row,
                "decision_reference_json": json.dumps(
                    {
                        **json.loads(
                            row["decision_reference_json"]
                        ),
                        "default_disposition": "TAMPERED",
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
            if row["requirement_set_id"] == started.requirement_set_id
            else row
            for row in rows["requirements"]
        )
        with self.assertRaisesRegex(CutoverRejected, "decision trace"):
            _validate_requirement_enrichment_publication_relationships(
                {**rows, "requirements": tampered_requirements}
            )

    def _prepare_applied_artifacts(self):
        (
            working_tree,
            initial_classification,
            working_classification,
            accepted_classification,
        ) = self._working_tree_and_classification(
            _accepted_working_tree_with_second_technical()
        )
        append_entry_classification_snapshot(
            base_snapshot=initial_classification,
            next_snapshot=working_classification,
            command_id="publication-v2-applied-classify",
            command_sha256="1" * 64,
            expected_revision=initial_classification.revision,
            expected_snapshot_sha256=initial_classification.payload_sha256,
        )
        receipt, run, core_preview = _applied_publication_inputs(
            working_tree,
            accepted_classification,
            include_shared_one_to_one=True,
            receipt_classification=working_classification,
            base_active_requirement_set_id=self.active["id"],
        )
        receipt = _receipt_with_updates(
            receipt,
            cumulative_review_node_ids=(_TECHNICAL_REVIEW_ID,),
            pass_through_review_node_ids=(_SECOND_TECHNICAL_REVIEW_ID,),
        )
        attempt_fields = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "lineage_sha256": run.lineage.lineage_sha256,
            "attempt_sequence": 1,
            "requested": receipt.requested_review_node_ids,
            "executed": receipt.executed_review_node_ids,
            "skipped": receipt.skipped_targets,
            "cumulative": receipt.cumulative_review_node_ids,
            "failed": (),
            "checkpoint_reused": (),
            "checkpoint_invalidated": (),
            "retryable": False,
            "error_code": "",
            "started_at": run.created_at_utc,
            "updated_at": run.created_at_utc,
            "session_id": run.lineage.session_id,
            "review_revision": run.lineage.review_revision,
            "command_id": receipt.command_id,
            "command_sha256": receipt.command_sha256,
            "expected_scope_revision": 0,
            "operation": receipt.operation,
            "scope": receipt.scope,
        }
        running = _build_scope_attempt(
            **attempt_fields,
            status=AtomizationScopeAttemptStatus.RUNNING,
            completed=(),
            checkpoint_created=(),
            provider_call_count=0,
            run=None,
            receipt=None,
        )
        self.repository.begin_atomization_scope_attempt(
            attempt=running,
            expected_scope_revision=0,
        )
        succeeded = _build_scope_attempt(
            **attempt_fields,
            status=AtomizationScopeAttemptStatus.SUCCEEDED,
            completed=receipt.succeeded_review_node_ids,
            checkpoint_created=receipt.new_review_node_ids,
            provider_call_count=receipt.provider_call_count,
            run=run,
            receipt=receipt,
        )
        stored_run, stored_receipt, created = (
            self.repository.create_atomization_scope_revision(
                run=run,
                receipt=receipt,
                attempt=succeeded,
                expected_scope_revision=0,
            )
        )
        self.assertTrue(created)
        self.assertEqual(stored_run, run)
        self.assertEqual(stored_receipt, receipt)
        append_entry_classification_snapshot(
            base_snapshot=working_classification,
            next_snapshot=accepted_classification,
            command_id="publication-v2-applied-accept",
            command_sha256="2" * 64,
            expected_revision=working_classification.revision,
            expected_snapshot_sha256=working_classification.payload_sha256,
        )
        self.repository.create_or_get_publication_preview(core_preview)
        return (
            working_tree,
            accepted_classification,
            receipt,
            run,
            core_preview,
        )

    def _applied_binding_preview(self):
        working_tree, accepted, receipt, run, core_preview = (
            self._prepare_applied_artifacts()
        )
        projection = build_requirement_enrichment_publication_projection_v2(
            accepted_working_tree=working_tree,
            accepted_entry_classification=accepted,
            atomization_state=RequirementEnrichmentAtomizationStateV2.APPLIED,
            atomization_scope_receipt=receipt,
            atomization_run=run,
            publication_preview=core_preview,
        )
        preview = build_requirement_enrichment_publication_binding_preview_v2(
            session_id=run.lineage.session_id,
            review_revision=run.lineage.review_revision,
            base_active_requirement_set_id=self.active["id"],
            projection=projection,
        )
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            preview
        )
        return preview, working_tree, accepted, receipt, run

    def _append_changed_accepted_classification(
        self,
        accepted: EntryClassificationSnapshotV1,
        *,
        suffix: str,
    ) -> EntryClassificationSnapshotV1:
        target = accepted.entries[0]
        replacement = (
            EntryClassificationType.TECHNICAL
            if target.effective_type is not EntryClassificationType.TECHNICAL
            else EntryClassificationType.INFORMATION
        )
        working = revise_entry_classification_snapshot(
            accepted,
            overrides={target.workflow_node_id: replacement},
            snapshot_id=f"classification-working-{suffix}",
            created_at="2026-08-07T08:02:00+00:00",
        )
        append_entry_classification_snapshot(
            base_snapshot=accepted,
            next_snapshot=working,
            command_id=f"publication-v2-reclassify-{suffix}",
            command_sha256="3" * 64,
            expected_revision=accepted.revision,
            expected_snapshot_sha256=accepted.payload_sha256,
        )
        changed = accept_entry_classification_snapshot(
            working,
            snapshot_id=f"classification-accepted-{suffix}",
            accepted_at="2026-08-07T08:03:00+00:00",
            accepted_by="publication-v2-toctou-test",
        )
        append_entry_classification_snapshot(
            base_snapshot=working,
            next_snapshot=changed,
            command_id=f"publication-v2-reaccept-{suffix}",
            command_sha256="4" * 64,
            expected_revision=working.revision,
            expected_snapshot_sha256=working.payload_sha256,
        )
        return changed

    def _append_undo_scope_revision(
        self,
        *,
        receipt,
        run,
        accepted_classification: EntryClassificationSnapshotV1,
        command_suffix: str,
    ):
        next_run = _copy_scope_run_revision(run, revision=run.revision + 1)
        command_sha256 = "9" * 64
        pass_through = tuple(
            dict.fromkeys(
                (
                    *receipt.cumulative_review_node_ids,
                    *receipt.pass_through_review_node_ids,
                )
            )
        )
        next_receipt = _receipt_with_updates(
            receipt,
            entry_classification_snapshot_id=accepted_classification.snapshot_id,
            entry_classification_revision=accepted_classification.revision,
            entry_classification_sha256=accepted_classification.payload_sha256,
            entry_classification_content_sha256=(
                stable_effective_entry_classification_sha256(
                    accepted_classification
                )
            ),
            scope_revision=next_run.revision,
            active_revision=0,
            previous_active_revision=receipt.active_revision,
            supersedes_receipt_id=receipt.receipt_id,
            command_id=f"publication-v2-undo-{command_suffix}",
            command_sha256=command_sha256,
            operation=AtomizationScopeOperation.UNDO,
            scope=AtomizationTargetScope.CURRENT,
            target_scope_sha256=command_sha256,
            requested_review_node_ids=(),
            executed_review_node_ids=(),
            new_review_node_ids=(),
            reused_review_node_ids=(),
            skipped_targets=(),
            succeeded_review_node_ids=(),
            failed_review_node_ids=(),
            pass_through_review_node_ids=pass_through,
            cumulative_review_node_ids=(),
            provider_call_count=0,
            checkpoint_created_count=0,
            checkpoint_reuse_count=0,
            atomization_run_id=next_run.atomization_run_id,
            atomization_run_revision=next_run.revision,
            created_at_utc=next_run.created_at_utc,
        )
        attempt_fields = {
            "project_id": self.project["id"],
            "document_id": self.document["id"],
            "lineage_sha256": run.lineage.lineage_sha256,
            "attempt_sequence": 2,
            "requested": (),
            "executed": (),
            "skipped": (),
            "cumulative": (),
            "failed": (),
            "checkpoint_created": (),
            "checkpoint_reused": (),
            "checkpoint_invalidated": (),
            "provider_call_count": 0,
            "retryable": False,
            "error_code": "",
            "started_at": next_run.created_at_utc,
            "updated_at": next_run.created_at_utc,
            "session_id": run.lineage.session_id,
            "review_revision": run.lineage.review_revision,
            "command_id": next_receipt.command_id,
            "command_sha256": next_receipt.command_sha256,
            "expected_scope_revision": receipt.scope_revision,
            "operation": next_receipt.operation,
            "scope": next_receipt.scope,
        }
        running = _build_scope_attempt(
            **attempt_fields,
            status=AtomizationScopeAttemptStatus.RUNNING,
            completed=(),
            run=None,
            receipt=None,
        )
        self.repository.begin_atomization_scope_attempt(
            attempt=running,
            expected_scope_revision=receipt.scope_revision,
        )
        succeeded = _build_scope_attempt(
            **attempt_fields,
            status=AtomizationScopeAttemptStatus.SUCCEEDED,
            completed=(),
            run=next_run,
            receipt=next_receipt,
        )
        self.repository.create_atomization_scope_revision(
            run=next_run,
            receipt=next_receipt,
            attempt=succeeded,
            expected_scope_revision=receipt.scope_revision,
        )
        return next_receipt

    def _ready_not_run_publication(self):
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        started = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        working_tree, *_classification = self._working_tree_and_classification()
        requirement_entry_ids = {
            item.requirement_tree_node_id: item.requirement_entry_id
            for item in started.row_bindings
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=preview.projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        builder_tree = builder_tree.model_copy(
            update={
                "source_identity": builder_tree.source_identity.model_copy(
                    update={"requirement_set_id": started.requirement_set_id}
                )
            }
        )
        tree = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=preview.projection,
            materialized_entries=(
                build_requirement_enrichment_publication_builder_entries_v2(
                    preview.projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
            ),
        )
        object_store = LocalObjectStore(self.root / "objects")
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )
        with patch.object(RUNTIME_STATE, "container", container):
            persisted = persist_requirement_tree_snapshot(
                requirement_set_id=started.requirement_set_id,
                snapshot=tree,
            )
            binding = build_requirement_enrichment_publication_binding_v2(
                started=started,
                requirement_tree_object_key=persisted[
                    "requirement_tree_object_key"
                ],
                requirement_tree_sha256=canonical_requirement_tree_sha256(tree),
                requirement_tree_schema_version=tree.schema_version,
            )
            self.repository.finalize_publication_binding_v2(binding)
        return preview, started, tree, container

    def _aborted_retry_tree(self, *, object_root: str):
        preview = self.repository.create_or_get_publication_binding_preview_v2(
            self._preview()
        )
        creator = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        retry = self.repository.begin_publication_binding_v2(
            preview=preview,
            current_projection=preview.projection,
        )
        self.assertTrue(creator.created)
        self.assertFalse(retry.created)
        working_tree, *_classification = self._working_tree_and_classification()
        requirement_entry_ids = {
            item.requirement_tree_node_id: item.requirement_entry_id
            for item in retry.row_bindings
        }
        builder_tree = _builder_like_tree(
            working_tree=working_tree,
            projection=preview.projection,
            requirement_entry_ids=requirement_entry_ids,
        )
        builder_tree = builder_tree.model_copy(
            update={
                "source_identity": builder_tree.source_identity.model_copy(
                    update={"requirement_set_id": retry.requirement_set_id}
                )
            }
        )
        tree = overlay_requirement_enrichment_publication_tree_v2(
            accepted_working_tree=working_tree,
            builder_tree=builder_tree,
            projection=preview.projection,
            materialized_entries=(
                build_requirement_enrichment_publication_builder_entries_v2(
                    preview.projection,
                    requirement_entry_ids=requirement_entry_ids,
                )
            ),
        )
        object_store = LocalObjectStore(self.root / object_root)
        container = SimpleNamespace(
            object_store=object_store,
            requirement_enrichment_repository=self.repository,
        )
        self.assertTrue(self.repository.abort_publication_binding_v2(creator))
        target_key = _tree_object_key(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=retry.requirement_set_id,
        )
        return (
            preview,
            creator,
            retry,
            tree,
            object_store,
            container,
            target_key,
            canonical_requirement_tree_sha256(tree),
        )

    def _binding_after_synthetic_tree(self, preview, started):
        tree_sha256 = "b" * 64
        tree_object_key = "requirement-trees/w4.json"
        with sqlite3.connect(db.DB_PATH) as connection:
            connection.execute(
                """
                UPDATE requirement_sets
                   SET requirement_tree_object_key = ?,
                       requirement_tree_sha256 = ?,
                       requirement_tree_schema_version = ?,
                       structure_snapshot_id = ?,
                       structure_tree_sha256 = ?, structure_revision = 1
                 WHERE id = ?
                """,
                (
                    tree_object_key,
                    tree_sha256,
                    "requirement-tree/v1",
                    preview.projection.effective_structure_id,
                    "c" * 64,
                    started.requirement_set_id,
                ),
            )
        return build_requirement_enrichment_publication_binding_v2(
            started=started,
            requirement_tree_object_key=tree_object_key,
            requirement_tree_sha256=tree_sha256,
            requirement_tree_schema_version="requirement-tree/v1",
        )


if __name__ == "__main__":
    unittest.main()
