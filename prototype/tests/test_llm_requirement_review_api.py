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

from fastapi import Response

from app.application.errors import ApplicationError as HTTPException

from app import db
if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmReviewSourceSpan,
    stable_llm_requirement_prediction_id,
)
from app.services.llm_extraction_result_cache import LlmExtractionResultCache
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from tests.test_llm_requirement_review import _prediction


main.app = http_app


class LlmRequirementReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.root))
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        db.init_db()
        source = self.root / "source.pdf"
        source.write_bytes(b"synthetic-review-api-source")
        self.project = db.create_project(name="Review API", customer_name="Customer")
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
        self.prediction = _prediction()
        self.initial = initial_llm_requirement_review(self.prediction)
        self.delta = evaluate_llm_requirement_review(
            self.prediction,
            self.initial,
        )
        shadow = {
            "mode": "EDITABLE_LLM_REVIEW",
            "serving_impact": "NONE",
            "llm_review": {
                "model_prediction": self.prediction.model_dump(mode="json"),
                "reviewed_result": self.initial.model_dump(mode="json"),
                "delta": self.delta.model_dump(mode="json"),
            },
        }
        self.bound = main._bind_llm_requirement_review_session(
            project_id=self.project["id"],
            document_id=self.document["id"],
            shadow=shadow,
        )
        self.session_id = self.bound["llm_review"]["persistence"]["session_id"]
        profile = build_local_rules_profile(project_id=self.project["id"])
        self.processing_plan = build_resolved_processing_plan(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=0,
        )

    def _current_runtime_binding(self) -> dict[str, object]:
        return {
            "source_sha256": self.document["source_sha256"],
            "processing_plan": self.processing_plan.model_dump(mode="json"),
            "result_runtime_sha256": (
                main._document_extraction_result_runtime_sha256(
                    self.processing_plan
                )
            ),
            "visual_strategy": main.extraction_visual_strategy_identity(None),
        }

    def tearDown(self) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_preview_binding_persists_one_source_bound_model_answer(self) -> None:
        review = self.bound["llm_review"]

        self.assertEqual(review["persistence"]["status"], "PERSISTED")
        self.assertEqual(review["persistence"]["revision"], 0)
        self.assertEqual(
            review["schema_version"],
            "llm-requirement-review-session/v4",
        )
        self.assertEqual(
            review["model_prediction"]["prediction_id"],
            self.prediction.prediction_id,
        )
        self.assertEqual(
            review["model_prediction"]["schema_version"],
            "llm-requirement-prediction/v5",
        )
        self.assertEqual(
            review["reviewed_result"]["schema_version"],
            "llm-requirement-review/v4",
        )
        self.assertEqual(
            review["model_prediction"]["nodes"][0]["review_text"],
            self.prediction.nodes[0].review_text,
        )
        reference_candidate = review["reference_candidate"]
        self.assertEqual(
            reference_candidate["schema_version"],
            "llm-requirement-review-reference-candidate/v1",
        )
        self.assertEqual(reference_candidate["status"], "NOT_CAPTURED")
        self.assertTrue(reference_candidate["exact_partition_validated"])
        self.assertFalse(reference_candidate["independent_review_verified"])
        self.assertFalse(reference_candidate["human_gold"])
        self.assertFalse(reference_candidate["serving_activation_eligible"])
        self.assertIn(
            "human_review_not_completed",
            reference_candidate["reason_codes"],
        )
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]), []
        )

    def test_legacy_review_is_explicitly_incompatible_and_requires_regeneration(
        self,
    ) -> None:
        with sqlite3.connect(db.DB_PATH) as connection:
            row = connection.execute(
                """
                SELECT model_prediction_json
                FROM llm_requirement_review_sessions
                WHERE session_id = ?
                """,
                (self.session_id,),
            ).fetchone()
            payload = json.loads(str(row[0]))
            payload["schema_version"] = "llm-requirement-prediction/v4"
            serialized = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            connection.execute(
                """
                UPDATE llm_requirement_review_sessions
                SET model_prediction_json = ?, model_prediction_sha256 = ?
                WHERE session_id = ?
                """,
                (
                    serialized,
                    hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                    self.session_id,
                ),
            )
            connection.commit()

        with self.assertRaises(HTTPException) as caught:
            main.get_llm_requirement_review_session(
                self.project["id"],
                self.document["id"],
                self.session_id,
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_requirement_review_schema_incompatible",
        )
        self.assertEqual(
            caught.exception.detail["stored_schema_version"],
            "llm-requirement-prediction/v4",
        )
        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_review")
        self.assertEqual(
            status["job"]["error_code"],
            "llm_requirement_review_schema_incompatible",
        )

    def test_exact_span_split_advances_revision_without_serving_mutation(self) -> None:
        original = self.initial.nodes[2]
        semantic_unit_id = original.source_semantic_unit_ids[0]
        owner_structure_node_id = original.source_owner_structure_node_ids[0]
        text_length = len(self.prediction.nodes[2].review_text)
        split_at = 12
        first = original.model_copy(
            update={
                "review_node_id": "review-api-span-first",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=semantic_unit_id,
                        owner_structure_node_id=owner_structure_node_id,
                        start_offset=0,
                        end_offset=split_at,
                    ),
                ),
            }
        )
        second = original.model_copy(
            update={
                "review_node_id": "review-api-span-second",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=semantic_unit_id,
                        owner_structure_node_id=owner_structure_node_id,
                        start_offset=split_at,
                        end_offset=text_length,
                    ),
                ),
            }
        )
        payload = main.LlmRequirementReviewUpdateRequest(
            expected_revision=0,
            nodes=(*self.initial.nodes[:2], first, second),
        )

        result = main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            payload,
            Response(),
        )

        self.assertEqual(
            result["llm_review"]["delta"]["metrics"]["split_target_count"], 1
        )
        self.assertEqual(len(result["llm_review"]["reviewed_result"]["nodes"]), 4)
        self.assertFalse(result["requirement_set_created"])
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]), []
        )

        reloaded = main.get_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            Response(),
        )
        self.assertEqual(reloaded["llm_review"]["persistence"]["revision"], 1)
        self.assertEqual(
            len(reloaded["llm_review"]["reviewed_result"]["nodes"]),
            4,
        )
        self.assertEqual(
            sum(
                len(node["source_spans"])
                for node in reloaded["llm_review"]["reviewed_result"]["nodes"]
            ),
            4,
        )
        self.assertEqual(
            reloaded["llm_review"]["delta"]["metrics"]["split_target_count"],
            1,
        )

    def test_human_confirmation_advances_review_without_serving_mutation(self) -> None:
        payload = main.LlmRequirementReviewUpdateRequest(
            expected_revision=0,
            status="HUMAN_REVIEWED",
            reviewer_label="reviewer-a",
            nodes=self.initial.nodes,
        )

        result = main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            payload,
            Response(),
        )

        reviewed = result["llm_review"]["reviewed_result"]
        self.assertEqual(reviewed["revision"], 1)
        self.assertEqual(reviewed["status"], "HUMAN_REVIEWED")
        self.assertEqual(
            result["llm_review"]["delta"]["metrics"]["confirmed_target_rate"],
            1.0,
        )
        reference_candidate = result["llm_review"]["reference_candidate"]
        self.assertEqual(
            reference_candidate["status"],
            "SELF_ATTESTED_REVIEW",
        )
        self.assertEqual(reference_candidate["revision"], 1)
        self.assertTrue(reference_candidate["reviewer_label_present"])
        self.assertTrue(reference_candidate["exact_partition_validated"])
        self.assertFalse(reference_candidate["independent_review_verified"])
        self.assertFalse(reference_candidate["human_gold"])
        self.assertFalse(reference_candidate["serving_activation_eligible"])
        self.assertIn(
            "self_attestation_not_independent",
            reference_candidate["reason_codes"],
        )
        self.assertEqual(result["serving_impact"], "NONE")
        self.assertFalse(result["requirement_set_created"])
        self.assertFalse(result["requirement_set_activated"])
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]), []
        )

        reopened = main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementReviewUpdateRequest(
                expected_revision=1,
                status="DRAFT",
                reviewer_label="reviewer-a",
                nodes=self.initial.nodes,
            ),
            Response(),
        )

        self.assertEqual(
            reopened["llm_review"]["reviewed_result"]["status"],
            "DRAFT",
        )
        self.assertEqual(
            reopened["llm_review"]["reference_candidate"]["status"],
            "NOT_CAPTURED",
        )
        self.assertIsNone(
            reopened["llm_review"]["reference_candidate"]["recorded_at_utc"]
        )
        self.assertFalse(reopened["requirement_set_created"])
        self.assertFalse(reopened["requirement_set_activated"])

    def test_single_item_confirmation_persists_without_completing_review(self) -> None:
        confirmed_id = self.initial.nodes[1].review_node_id
        payload = main.LlmRequirementReviewUpdateRequest(
            expected_revision=0,
            status="DRAFT",
            confirmed_review_node_ids=(confirmed_id,),
            nodes=self.initial.nodes,
        )

        result = main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            payload,
            Response(),
        )

        reviewed = result["llm_review"]["reviewed_result"]
        self.assertEqual(reviewed["status"], "DRAFT")
        self.assertEqual(reviewed["confirmed_review_node_ids"], [confirmed_id])
        self.assertEqual(reviewed["revision"], 1)
        self.assertFalse(result["requirement_set_created"])

        reloaded = main.get_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            Response(),
        )
        self.assertEqual(
            reloaded["llm_review"]["reviewed_result"]["confirmed_review_node_ids"],
            [confirmed_id],
        )

    def test_compact_confirmation_does_not_require_review_nodes_from_client(
        self,
    ) -> None:
        confirmed_id = self.initial.nodes[0].review_node_id

        confirmed = main.update_llm_requirement_review_confirmation(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementConfirmationRequest(
                expected_revision=0,
                review_node_id=confirmed_id,
                confirmed=True,
            ),
            Response(),
        )
        undone = main.update_llm_requirement_review_confirmation(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementConfirmationRequest(
                expected_revision=1,
                review_node_id=confirmed_id,
                confirmed=False,
            ),
            Response(),
        )

        self.assertEqual(confirmed["confirmation"]["revision"], 1)
        self.assertEqual(confirmed["confirmation"]["review_node_id"], confirmed_id)
        self.assertTrue(confirmed["confirmation"]["confirmed"])
        self.assertTrue(confirmed["confirmation"]["changed"])
        self.assertEqual(undone["confirmation"]["revision"], 2)
        self.assertFalse(undone["confirmation"]["confirmed"])
        self.assertTrue(undone["confirmation"]["changed"])
        response_text = json.dumps(confirmed, ensure_ascii=False)
        self.assertLess(len(response_text.encode("utf-8")), 1_024)
        self.assertNotIn("llm_review", response_text)
        self.assertNotIn("model_prediction", response_text)
        self.assertNotIn("reviewed_result", response_text)
        self.assertNotIn("nodes", response_text)
        for node in self.prediction.nodes:
            self.assertNotIn(node.review_text, response_text)

        reloaded = main.get_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            Response(),
        )
        self.assertEqual(
            reloaded["llm_review"]["reviewed_result"]["confirmed_review_node_ids"],
            [],
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            full_event_count = connection.execute(
                "SELECT COUNT(*) FROM llm_requirement_review_events"
            ).fetchone()[0]
            compact_event_count = connection.execute(
                "SELECT COUNT(*) FROM llm_requirement_review_confirmation_events"
            ).fetchone()[0]
        self.assertEqual(full_event_count, 1)
        self.assertEqual(compact_event_count, 2)

    def test_compact_confirmation_noop_is_small_and_does_not_append_event(
        self,
    ) -> None:
        review_node_id = self.initial.nodes[0].review_node_id

        result = main.update_llm_requirement_review_confirmation(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementConfirmationRequest(
                expected_revision=0,
                review_node_id=review_node_id,
                confirmed=False,
            ),
            Response(),
        )

        self.assertFalse(result["confirmation"]["changed"])
        self.assertEqual(result["confirmation"]["revision"], 0)
        self.assertLess(
            len(json.dumps(result, ensure_ascii=False).encode("utf-8")),
            1_024,
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM llm_requirement_review_confirmation_events"
            ).fetchone()[0]
        self.assertEqual(event_count, 0)

    def test_compact_confirmation_rejects_a_stale_revision(self) -> None:
        review_node_id = self.initial.nodes[0].review_node_id
        main.update_llm_requirement_review_confirmation(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementConfirmationRequest(
                expected_revision=0,
                review_node_id=review_node_id,
                confirmed=True,
            ),
            Response(),
        )

        with self.assertRaises(HTTPException) as caught:
            main.update_llm_requirement_review_confirmation(
                self.project["id"],
                self.document["id"],
                self.session_id,
                main.LlmRequirementConfirmationRequest(
                    expected_revision=0,
                    review_node_id=review_node_id,
                    confirmed=False,
                ),
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_requirement_review_revision_conflict",
        )
        with sqlite3.connect(db.DB_PATH) as connection:
            event_count = connection.execute(
                "SELECT COUNT(*) FROM llm_requirement_review_confirmation_events"
            ).fetchone()[0]
        self.assertEqual(event_count, 1)

    def test_completed_status_overlays_latest_persisted_confirmation(self) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": "llm-extraction-" + "1" * 32,
                "project_id": self.project["id"],
                "document_id": self.document["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "COMPLETED",
                "stage": "completed",
                "result": {
                    **self.bound,
                    **self._current_runtime_binding(),
                    "requirement_tree_projection_runtime_version": (
                        main.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
                    ),
                    "structure_agent": {"editable": True},
                    "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
                    "pipeline": {
                        "source_ir": {
                            "adapter_id": main.PDF_SOURCE_IR_ADAPTER_ID,
                            "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                        },
                        "structure": {
                            "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                        },
                        "semantic": {
                            "planning_runtime_version": (
                                main.SEMANTIC_PLANNING_RUNTIME_VERSION
                            ),
                            "classifier_runtime_version": (
                                main.MODEL_SEMANTIC_CLASSIFIER_VERSION
                            ),
                            "assembler_runtime_version": (
                                main.MODEL_REQUIREMENT_ASSEMBLER_VERSION
                            ),
                            "document_runtime_version": (
                                main.LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
                            ),
                        },
                    },
                    "counts": {},
                },
            }
        confirmed_id = self.initial.nodes[0].review_node_id
        main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementReviewUpdateRequest(
                expected_revision=0,
                status="DRAFT",
                confirmed_review_node_ids=(confirmed_id,),
                nodes=self.initial.nodes,
            ),
            Response(),
        )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        reviewed = status["result"]["llm_review"]["reviewed_result"]
        self.assertEqual(reviewed["revision"], 1)
        self.assertEqual(reviewed["confirmed_review_node_ids"], [confirmed_id])

    def test_stale_in_memory_completed_result_is_rejected_as_incompatible(
        self,
    ) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": "llm-extraction-" + "9" * 32,
                "project_id": self.project["id"],
                "document_id": self.document["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "COMPLETED",
                "stage": "completed",
                "result": {
                    **self.bound,
                    "schema_version": "extraction-llm-shadow-preview/v3",
                    "legacy_result_marker": True,
                    "pipeline": {
                        "source_ir": {
                            "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                        },
                        "structure": {
                            "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                        },
                    },
                    "counts": {
                        "review_groups": 182,
                        "semantic_annotations": 177,
                    },
                },
            }

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_result")
        self.assertEqual(
            status["job"]["error_code"],
            "llm_document_extraction_result_schema_incompatible",
        )
        self.assertIsNone(status["result"])

    def test_active_document_extraction_blocks_review_mutations(self) -> None:
        for status in ("PENDING", "RUNNING", "CANCEL_REQUESTED"):
            with self.subTest(status=status):
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        "job_id": f"llm-extraction-{status.lower()}",
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                        "source_sha256": self.document["source_sha256"],
                        "status": status,
                        "stage": status.lower(),
                    }

                with self.assertRaises(HTTPException) as full_update:
                    main.update_llm_requirement_review_session(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        main.LlmRequirementReviewUpdateRequest(
                            expected_revision=0,
                            nodes=self.initial.nodes,
                        ),
                        Response(),
                    )
                with self.assertRaises(HTTPException) as compact_update:
                    main.update_llm_requirement_review_confirmation(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        main.LlmRequirementConfirmationRequest(
                            expected_revision=0,
                            review_node_id=self.initial.nodes[0].review_node_id,
                            confirmed=True,
                        ),
                        Response(),
                    )

                for caught in (full_update.exception, compact_update.exception):
                    self.assertEqual(caught.status_code, 409)
                    self.assertEqual(
                        caught.detail["code"],
                        "llm_document_extraction_in_progress",
                    )

    def test_terminal_retry_keeps_cached_review_browsable_but_blocks_mutations(
        self,
    ) -> None:
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload={
                "source_sha256": self.document["source_sha256"],
                "structure_agent": {"editable": True},
            },
        )

        for status in ("FAILED", "INTERRUPTED", "CANCELLED"):
            with self.subTest(status=status):
                with main._LLM_EXTRACTION_JOB_LOCK:
                    main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                        "job_id": f"llm-extraction-{status.lower()}",
                        "project_id": self.project["id"],
                        "document_id": self.document["id"],
                        "source_sha256": self.document["source_sha256"],
                        "status": status,
                        "stage": status.lower(),
                        "result": None,
                        "partial_result": None,
                    }

                browsed = main.get_llm_requirement_review_session(
                    self.project["id"],
                    self.document["id"],
                    self.session_id,
                    Response(),
                )
                with self.assertRaises(HTTPException) as full_update:
                    main.update_llm_requirement_review_session(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        main.LlmRequirementReviewUpdateRequest(
                            expected_revision=0,
                            nodes=self.initial.nodes,
                        ),
                        Response(),
                    )
                with self.assertRaises(HTTPException) as compact_update:
                    main.update_llm_requirement_review_confirmation(
                        self.project["id"],
                        self.document["id"],
                        self.session_id,
                        main.LlmRequirementConfirmationRequest(
                            expected_revision=0,
                            review_node_id=self.initial.nodes[0].review_node_id,
                            confirmed=True,
                        ),
                        Response(),
                    )

                self.assertEqual(
                    browsed["llm_review"]["reviewed_result"]["revision"],
                    0,
                )
                for caught in (full_update.exception, compact_update.exception):
                    self.assertEqual(caught.status_code, 409)
                    self.assertEqual(
                        caught.detail["code"],
                        "structure_result_read_only",
                    )
                    self.assertEqual(
                        caught.detail["read_only_reason"],
                        "structure_derivation_rerun_not_complete",
                    )

    def test_completed_result_overlays_only_its_exact_prediction(self) -> None:
        second_prediction = _rerun_prediction(self.prediction)
        second_initial = initial_llm_requirement_review(second_prediction)
        second_delta = evaluate_llm_requirement_review(
            second_prediction,
            second_initial,
        )
        second_bound = main._bind_llm_requirement_review_session(
            project_id=self.project["id"],
            document_id=self.document["id"],
            shadow={
                "llm_review": {
                    "model_prediction": second_prediction.model_dump(mode="json"),
                    "reviewed_result": second_initial.model_dump(mode="json"),
                    "delta": second_delta.model_dump(mode="json"),
                }
            },
        )
        first_confirmed_id = self.initial.nodes[0].review_node_id
        main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            main.LlmRequirementReviewUpdateRequest(
                expected_revision=0,
                confirmed_review_node_ids=(first_confirmed_id,),
                nodes=self.initial.nodes,
            ),
            Response(),
        )

        overlaid = main._with_matching_llm_requirement_review(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            result=second_bound,
        )

        self.assertEqual(
            overlaid["llm_review"]["model_prediction"]["prediction_id"],
            second_prediction.prediction_id,
        )
        self.assertEqual(overlaid["llm_review"]["persistence"]["revision"], 0)

    def test_completed_result_is_restored_from_source_bound_cache(self) -> None:
        cached_result = {
            **self.bound,
            **self._current_runtime_binding(),
            "requirement_tree_projection_runtime_version": (
                main.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "collection": "llm_requirement_document",
            "status": "READY",
            "pipeline": {
                "source_ir": {
                    "adapter_id": main.PDF_SOURCE_IR_ADAPTER_ID,
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
                "semantic": {
                    "planning_runtime_version": (
                        main.SEMANTIC_PLANNING_RUNTIME_VERSION
                    ),
                    "classifier_runtime_version": (
                        main.MODEL_SEMANTIC_CLASSIFIER_VERSION
                    ),
                    "assembler_runtime_version": (
                        main.MODEL_REQUIREMENT_ASSEMBLER_VERSION
                    ),
                    "document_runtime_version": (
                        main.LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
                    ),
                },
            },
            "counts": {
                "semantic_chunks": 2,
                "successful_provisions": 2,
                "semantic_annotations": 3,
            },
        }
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload=cached_result,
        )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["status"], "COMPLETED")
        self.assertEqual(status["job"]["progress"]["completed_chunks"], 2)
        self.assertEqual(status["result"]["status"], "READY")
        self.assertEqual(
            status["result"]["llm_review"]["persistence"]["session_id"],
            self.session_id,
        )

    def test_runtime_cache_gate_requires_all_semantic_runtime_identities(
        self,
    ) -> None:
        payload = {
            **self.bound,
            **self._current_runtime_binding(),
            "requirement_tree_projection_runtime_version": (
                main.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "pipeline": {
                "source_ir": {
                    "adapter_id": main.PDF_SOURCE_IR_ADAPTER_ID,
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
                "semantic": {
                    "planning_runtime_version": (
                        main.SEMANTIC_PLANNING_RUNTIME_VERSION
                    ),
                    "classifier_runtime_version": (
                        main.MODEL_SEMANTIC_CLASSIFIER_VERSION
                    ),
                    "assembler_runtime_version": (
                        main.MODEL_REQUIREMENT_ASSEMBLER_VERSION
                    ),
                    "document_runtime_version": (
                        main.LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
                    ),
                },
            },
        }

        self.assertTrue(main._llm_extraction_result_matches_runtime(payload))
        missing_runtime = json.loads(json.dumps(payload))
        missing_runtime.pop("result_runtime_sha256")
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(missing_runtime)
        )
        stale_runtime = json.loads(json.dumps(payload))
        stale_runtime["result_runtime_sha256"] = "0" * 64
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(stale_runtime)
        )
        stale_source_adapter = json.loads(json.dumps(payload))
        stale_source_adapter["pipeline"]["source_ir"]["version"] = "legacy-pdf"
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(stale_source_adapter)
        )
        for field in (
            "planning_runtime_version",
            "classifier_runtime_version",
            "assembler_runtime_version",
            "document_runtime_version",
        ):
            stale = json.loads(json.dumps(payload))
            stale["pipeline"]["semantic"][field] = "legacy-runtime"
            self.assertFalse(
                main._llm_extraction_result_matches_runtime(stale),
                msg=field,
            )
        missing = json.loads(json.dumps(payload))
        missing["pipeline"].pop("semantic")
        self.assertFalse(main._llm_extraction_result_matches_runtime(missing))
        missing_projection_runtime = json.loads(json.dumps(payload))
        missing_projection_runtime.pop(
            "requirement_tree_projection_runtime_version"
        )
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(
                missing_projection_runtime
            )
        )
        stale_projection_runtime = json.loads(json.dumps(payload))
        stale_projection_runtime[
            "requirement_tree_projection_runtime_version"
        ] = "requirement-tree-projection/v10"
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(stale_projection_runtime)
        )
        review_session_only = json.loads(json.dumps(payload))
        review_session_only["recovery"] = {"mode": "REVIEW_SESSION_ONLY"}
        self.assertTrue(
            main._llm_extraction_result_matches_runtime(review_session_only)
        )
        review_session_only["pipeline"].pop("semantic")
        self.assertFalse(
            main._llm_extraction_result_matches_runtime(review_session_only)
        )

    def test_stale_resolver_cache_is_rejected_as_incompatible(self) -> None:
        cached_result = {
            **self.bound,
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "collection": "llm_requirement_document",
            "status": "READY",
            "pipeline": {
                "source_ir": {
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": "stale-resolver-version",
                },
            },
            "counts": {
                "semantic_chunks": 2,
                "successful_provisions": 2,
                "semantic_annotations": 3,
            },
        }
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload=cached_result,
        )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_result")
        self.assertEqual(
            status["job"]["error_code"],
            "llm_document_extraction_result_schema_incompatible",
        )
        self.assertIsNone(status["result"])

    def test_legacy_shadow_cache_is_rejected_as_incompatible(
        self,
    ) -> None:
        cached_result = {
            **self.bound,
            "schema_version": "extraction-llm-shadow-preview/v4",
            "legacy_cache_marker": True,
            "pipeline": {
                "source_ir": {
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
            },
            "counts": {
                "semantic_chunks": 2,
                "successful_provisions": 2,
                "semantic_annotations": 3,
            },
        }
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload=cached_result,
        )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_result")
        self.assertEqual(
            status["job"]["error_code"],
            "llm_document_extraction_result_schema_incompatible",
        )
        self.assertIsNone(status["result"])

    def test_session_only_restore_without_runtime_identity_is_incompatible(
        self,
    ) -> None:
        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["status"], "FAILED")
        self.assertEqual(status["job"]["stage"], "incompatible_result")
        self.assertEqual(
            status["job"]["error_code"],
            "llm_document_extraction_result_schema_incompatible",
        )
        self.assertIsNone(status["result"])

    def test_stale_revision_is_a_conflict(self) -> None:
        payload = main.LlmRequirementReviewUpdateRequest(
            expected_revision=0,
            nodes=self.initial.nodes,
        )
        main.update_llm_requirement_review_session(
            self.project["id"],
            self.document["id"],
            self.session_id,
            payload,
            Response(),
        )

        with self.assertRaises(HTTPException) as caught:
            main.update_llm_requirement_review_session(
                self.project["id"],
                self.document["id"],
                self.session_id,
                payload,
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_requirement_review_revision_conflict",
        )

    def test_human_confirmation_requires_reviewer_label(self) -> None:
        payload = main.LlmRequirementReviewUpdateRequest(
            expected_revision=0,
            status="HUMAN_REVIEWED",
            nodes=self.initial.nodes,
        )

        with self.assertRaises(HTTPException) as caught:
            main.update_llm_requirement_review_session(
                self.project["id"],
                self.document["id"],
                self.session_id,
                payload,
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_requirement_review_invalid",
        )

    def test_session_is_scoped_to_its_project_and_document(self) -> None:
        other = db.create_project(name="Other", customer_name="Other")

        with self.assertRaises(HTTPException) as caught:
            main.get_llm_requirement_review_session(
                other["id"],
                self.document["id"],
                self.session_id,
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 404)

    def test_delete_document_purges_review_text_and_cached_result(self) -> None:
        LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload={**self.bound, "counts": {}},
        )

        with patch.object(
            main.VisualTextSnapshotCache,
            "purge_document",
            return_value=False,
        ) as purge_visual_snapshots:
            result = main.delete_sor_document(
                self.project["id"],
                self.document["id"],
            )

        self.assertTrue(result["ok"])
        purge_visual_snapshots.assert_called_once_with(
            project_id=self.project["id"],
            document_id=self.document["id"],
        )
        self.assertIsNone(db.get_sor_document(self.document["id"]))
        self.assertIsNone(
            main._llm_requirement_review_repository().get(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session_id,
            )
        )
        self.assertIsNone(
            LlmExtractionResultCache(self.root).load(
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.document["source_sha256"],
            )
        )

    def test_visual_snapshot_purge_failure_preserves_document_binding(self) -> None:
        with patch.object(
            main.VisualTextSnapshotCache,
            "purge_document",
            side_effect=OSError("visual cache unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "visual cache unavailable"):
                main.delete_sor_document(
                    self.project["id"],
                    self.document["id"],
                )

        self.assertIsNotNone(db.get_sor_document(self.document["id"]))
        self.assertIsNotNone(
            main._llm_requirement_review_repository().get(
                project_id=self.project["id"],
                document_id=self.document["id"],
                session_id=self.session_id,
            )
        )


def _rerun_prediction(
    prediction: LlmRequirementModelPrediction,
) -> LlmRequirementModelPrediction:
    provider_run_ids = (*prediction.provider_run_ids, "provider-run-rerun")
    kwargs = {
        "source_ir_id": prediction.source_ir_id,
        "source_sha256": prediction.source_sha256,
        "structure_snapshot_id": prediction.structure_snapshot_id,
        "scope_locator_sha256": prediction.scope_locator_sha256,
        "provider_id": prediction.provider_id,
        "provider_version": prediction.provider_version,
        "model_id": prediction.model_id,
        "classification_prompt_version": prediction.classification_prompt_version,
        "assembly_prompt_version": prediction.assembly_prompt_version,
        "semantic_unit_plan_ids": prediction.semantic_unit_plan_ids,
        "semantic_unit_ids": prediction.semantic_unit_ids,
        "provider_run_ids": provider_run_ids,
        "nodes": prediction.nodes,
    }
    return LlmRequirementModelPrediction(
        prediction_id=stable_llm_requirement_prediction_id(**kwargs),
        **kwargs,
    )


if __name__ == "__main__":
    unittest.main()
