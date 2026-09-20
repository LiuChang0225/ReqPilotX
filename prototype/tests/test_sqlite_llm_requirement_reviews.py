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
import unittest

from app.adapters.persistence.sqlite_llm_requirement_reviews import (
    SQLiteLlmRequirementReviewRepository,
)
from app.application.extraction.llm_requirement_review import (
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementReviewStatus,
)
from app.ports.llm_requirement_review_repository import (
    LlmRequirementReviewConflictError,
    LlmRequirementReviewIncompatibleError,
    LlmRequirementReviewRepositoryError,
)
from tests.test_llm_requirement_review import _prediction


class SQLiteLlmRequirementReviewRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.repository = SQLiteLlmRequirementReviewRepository(self.connection)
        self.prediction = _prediction()
        self.initial = initial_llm_requirement_review(self.prediction)
        self.initial_delta = evaluate_llm_requirement_review(
            self.prediction,
            self.initial,
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_create_or_get_is_idempotent_for_one_prediction(self) -> None:
        first = self._create()
        second = self._create()

        self.assertEqual(first.session_id, second.session_id)
        self.assertEqual(first.prediction, self.prediction)
        self.assertEqual(second.reviewed_result, self.initial)
        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_events"
        ).fetchone()[0]
        self.assertEqual(event_count, 1)

    def test_update_advances_one_revision_and_appends_event(self) -> None:
        session = self._create()
        reviewed = self.initial.model_copy(
            update={
                "revision": 1,
                "status": LlmRequirementReviewStatus.HUMAN_REVIEWED,
                "reviewer_label": "reviewer-a",
            }
        )
        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        updated = self.repository.update(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=0,
            reviewed_result=reviewed,
            delta=delta,
        )

        self.assertEqual(updated.reviewed_result.revision, 1)
        self.assertEqual(
            updated.reviewed_result.status,
            LlmRequirementReviewStatus.HUMAN_REVIEWED,
        )
        loaded = self.repository.get(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
        )
        self.assertEqual(loaded, updated)
        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_events"
        ).fetchone()[0]
        self.assertEqual(event_count, 2)

    def test_stale_update_is_rejected_without_extra_event(self) -> None:
        session = self._create()
        reviewed = self.initial.model_copy(update={"revision": 1})
        delta = evaluate_llm_requirement_review(self.prediction, reviewed)
        self.repository.update(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=0,
            reviewed_result=reviewed,
            delta=delta,
        )

        with self.assertRaisesRegex(
            LlmRequirementReviewConflictError,
            "revision has changed",
        ):
            self.repository.update(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
                expected_revision=0,
                reviewed_result=reviewed,
                delta=delta,
            )

        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_events"
        ).fetchone()[0]
        self.assertEqual(event_count, 2)

    def test_confirmation_update_appends_only_compact_hash_chained_events(
        self,
    ) -> None:
        session = self._create()
        review_node_id = self.initial.nodes[0].review_node_id
        confirmed = self.initial.model_copy(
            update={
                "revision": 1,
                "confirmed_review_node_ids": (review_node_id,),
            }
        )
        confirmed_delta = evaluate_llm_requirement_review(
            self.prediction,
            confirmed,
        )

        first = self.repository.update_confirmation(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=0,
            review_node_id=review_node_id,
            confirmed=True,
            reviewed_result=confirmed,
            delta=confirmed_delta,
        )
        undone = confirmed.model_copy(
            update={
                "revision": 2,
                "confirmed_review_node_ids": (),
            }
        )
        undone_delta = evaluate_llm_requirement_review(
            self.prediction,
            undone,
        )
        second = self.repository.update_confirmation(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=1,
            review_node_id=review_node_id,
            confirmed=False,
            reviewed_result=undone,
            delta=undone_delta,
        )

        self.assertEqual(
            first.reviewed_result.confirmed_review_node_ids, (review_node_id,)
        )
        self.assertEqual(second.reviewed_result.confirmed_review_node_ids, ())
        full_event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_events"
        ).fetchone()[0]
        self.assertEqual(full_event_count, 1)
        compact_events = self.connection.execute(
            """
            SELECT * FROM llm_requirement_review_confirmation_events
            WHERE session_id = ?
            ORDER BY revision
            """,
            (session.session_id,),
        ).fetchall()
        self.assertEqual(len(compact_events), 2)
        self.assertEqual(compact_events[0]["previous_event_sha256"], "0" * 64)
        self.assertEqual(
            compact_events[1]["previous_event_sha256"],
            compact_events[0]["event_sha256"],
        )
        self.assertEqual(
            compact_events[1]["previous_reviewed_result_sha256"],
            compact_events[0]["reviewed_result_sha256"],
        )
        self.assertEqual(
            tuple(compact_events[0].keys()),
            (
                "session_id",
                "revision",
                "review_node_id",
                "confirmed",
                "status",
                "previous_reviewed_result_sha256",
                "reviewed_result_sha256",
                "previous_event_sha256",
                "event_sha256",
                "created_at_utc",
            ),
        )
        compact_text = "\n".join(
            str(value) for event in compact_events for value in tuple(event)
        )
        for event in compact_events:
            self.assertLess(
                len(json.dumps(dict(event), ensure_ascii=False).encode("utf-8")),
                1_024,
            )
        for node in self.prediction.nodes:
            self.assertNotIn(node.review_text, compact_text)
        self.assertNotIn("nodes", compact_text)
        self.assertNotIn("delta", compact_text)
        self.assertLess(len(compact_text.encode("utf-8")), 2_000)

    def test_confirmation_update_rejects_any_non_confirmation_mutation(
        self,
    ) -> None:
        session = self._create()
        review_node_id = self.initial.nodes[0].review_node_id
        changed_node = self.initial.nodes[0].model_copy(
            update={"reason_codes": ("reviewer_changed",)}
        )
        reviewed = self.initial.model_copy(
            update={
                "revision": 1,
                "nodes": (changed_node, *self.initial.nodes[1:]),
                "confirmed_review_node_ids": (review_node_id,),
            }
        )
        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        with self.assertRaisesRegex(
            ValueError,
            "cannot change reviewed nodes",
        ):
            self.repository.update_confirmation(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
                expected_revision=0,
                review_node_id=review_node_id,
                confirmed=True,
                reviewed_result=reviewed,
                delta=delta,
            )

        extra_review_node_id = self.initial.nodes[1].review_node_id
        multiple_confirmations = self.initial.model_copy(
            update={
                "revision": 1,
                "confirmed_review_node_ids": (
                    review_node_id,
                    extra_review_node_id,
                ),
            }
        )
        with self.assertRaisesRegex(
            ValueError,
            "only the requested node",
        ):
            self.repository.update_confirmation(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
                expected_revision=0,
                review_node_id=review_node_id,
                confirmed=True,
                reviewed_result=multiple_confirmations,
                delta=evaluate_llm_requirement_review(
                    self.prediction,
                    multiple_confirmations,
                ),
            )

        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM llm_requirement_review_confirmation_events"
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            self.repository.get(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
            ),
            session,
        )

    def test_confirmation_update_rejects_a_tampered_event_chain(self) -> None:
        session = self._create()
        review_node_id = self.initial.nodes[0].review_node_id
        confirmed = self.initial.model_copy(
            update={
                "revision": 1,
                "confirmed_review_node_ids": (review_node_id,),
            }
        )
        self.repository.update_confirmation(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=0,
            review_node_id=review_node_id,
            confirmed=True,
            reviewed_result=confirmed,
            delta=evaluate_llm_requirement_review(self.prediction, confirmed),
        )
        self.connection.execute(
            """
            UPDATE llm_requirement_review_confirmation_events
            SET event_sha256 = ?
            WHERE session_id = ? AND revision = 1
            """,
            ("f" * 64, session.session_id),
        )
        self.connection.commit()
        undone = confirmed.model_copy(
            update={
                "revision": 2,
                "confirmed_review_node_ids": (),
            }
        )

        with self.assertRaisesRegex(
            LlmRequirementReviewRepositoryError,
            "confirmation event hash mismatch",
        ):
            self.repository.update_confirmation(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
                expected_revision=1,
                review_node_id=review_node_id,
                confirmed=False,
                reviewed_result=undone,
                delta=evaluate_llm_requirement_review(self.prediction, undone),
            )

        loaded = self.repository.get(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
        )
        self.assertEqual(loaded.reviewed_result.revision, 1)

    def test_get_latest_is_bound_to_document_and_source_identity(self) -> None:
        created = self._create()
        latest = self.repository.get_latest(
            project_id="project-a",
            document_id="document-a",
            source_sha256=self.prediction.source_sha256,
        )
        stale_source = self.repository.get_latest(
            project_id="project-a",
            document_id="document-a",
            source_sha256="f" * 64,
        )

        self.assertEqual(latest, created)
        self.assertIsNone(stale_source)

    def test_get_for_prediction_requires_the_exact_model_answer(self) -> None:
        created = self._create()

        matching = self.repository.get_for_prediction(
            project_id="project-a",
            document_id="document-a",
            source_sha256=self.prediction.source_sha256,
            prediction_id=self.prediction.prediction_id,
        )
        missing = self.repository.get_for_prediction(
            project_id="project-a",
            document_id="document-a",
            source_sha256=self.prediction.source_sha256,
            prediction_id="llm-prediction-" + "f" * 32,
        )

        self.assertEqual(matching, created)
        self.assertIsNone(missing)

    def test_purge_document_removes_sessions_and_append_only_events(self) -> None:
        session = self._create()
        review_node_id = self.initial.nodes[0].review_node_id
        reviewed = self.initial.model_copy(
            update={
                "revision": 1,
                "confirmed_review_node_ids": (review_node_id,),
            }
        )
        self.repository.update_confirmation(
            project_id="project-a",
            document_id="document-a",
            session_id=session.session_id,
            expected_revision=0,
            review_node_id=review_node_id,
            confirmed=True,
            reviewed_result=reviewed,
            delta=evaluate_llm_requirement_review(self.prediction, reviewed),
        )

        purged = self.repository.purge_document(
            project_id="project-a",
            document_id="document-a",
        )

        self.assertEqual(purged, 1)
        self.assertIsNone(
            self.repository.get(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
            )
        )
        event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_events"
        ).fetchone()[0]
        self.assertEqual(event_count, 0)
        confirmation_event_count = self.connection.execute(
            "SELECT COUNT(*) FROM llm_requirement_review_confirmation_events"
        ).fetchone()[0]
        self.assertEqual(confirmation_event_count, 0)

    def test_tampered_stored_payload_is_detected(self) -> None:
        session = self._create()
        self.connection.execute(
            """
            UPDATE llm_requirement_review_sessions
            SET reviewed_result_json = '{}'
            WHERE session_id = ?
            """,
            (session.session_id,),
        )
        self.connection.commit()

        with self.assertRaisesRegex(
            LlmRequirementReviewRepositoryError,
            "payload hash mismatch",
        ):
            self.repository.get(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
            )

    def test_legacy_prediction_schema_is_reported_as_incompatible(self) -> None:
        session = self._create()
        row = self.connection.execute(
            """
            SELECT model_prediction_json
            FROM llm_requirement_review_sessions
            WHERE session_id = ?
            """,
            (session.session_id,),
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload["schema_version"] = "llm-requirement-prediction/v4"
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.connection.execute(
            """
            UPDATE llm_requirement_review_sessions
            SET model_prediction_json = ?, model_prediction_sha256 = ?
            WHERE session_id = ?
            """,
            (
                serialized,
                hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                session.session_id,
            ),
        )
        self.connection.commit()

        with self.assertRaises(LlmRequirementReviewIncompatibleError) as caught:
            self.repository.get(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
            )

        self.assertEqual(
            caught.exception.stored_schema_version,
            "llm-requirement-prediction/v4",
        )
        self.assertEqual(
            caught.exception.current_schema_version,
            "llm-requirement-prediction/v5",
        )

    def test_legacy_review_schema_is_reported_as_incompatible(self) -> None:
        session = self._create()
        row = self.connection.execute(
            """
            SELECT reviewed_result_json
            FROM llm_requirement_review_sessions
            WHERE session_id = ?
            """,
            (session.session_id,),
        ).fetchone()
        payload = json.loads(str(row[0]))
        payload["schema_version"] = "llm-requirement-review/v3"
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        self.connection.execute(
            """
            UPDATE llm_requirement_review_sessions
            SET reviewed_result_json = ?, reviewed_result_sha256 = ?
            WHERE session_id = ?
            """,
            (
                serialized,
                hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                session.session_id,
            ),
        )
        self.connection.commit()

        with self.assertRaises(LlmRequirementReviewIncompatibleError) as caught:
            self.repository.get(
                project_id="project-a",
                document_id="document-a",
                session_id=session.session_id,
            )

        self.assertEqual(
            caught.exception.stored_schema_version,
            "llm-requirement-review/v3",
        )
        self.assertEqual(
            caught.exception.current_schema_version,
            "llm-requirement-review/v4",
        )

    def _create(self):
        return self.repository.create_or_get(
            project_id="project-a",
            document_id="document-a",
            prediction=self.prediction,
            reviewed_result=self.initial,
            delta=self.initial_delta,
        )


if __name__ == "__main__":
    unittest.main()
