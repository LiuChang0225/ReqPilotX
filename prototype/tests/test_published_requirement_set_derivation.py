from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
    from .test_working_tree_publication import (
        _bound_workbench,
        _payload,
        _persist_classification_head,
        _publication_source,
    )
else:
    from runtime_isolation import activate_test_runtime
    from test_working_tree_publication import (
        _bound_workbench,
        _payload,
        _persist_classification_head,
        _publication_source,
    )

activate_test_runtime()

import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from app import db
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application.extraction.requirement_tree import (
    build_requirement_tree_snapshot,
)
from app.application.extraction.requirement_tree_snapshots import (
    clear_verified_requirement_tree_snapshot_cache,
    load_requirement_set_tree_snapshot,
)
from app.application.extraction.working_tree_publication import (
    publish_working_requirement_tree_draft,
)
from app.application.requirements import (
    derive_document_requirement_set,
    get_document_lock_readiness,
    lock_document_extraction,
)
from app.application.errors import ApplicationError
from app.application.requirements_contracts import LockExtractionRequest
from app.application.requirement_matrix import (
    build_requirement_matrix_snapshot,
    patch_requirement_matrix_node_disposition,
    post_requirement_matrix_node_decision,
    select_matrix_analysis_requirements,
)
from app.application.requirement_matrix_contracts import (
    MatrixDecisionRequestV1,
    MatrixDispositionPatchV1,
)
from app.application.requirement_set_impact_service import (
    REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION,
    requirement_analysis_input_sha256,
)
from app.domain.requirement_matrix import MatrixNodeDisposition
from app.adapters.persistence.requirement_matrix_states import (
    list_requirement_matrix_node_states,
)
from app.application.runtime import RUNTIME_STATE
from app.domain.entry_classification import EntryClassificationType


SOURCE_SHA256 = "a" * 64


class PublishedRequirementSetDerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_verified_requirement_tree_snapshot_cache()
        self.runtime_lineage_patch = patch(
            "app.application.requirement_matrix."
            "_requirement_set_has_current_runtime_lineage",
            return_value=True,
        )
        self.runtime_lineage_patch.start()
        self.addCleanup(self.runtime_lineage_patch.stop)
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.root = Path(self.temporary_directory.name)
        self.database_patch = patch.object(db, "DB_PATH", self.root / "app.db")
        self.database_patch.start()
        self.addCleanup(self.database_patch.stop)
        self.addCleanup(self.temporary_directory.cleanup)
        self.object_store = LocalObjectStore(self.root / "objects")
        self.container_patch = patch.object(
            RUNTIME_STATE,
            "container",
            SimpleNamespace(object_store=self.object_store),
        )
        self.container_patch.start()
        self.addCleanup(self.container_patch.stop)
        self.addCleanup(clear_verified_requirement_tree_snapshot_cache)
        db.init_db()
        self.project = db.create_project("Derivation", "Customer")
        source_path = self.root / "source.pdf"
        source_path.write_bytes(b"source")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="source.pdf",
            file_type="pdf",
            file_path=str(source_path),
            extracted_text="",
            source_sha256=SOURCE_SHA256,
        )

    def _publish_and_lock(self, *, suffix: str):
        payload = _payload()
        workbench = _bound_workbench(
            self.project["id"], self.document["id"]
        )
        working_tree = build_requirement_tree_snapshot(
            workbench=workbench,
            payload=payload,
        )
        accepted = _persist_classification_head(
            working_tree,
            entry_type=EntryClassificationType.TECHNICAL,
            suffix=suffix,
            accept=True,
        )
        with _publication_source(
            document=self.document,
            payload=payload,
            current_workbench=lambda: workbench,
        ):
            publication = publish_working_requirement_tree_draft(
                self.project["id"],
                self.document["id"],
                SimpleNamespace(headers={}),
            )

        source_set_id = publication["publication"]["requirement_set_id"]
        db.activate_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set_id,
        )
        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=source_set_id,
        )
        return db.get_requirement_set(source_set_id) or {}, accepted

    def _seed_current_analysis_result(
        self,
        requirement_set_id: str,
        *,
        reason: str = "Evidence is sufficient.",
    ) -> dict:
        matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            requirement_set_id,
        )
        requirements = db.list_requirements(
            self.project["id"],
            self.document["id"],
            requirement_set_id=requirement_set_id,
        )
        candidate_node = next(
            node for node in matrix.nodes if node.requirement_id
        )
        with db.get_conn() as connection:
            current_state = connection.execute(
                """
                SELECT id FROM requirement_matrix_node_states
                WHERE requirement_set_id = ? AND node_id = ?
                """,
                (requirement_set_id, candidate_node.node_id),
            ).fetchone()
            if current_state is None:
                connection.execute(
                    """
                    INSERT INTO requirement_matrix_node_states (
                        id, project_id, sor_document_id, requirement_set_id,
                        node_id, disposition, reason, revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'ANALYZE',
                              'test analysis selection', 1, ?, ?)
                    """,
                    (
                        f"analysis-state-{candidate_node.node_id}",
                        self.project["id"],
                        self.document["id"],
                        requirement_set_id,
                        candidate_node.node_id,
                        "2026-08-08T00:00:00+00:00",
                        "2026-08-08T00:00:00+00:00",
                    ),
                )
        matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            requirement_set_id,
        )
        analysis_row = select_matrix_analysis_requirements(
            matrix,
            requirements,
        )[0]
        db.upsert_assessment(
            analysis_row["id"],
            profile_id="profile-1",
            provider="provider-1",
            ai_decision="COMPLIANT",
            ai_reason=reason,
            ai_confidence=0.91,
            assessment_mode="llm_primary",
            retrieval_backend="local",
            analysis_label="REQUIREMENT",
            analysis_basis="FULL",
            normalized_requirement="safe state within 100 ms",
            risk_level="LOW",
            clarification_question="",
            model_name="model-1",
            prompt_version="prompt-v1",
            compliance_status="COMPLIANT",
            analysis_input_schema_version=(
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            ),
            analysis_input_sha256=requirement_analysis_input_sha256(
                analysis_row
            ),
        )
        return analysis_row

    def test_locked_w3_publication_derives_a_readable_editable_draft(self) -> None:
        source_set, accepted = self._publish_and_lock(
            suffix="derive-regression"
        )
        source_set_id = source_set["id"]

        result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set_id,
        )

        self.assertTrue(result["created"])
        draft = result["requirement_set"]
        self.assertEqual(draft["status"], "DRAFT")
        self.assertTrue(draft["is_active"])
        self.assertEqual(draft["supersedes_id"], source_set_id)
        self.assertEqual(
            draft["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            draft["entry_classification_snapshot_sha256"],
            accepted.payload_sha256,
        )
        self.assertEqual(draft["publication_state"], "READY")
        self.assertNotEqual(
            draft["publication_key"], source_set["publication_key"]
        )

        loaded = load_requirement_set_tree_snapshot(draft)

        self.assertEqual(
            loaded.source_identity.requirement_set_id,
            draft["id"],
        )
        self.assertIn(
            "TECHNICAL",
            {node.classification for node in loaded.nodes},
        )

        readiness = get_document_lock_readiness(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        self.assertEqual(
            readiness["count_metadata"],
            {
                "content_classification": "AVAILABLE",
                "entry_composition": "AVAILABLE",
                "source": "FROZEN_TREE",
            },
        )
        self.assertNotIn(
            "LEGACY_BREAKDOWN_METADATA_UNAVAILABLE",
            {warning["code"] for warning in readiness["warnings"]},
        )

    def test_database_fork_rejects_published_source_without_verified_tree_key(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="unsafe-db-fork")
        versions_before = db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.fork_requirement_set_for_edit(
                self.project["id"], self.document["id"]
            )

        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            versions_before,
        )
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            source_set["id"],
        )

    def test_unlock_retains_only_proven_current_result_and_manual_decision(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="impact-retain")
        requirements = db.list_requirements(
            self.project["id"],
            self.document["id"],
            requirement_set_id=source_set["id"],
        )
        matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )
        eligible_node = next(
            node for node in matrix.nodes if node.requirement_id
        )
        list_requirement_matrix_node_states(
            project_id=self.project["id"],
            document_id=self.document["id"],
            requirement_set_id=source_set["id"],
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                INSERT INTO requirement_matrix_node_states (
                    id, project_id, sor_document_id, requirement_set_id,
                    node_id, disposition, reason, revision,
                    created_at, updated_at
                ) VALUES (
                    'impact-source-state', ?, ?, ?, ?, 'ANALYZE',
                    'test source selection', 1,
                    '2026-08-08T00:00:00+00:00',
                    '2026-08-08T00:00:00+00:00'
                )
                """,
                (
                    self.project["id"],
                    self.document["id"],
                    source_set["id"],
                    eligible_node.node_id,
                ),
            )
        matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )
        target = select_matrix_analysis_requirements(matrix, requirements)[0]
        source_requirement_id = target["id"]
        source_node_id = target["matrix_node_id"]
        db.upsert_assessment(
            source_requirement_id,
            profile_id="profile-1",
            provider="provider-1",
            ai_decision="COMPLIANT",
            ai_reason="Evidence is sufficient.",
            ai_confidence=0.91,
            assessment_mode="llm_primary",
            retrieval_backend="local",
            analysis_label="REQUIREMENT",
            analysis_basis="FULL",
            normalized_requirement="safe state within 100 ms",
            risk_level="LOW",
            clarification_question="",
            model_name="model-1",
            prompt_version="prompt-v1",
            compliance_status="COMPLIANT",
            analysis_input_schema_version=(
                REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
            ),
            analysis_input_sha256=requirement_analysis_input_sha256(target),
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE assessments
                SET final_decision = 'ACCEPT', engineer_reason = '已人工确认'
                WHERE requirement_id = ?
                """,
                (source_requirement_id,),
            )

        result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )

        receipt = result["impact_receipt"]
        self.assertEqual(receipt["retained_result_node_ids"], [source_node_id])
        self.assertEqual(receipt["stale_result_node_ids"], [])
        self.assertEqual(result["affected_result_count"], 0)
        draft = result["requirement_set"]
        derived_requirement = db.get_requirement_in_set_by_prior(
            draft["id"],
            source_requirement_id,
        )
        self.assertIsNotNone(derived_requirement)
        carried = db.get_assessment(derived_requirement["id"])
        self.assertEqual(carried["reference_status"], "CURRENT")
        self.assertEqual(carried["result_origin"], "CARRIED")
        self.assertEqual(
            carried["source_assessment_id"],
            db.get_assessment(source_requirement_id)["id"],
        )
        self.assertEqual(carried["final_decision"], "ACCEPT")
        self.assertEqual(carried["engineer_reason"], "已人工确认")
        self.assertEqual(
            carried["impact_receipt_id"],
            receipt["receipt_id"],
        )
        self.assertEqual(
            db.get_assessment(source_requirement_id)["result_origin"],
            "NATIVE",
        )

        draft_matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        draft_node = next(
            node for node in draft_matrix.nodes if node.node_id == source_node_id
        )
        changed = patch_requirement_matrix_node_disposition(
            self.project["id"],
            self.document["id"],
            draft["id"],
            source_node_id,
            MatrixDispositionPatchV1(
                disposition=MatrixNodeDisposition.RETAIN,
                reason="本轮不分析",
                expected_revision=draft_node.save_revision,
            ),
        )
        self.assertEqual(
            changed["impact_receipt"]["stale_result_node_ids"],
            [source_node_id],
        )
        self.assertEqual(
            changed["impact_receipt"]["changed_node_ids"],
            [source_node_id],
        )
        stale = db.get_assessment(derived_requirement["id"])
        self.assertEqual(stale["reference_status"], "STALE_REFERENCE")
        self.assertIsNone(stale["final_decision"])
        readiness = get_document_lock_readiness(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        self.assertEqual(readiness["counts"]["stale_analysis_results"], 0)
        self.assertNotIn(
            "STALE_ANALYSIS_RESULTS_REQUIRE_RERUN",
            {warning["code"] for warning in readiness["warnings"]},
        )

        restored = patch_requirement_matrix_node_disposition(
            self.project["id"],
            self.document["id"],
            draft["id"],
            source_node_id,
            MatrixDispositionPatchV1(
                disposition=MatrixNodeDisposition.ANALYZE,
                reason="恢复分析",
                expected_revision=changed["node"]["save_revision"],
            ),
        )
        self.assertEqual(
            restored["impact_receipt"]["retained_result_node_ids"],
            [source_node_id],
        )
        self.assertEqual(
            restored["impact_receipt"]["stale_result_node_ids"],
            [],
        )
        current_again = db.get_assessment(derived_requirement["id"])
        self.assertEqual(current_again["reference_status"], "CURRENT")
        self.assertEqual(current_again["final_decision"], "ACCEPT")

    def test_database_fork_rejects_wrong_but_well_formed_publication_basis(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="wrong-db-basis")
        versions_before = db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.fork_requirement_set_for_edit_with_mapping(
                self.project["id"],
                self.document["id"],
                verified_source_publication_basis_key="0" * 64,
            )

        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            versions_before,
        )
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            source_set["id"],
        )

    def test_strict_lock_rejects_matrix_change_after_readiness_comparison(self) -> None:
        source_set, _ = self._publish_and_lock(suffix="lock-state-cas")
        draft = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )["requirement_set"]
        matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        node = next(item for item in matrix.nodes if item.requirement_id)
        readiness = get_document_lock_readiness(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        original_lock = db.lock_sor_document_extraction

        def mutate_then_lock(document_id: str, **kwargs):
            patch_requirement_matrix_node_disposition(
                self.project["id"],
                self.document["id"],
                draft["id"],
                node.node_id,
                MatrixDispositionPatchV1(
                    disposition=MatrixNodeDisposition.RETAIN,
                    reason="并发修改",
                    expected_revision=node.save_revision,
                ),
            )
            return original_lock(document_id, **kwargs)

        with patch.object(
            db,
            "lock_sor_document_extraction",
            side_effect=mutate_then_lock,
        ), self.assertRaises(ApplicationError) as raised:
            lock_document_extraction(
                self.project["id"],
                self.document["id"],
                LockExtractionRequest(
                    requirement_set_id=draft["id"],
                    schema_version="requirement-set-lock-request/v1",
                    expected_readiness_sha256=readiness["readiness_sha256"],
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "requirement_set_lock_readiness_changed",
        )
        self.assertEqual(
            (db.get_requirement_set(draft["id"]) or {})["status"],
            "DRAFT",
        )

    def test_strict_lock_rejects_evidence_change_after_readiness_comparison(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="lock-evidence-cas")
        draft = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )["requirement_set"]
        requirement = db.list_requirements(
            self.project["id"],
            self.document["id"],
            requirement_set_id=draft["id"],
        )[0]
        readiness = get_document_lock_readiness(
            self.project["id"],
            self.document["id"],
            draft["id"],
        )
        original_lock = db.lock_sor_document_extraction

        def mutate_then_lock(document_id: str, **kwargs):
            db.replace_evidences(
                requirement["id"],
                [
                    {
                        "source_doc_id": "knowledge-late",
                        "source_doc_name": "late.txt",
                        "source_doc_version": "v1",
                        "source_location": "line 1",
                        "excerpt": "late evidence",
                        "relevance": 0.9,
                    }
                ],
            )
            return original_lock(document_id, **kwargs)

        with patch.object(
            db,
            "lock_sor_document_extraction",
            side_effect=mutate_then_lock,
        ), self.assertRaises(ApplicationError) as raised:
            lock_document_extraction(
                self.project["id"],
                self.document["id"],
                LockExtractionRequest(
                    requirement_set_id=draft["id"],
                    schema_version="requirement-set-lock-request/v1",
                    expected_readiness_sha256=readiness["readiness_sha256"],
                ),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "requirement_set_lock_readiness_changed",
        )
        self.assertEqual(
            (db.get_requirement_set(draft["id"]) or {})["status"],
            "DRAFT",
        )

    def test_failed_staging_derivation_never_replaces_or_breaks_locked_source(self) -> None:
        source_set, _ = self._publish_and_lock(suffix="staging-failure")
        from app.application.extraction.requirement_tree_snapshots import (
            clone_requirement_set_tree_snapshot,
        )

        captured: dict[str, str] = {}

        def inspect_staging(**kwargs):
            target = kwargs["target_requirement_set"]
            captured["target_id"] = str(target["id"])
            self.assertFalse(bool(int(target["is_active"] or 0)))
            self.assertEqual(
                (db.get_active_requirement_set(self.document["id"]) or {})["id"],
                source_set["id"],
            )
            return clone_requirement_set_tree_snapshot(**kwargs)

        with patch(
            "app.application.extraction.requirement_tree_snapshots."
            "clone_requirement_set_tree_snapshot",
            side_effect=inspect_staging,
        ), patch(
            "app.adapters.persistence.requirement_set_impacts."
            "apply_requirement_set_impact_receipt",
            side_effect=TypeError("injected unexpected impact failure"),
        ), self.assertRaises(ApplicationError):
            derive_document_requirement_set(
                self.project["id"],
                self.document["id"],
                source_set["id"],
            )

        target_id = captured["target_id"]
        self.assertIsNone(db.get_requirement_set(target_id))
        active = db.get_active_requirement_set(self.document["id"]) or {}
        self.assertEqual(active["id"], source_set["id"])
        self.assertEqual(active["status"], "LOCKED")
        self.assertFalse(
            any(
                path.is_file() and target_id in str(path)
                for path in self.root.rglob("*")
            )
        )

    def test_staged_finalize_rejects_source_decision_written_after_impact_apply(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="finalize-source-race")
        analysis_row = self._seed_current_analysis_result(source_set["id"])
        source_matrix = build_requirement_matrix_snapshot(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )
        source_node = next(
            node
            for node in source_matrix.nodes
            if node.node_id == analysis_row["matrix_node_id"]
        )
        from app.adapters.persistence.requirement_set_impacts import (
            finalize_staged_requirement_set_activation,
        )

        captured: dict[str, str] = {}

        def decide_then_finalize(receipt, **kwargs):
            captured["target_id"] = receipt.new_requirement_set_id
            post_requirement_matrix_node_decision(
                self.project["id"],
                self.document["id"],
                source_set["id"],
                source_node.node_id,
                MatrixDecisionRequestV1(
                    final_decision="ACCEPT",
                    engineer_reason="并发人工确认",
                    expected_revision=source_node.save_revision,
                ),
            )
            return finalize_staged_requirement_set_activation(
                receipt,
                **kwargs,
            )

        with patch(
            "app.adapters.persistence.requirement_set_impacts."
            "finalize_staged_requirement_set_activation",
            side_effect=decide_then_finalize,
        ), self.assertRaises(ApplicationError):
            derive_document_requirement_set(
                self.project["id"],
                self.document["id"],
                source_set["id"],
            )

        target_id = captured["target_id"]
        self.assertIsNone(db.get_requirement_set(target_id))
        active = db.get_active_requirement_set(self.document["id"]) or {}
        self.assertEqual(active["id"], source_set["id"])
        self.assertEqual(active["status"], "LOCKED")
        source_assessment = db.get_assessment(analysis_row["id"]) or {}
        self.assertEqual(source_assessment["final_decision"], "ACCEPT")
        self.assertEqual(source_assessment["engineer_reason"], "并发人工确认")

    def test_failed_cleanup_never_deletes_a_concurrently_activated_target(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="finalize-cleanup-race")
        from app.adapters.persistence.requirement_set_impacts import (
            apply_requirement_set_impact_receipt,
            finalize_staged_requirement_set_activation,
        )

        captured: dict[str, str] = {}

        def activate_then_fail(receipt, **kwargs):
            result = apply_requirement_set_impact_receipt(receipt, **kwargs)
            captured["target_id"] = receipt.new_requirement_set_id
            finalize_staged_requirement_set_activation(
                result.receipt,
                analysis_input_schema_version=kwargs[
                    "analysis_input_schema_version"
                ],
                workflow_stage="REVIEWING",
                expected_active_requirement_set_id=source_set["id"],
            )
            raise RuntimeError("injected failure after concurrent activation")

        with patch(
            "app.adapters.persistence.requirement_set_impacts."
            "apply_requirement_set_impact_receipt",
            side_effect=activate_then_fail,
        ), self.assertRaises(ApplicationError) as raised:
            derive_document_requirement_set(
                self.project["id"],
                self.document["id"],
                source_set["id"],
            )

        self.assertEqual(raised.exception.status_code, 409)
        target_id = captured["target_id"]
        target = db.get_requirement_set(target_id) or {}
        self.assertEqual(target["id"], target_id)
        self.assertTrue(bool(int(target["is_active"] or 0)))
        self.assertTrue(str(target["activated_at"] or ""))
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            target_id,
        )
        self.assertEqual(
            load_requirement_set_tree_snapshot(target).source_identity.requirement_set_id,
            target_id,
        )

    def test_all_retained_derivation_carries_document_ai_projection_and_kb_staleness(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="ai-projection-carry")
        self._seed_current_analysis_result(source_set["id"])
        assessed_at = "2026-08-08T08:00:00+00:00"
        knowledge_base_version = int(
            (db.get_project(self.project["id"]) or {})[
                "knowledge_base_version"
            ]
        )
        db.update_sor_document_ai_state(
            self.document["id"],
            ai_status="READY",
            ai_assessed_at=assessed_at,
            ai_requirement_set_version=int(source_set["version"]),
            ai_knowledge_base_version=knowledge_base_version,
            workflow_stage="AI_READY",
        )

        first_result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )
        first = first_result["requirement_set"]
        self.assertEqual(first_result["affected_result_count"], 0)
        first_projection = db.get_sor_document(self.document["id"]) or {}
        self.assertEqual(first_projection["ai_status"], "READY")
        self.assertEqual(first_projection["ai_assessed_at"], assessed_at)
        self.assertEqual(
            int(first_projection["ai_requirement_set_version"]),
            int(first["version"]),
        )
        self.assertEqual(
            int(first_projection["ai_knowledge_base_version"]),
            knowledge_base_version,
        )

        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=first["id"],
        )
        bumped = db.bump_project_knowledge_base_version(self.project["id"])
        self.assertEqual(
            int(bumped["knowledge_base_version"]),
            knowledge_base_version + 1,
        )
        self.assertEqual(
            (db.get_sor_document(self.document["id"]) or {})["ai_status"],
            "STALE",
        )

        second_result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            first["id"],
        )
        second = second_result["requirement_set"]
        self.assertEqual(second_result["affected_result_count"], 0)
        second_projection = db.get_sor_document(self.document["id"]) or {}
        self.assertEqual(second_projection["ai_status"], "STALE")
        self.assertEqual(second_projection["ai_assessed_at"], assessed_at)
        self.assertEqual(
            int(second_projection["ai_requirement_set_version"]),
            int(second["version"]),
        )
        self.assertEqual(
            int(second_projection["ai_knowledge_base_version"]),
            knowledge_base_version,
        )

    def test_result_publication_cannot_cross_staged_activation_linearization(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="publication-finalize-race")
        analysis_row = self._seed_current_analysis_result(
            source_set["id"],
            reason="original result",
        )
        from app.adapters.persistence import assessment_results
        from app.adapters.persistence import requirement_set_impacts

        release_publisher = Event()
        publisher_attempted = Event()
        publisher_done = Event()
        publication_errors: list[BaseException] = []
        original_fully_applied = (
            requirement_set_impacts._receipt_is_fully_applied
        )
        held_once = False

        def hold_finalizer(*args, **kwargs):
            nonlocal held_once
            fully_applied = original_fully_applied(*args, **kwargs)
            if fully_applied and not held_once:
                held_once = True
                release_publisher.set()
                self.assertTrue(publisher_attempted.wait(timeout=2))
                time.sleep(0.1)
                self.assertFalse(publisher_done.is_set())
            return fully_applied

        replacement = {
            "assessment": {
                "ai_decision": "NON_COMPLIANT",
                "compliance_status": "NON_COMPLIANT",
                "ai_reason": "late result",
                "ai_confidence": 0.88,
                "assessment_mode": "llm_primary",
                "analysis_label": "REQUIREMENT",
                "analysis_basis": "FULL",
                "normalized_requirement": "late result",
                "risk_level": "HIGH",
                "clarification_question": "",
                "model_name": "model-2",
                "prompt_version": "prompt-v2",
            },
            "evidences": [],
            "runs": [],
        }

        def publish_late_result() -> None:
            release_publisher.wait(timeout=2)
            publisher_attempted.set()
            try:
                assessment_results.publish_serving_results(
                    [(analysis_row["id"], replacement)],
                    document_id=self.document["id"],
                    requirement_set_id=source_set["id"],
                    requirement_set_version=int(source_set["version"]),
                    knowledge_base_version=int(
                        (db.get_project(self.project["id"]) or {})[
                            "knowledge_base_version"
                        ]
                    ),
                    profile_id="profile-2",
                    provider="provider-2",
                    retrieval_backend="local",
                    analysis_input_schema_version=(
                        REQUIREMENT_ANALYSIS_INPUT_SCHEMA_VERSION
                    ),
                    analysis_input_sha256_by_requirement_id={
                        analysis_row["id"]: (
                            requirement_analysis_input_sha256(analysis_row)
                        )
                    },
                )
            except BaseException as exc:  # Captured for assertion in the owner thread.
                publication_errors.append(exc)
            finally:
                publisher_done.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(publish_late_result)
            with patch.object(
                requirement_set_impacts,
                "_receipt_is_fully_applied",
                side_effect=hold_finalizer,
            ):
                result = derive_document_requirement_set(
                    self.project["id"],
                    self.document["id"],
                    source_set["id"],
                )
            future.result(timeout=5)

        self.assertTrue(publisher_done.is_set())
        self.assertEqual(len(publication_errors), 1)
        self.assertIsInstance(publication_errors[0], RuntimeError)
        self.assertIn("active LOCKED", str(publication_errors[0]))
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            result["requirement_set"]["id"],
        )
        self.assertEqual(
            (db.get_assessment(analysis_row["id"]) or {})["ai_reason"],
            "original result",
        )

    def test_actual_analysis_target_with_unproven_context_projects_stale(self) -> None:
        source_set, _ = self._publish_and_lock(suffix="ai-projection-stale")
        analysis_row = self._seed_current_analysis_result(source_set["id"])
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE assessments
                SET analysis_input_schema_version = '', analysis_input_sha256 = ''
                WHERE requirement_id = ?
                """,
                (analysis_row["id"],),
            )
        assessed_at = "2026-08-08T09:00:00+00:00"
        knowledge_base_version = int(
            (db.get_project(self.project["id"]) or {})[
                "knowledge_base_version"
            ]
        )
        db.update_sor_document_ai_state(
            self.document["id"],
            ai_status="READY",
            ai_assessed_at=assessed_at,
            ai_requirement_set_version=int(source_set["version"]),
            ai_knowledge_base_version=knowledge_base_version,
        )

        result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )

        self.assertEqual(result["affected_result_count"], 1)
        projection = db.get_sor_document(self.document["id"]) or {}
        self.assertEqual(projection["ai_status"], "STALE")
        self.assertEqual(projection["ai_assessed_at"], assessed_at)
        self.assertEqual(
            int(projection["ai_requirement_set_version"]),
            int(result["requirement_set"]["version"]),
        )
        self.assertEqual(
            int(projection["ai_knowledge_base_version"]),
            knowledge_base_version,
        )

    def test_non_analysis_no_prior_result_does_not_degrade_ready_projection(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="non-analysis-projection")
        assessed_at = "2026-08-08T10:00:00+00:00"
        knowledge_base_version = int(
            (db.get_project(self.project["id"]) or {})[
                "knowledge_base_version"
            ]
        )
        db.update_sor_document_ai_state(
            self.document["id"],
            ai_status="READY",
            ai_assessed_at=assessed_at,
            ai_requirement_set_version=int(source_set["version"]),
            ai_knowledge_base_version=knowledge_base_version,
        )

        result = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )

        self.assertEqual(result["affected_result_count"], 0)
        self.assertTrue(result["impact_receipt"]["node_results"])
        self.assertEqual(
            {
                node["outcome"]
                for node in result["impact_receipt"]["node_results"]
            },
            {"NO_PRIOR_RESULT"},
        )
        projection = db.get_sor_document(self.document["id"]) or {}
        self.assertEqual(projection["ai_status"], "READY")
        self.assertEqual(projection["ai_assessed_at"], assessed_at)
        self.assertEqual(
            int(projection["ai_requirement_set_version"]),
            int(result["requirement_set"]["version"]),
        )
        self.assertEqual(
            int(projection["ai_knowledge_base_version"]),
            knowledge_base_version,
        )

    def test_database_fork_rejects_same_lineage_active_source_switch(
        self,
    ) -> None:
        source_set, _ = self._publish_and_lock(suffix="active-source-switch")
        first = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )["requirement_set"]
        versions_before = db.list_requirement_sets(
            self.project["id"], self.document["id"]
        )

        with self.assertRaises(db.RequirementSetMutationBlocked):
            db.fork_requirement_set_for_edit_with_mapping(
                self.project["id"],
                self.document["id"],
                expected_source_requirement_set_id=source_set["id"],
                verified_source_publication_basis_key=(
                    source_set["publication_key"]
                ),
            )

        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]),
            versions_before,
        )
        self.assertEqual(
            (db.get_active_requirement_set(self.document["id"]) or {})["id"],
            first["id"],
        )

    def test_derived_w3_draft_keeps_verifiable_lineage_when_derived_again(
        self,
    ) -> None:
        source_set, accepted = self._publish_and_lock(suffix="derive-chain")
        first = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            source_set["id"],
        )["requirement_set"]
        db.lock_sor_document_extraction(
            self.document["id"],
            expected_requirement_set_id=first["id"],
        )

        second = derive_document_requirement_set(
            self.project["id"],
            self.document["id"],
            first["id"],
        )["requirement_set"]

        self.assertEqual(second["supersedes_id"], first["id"])
        self.assertEqual(
            second["entry_classification_snapshot_id"],
            accepted.snapshot_id,
        )
        self.assertEqual(
            len(
                {
                    source_set["publication_key"],
                    first["publication_key"],
                    second["publication_key"],
                }
            ),
            3,
        )
        loaded = load_requirement_set_tree_snapshot(second)
        self.assertEqual(
            loaded.source_identity.requirement_set_id,
            second["id"],
        )


if __name__ == "__main__":
    unittest.main()
