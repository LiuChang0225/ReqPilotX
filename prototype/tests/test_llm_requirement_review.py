from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from app.application.extraction.llm_requirement_review import (
    _retain_contiguous_review_ownership,
    build_llm_requirement_model_prediction,
    build_llm_requirement_review_reference_candidate,
    evaluate_llm_requirement_review,
    initial_llm_requirement_review,
    validate_llm_requirement_review,
)
from app.application.extraction.llm_provision_batch import (
    extract_requirement_provisions_with_provider,
)
from app.application.extraction.locate_llm_scope import (
    locate_llm_extraction_scope,
)
from app.domain.extraction.llm_requirement_review import (
    LlmRequirementModelPrediction,
    LlmRequirementPredictionNode,
    LlmRequirementReviewOperationType,
    LlmRequirementReviewSession,
    LlmRequirementReviewStatus,
    LlmRequirementReviewedResult,
    LlmReviewSourceSpan,
    source_binding_sha256,
    stable_llm_requirement_prediction_id,
)
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionRole,
)
from app.domain.extraction.semantics import RequirementSubtype, SemanticClass
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from tests.test_llm_provision_batch import (
    _ProvisionProvider,
    _three_provision_input,
)


class LlmRequirementReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.prediction = _prediction()

    def test_initial_review_is_the_single_model_answer_without_edits(self) -> None:
        reviewed = initial_llm_requirement_review(self.prediction)

        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        self.assertEqual(reviewed.status, LlmRequirementReviewStatus.DRAFT)
        self.assertEqual(reviewed.revision, 0)
        self.assertEqual(len(reviewed.nodes), 3)
        self.assertEqual(delta.metrics.confirmed_target_count, 3)
        self.assertEqual(delta.metrics.confirmed_target_rate, 1.0)
        self.assertEqual(delta.metrics.semantic_class_agreement, 1.0)
        self.assertEqual(delta.metrics.requirement_subtype_compared_target_count, 2)
        self.assertEqual(delta.metrics.projection_compared_target_count, 3)
        self.assertEqual(
            {item.operation for item in delta.operations},
            {LlmRequirementReviewOperationType.CONFIRMED},
        )
        self.assertEqual(reviewed.confirmed_review_node_ids, ())

    def test_reference_candidate_is_text_free_and_never_self_promotes_to_gold(
        self,
    ) -> None:
        reviewed = initial_llm_requirement_review(self.prediction)
        delta = evaluate_llm_requirement_review(self.prediction, reviewed)
        now = datetime.now(timezone.utc)
        draft_session = LlmRequirementReviewSession(
            session_id="llm-review-" + "a" * 32,
            project_id="project-a",
            document_id="document-a",
            prediction=self.prediction,
            reviewed_result=reviewed,
            delta=delta,
            created_at_utc=now,
            updated_at_utc=now,
        )

        draft_candidate = build_llm_requirement_review_reference_candidate(
            draft_session
        )

        self.assertEqual(draft_candidate.status.value, "NOT_CAPTURED")
        self.assertIn("human_review_not_completed", draft_candidate.reason_codes)
        self.assertTrue(draft_candidate.exact_partition_validated)
        self.assertFalse(draft_candidate.independent_review_verified)
        self.assertFalse(draft_candidate.human_gold)
        self.assertFalse(draft_candidate.serving_activation_eligible)
        self.assertIsNone(draft_candidate.recorded_at_utc)

        human_reviewed = reviewed.model_copy(
            update={
                "revision": 1,
                "status": LlmRequirementReviewStatus.HUMAN_REVIEWED,
                "reviewer_label": "reviewer-a",
            }
        )
        human_delta = evaluate_llm_requirement_review(
            self.prediction,
            human_reviewed,
        )
        human_session = draft_session.model_copy(
            update={
                "reviewed_result": human_reviewed,
                "delta": human_delta,
                "updated_at_utc": now,
            }
        )

        captured = build_llm_requirement_review_reference_candidate(human_session)

        self.assertEqual(captured.status.value, "SELF_ATTESTED_REVIEW")
        self.assertTrue(captured.reviewer_label_present)
        self.assertEqual(captured.recorded_at_utc, now)
        self.assertIn("self_attestation_not_independent", captured.reason_codes)
        self.assertFalse(captured.independent_review_verified)
        self.assertFalse(captured.human_gold)
        self.assertFalse(captured.serving_activation_eligible)
        self.assertNotIn("reviewer-a", captured.model_dump_json())
        self.assertNotIn(
            self.prediction.nodes[0].review_text, captured.model_dump_json()
        )
        self.assertEqual(
            captured,
            build_llm_requirement_review_reference_candidate(human_session),
        )

    def test_previous_review_session_schema_requires_regeneration(self) -> None:
        reviewed = initial_llm_requirement_review(self.prediction)
        delta = evaluate_llm_requirement_review(self.prediction, reviewed)
        now = datetime.now(timezone.utc)
        session = LlmRequirementReviewSession(
            session_id="llm-review-" + "a" * 32,
            project_id="project-a",
            document_id="document-a",
            prediction=self.prediction,
            reviewed_result=reviewed,
            delta=delta,
            created_at_utc=now,
            updated_at_utc=now,
        )
        payload = session.model_dump(mode="json")
        payload["schema_version"] = "llm-requirement-review-session/v3"

        with self.assertRaises(ValidationError):
            LlmRequirementReviewSession.model_validate(payload)

    def test_model_continuation_is_one_full_review_unit_without_duplicate_owner(
        self,
    ) -> None:
        prediction = _prediction_with_owned_continuation()

        reviewed = initial_llm_requirement_review(prediction)
        delta = evaluate_llm_requirement_review(prediction, reviewed)

        self.assertEqual(len(prediction.nodes), 2)
        self.assertEqual(len(reviewed.nodes), 1)
        self.assertEqual(
            reviewed.nodes[0].source_semantic_unit_ids,
            tuple(item.semantic_unit_id for item in prediction.nodes),
        )
        self.assertEqual(len(reviewed.nodes[0].source_spans), 2)
        self.assertEqual(delta.metrics.target_count, 1)
        self.assertEqual(delta.metrics.confirmed_target_count, 1)
        self.assertEqual(delta.metrics.merged_review_node_count, 0)
        self.assertNotIn(
            LlmRequirementReviewOperationType.MERGED,
            {item.operation for item in delta.operations},
        )

    def test_projected_continuation_annotations_build_one_review_owner(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        batch = extract_requirement_provisions_with_provider(
            source,
            tree,
            _ProvisionProvider(mark_content_continuation=True),
            locator,
        )

        prediction = build_llm_requirement_model_prediction(
            source,
            tree,
            located_scope,
            batch,
        )
        reviewed = initial_llm_requirement_review(prediction)

        owned = tuple(
            node
            for node in prediction.nodes
            if node.review_owner_semantic_unit_id is not None
        )
        self.assertTrue(owned)
        for continuation in owned:
            owner_id = continuation.review_owner_semantic_unit_id
            owner = next(
                node for node in prediction.nodes if node.semantic_unit_id == owner_id
            )
            self.assertTrue(owner.include_in_view)
            self.assertFalse(continuation.include_in_view)
            review_node = next(
                node for node in reviewed.nodes if node.review_node_id == owner_id
            )
            self.assertIn(
                continuation.semantic_unit_id,
                review_node.source_semantic_unit_ids,
            )
            self.assertEqual(
                sum(
                    span.semantic_unit_id == continuation.semantic_unit_id
                    for span in review_node.source_spans
                ),
                1,
            )

    def test_interleaved_model_ownership_is_pruned_to_safe_review_units(
        self,
    ) -> None:
        source, tree, locator = _three_provision_input()
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        ordered_targets = sorted(
            located_scope.scope_structure_node_ids[:3],
            key=lambda target_id: (
                nodes_by_id[target_id].source_order,
                target_id,
            ),
        )
        owner_id, peer_id, continuation_id = ordered_targets
        ownership = {
            owner_id: owner_id,
            peer_id: peer_id,
            continuation_id: owner_id,
        }

        normalized = _retain_contiguous_review_ownership(
            ownership,
            nodes_by_id=nodes_by_id,
        )

        self.assertEqual(
            normalized,
            {
                owner_id: owner_id,
                peer_id: peer_id,
                continuation_id: continuation_id,
            },
        )

    def test_contiguous_model_ownership_is_preserved(self) -> None:
        source, tree, locator = _three_provision_input()
        located_scope = locate_llm_extraction_scope(source, tree, locator)
        nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
        ordered_targets = sorted(
            located_scope.scope_structure_node_ids[:3],
            key=lambda target_id: (
                nodes_by_id[target_id].source_order,
                target_id,
            ),
        )
        owner_id, continuation_id, peer_id = ordered_targets
        ownership = {
            owner_id: owner_id,
            continuation_id: owner_id,
            peer_id: peer_id,
        }

        normalized = _retain_contiguous_review_ownership(
            ownership,
            nodes_by_id=nodes_by_id,
        )

        self.assertEqual(normalized, ownership)

    def test_prediction_rejects_interleaved_review_owners(self) -> None:
        base = _prediction_with_owned_continuation_and_peer()
        owner, continuation, peer = base.nodes
        nodes = (
            owner,
            peer.model_copy(update={"source_order": 1, "unit_order": 1}),
            continuation.model_copy(update={"source_order": 2, "unit_order": 2}),
        )
        kwargs = {
            **base.model_dump(
                exclude={"schema_version", "prediction_id", "nodes"},
                mode="python",
            ),
            "nodes": nodes,
            "semantic_unit_ids": tuple(node.semantic_unit_id for node in nodes),
        }

        with self.assertRaisesRegex(
            ValueError,
            "prediction review owners cannot interleave",
        ):
            LlmRequirementModelPrediction(
                prediction_id=stable_llm_requirement_prediction_id(**kwargs),
                **kwargs,
            )

    def test_model_continuation_split_at_source_boundary_counts_as_one_split(
        self,
    ) -> None:
        prediction = _prediction_with_owned_continuation()
        initial = initial_llm_requirement_review(prediction)
        original = initial.nodes[0]
        owner_span, continuation_span = original.source_spans
        owner = original.model_copy(
            update={
                "review_node_id": "review-owner-only",
                "source_semantic_unit_ids": (owner_span.semantic_unit_id,),
                "source_owner_structure_node_ids": (
                    owner_span.owner_structure_node_id,
                ),
                "source_spans": (owner_span,),
            }
        )
        continuation = original.model_copy(
            update={
                "review_node_id": "review-continuation-only",
                "source_semantic_unit_ids": (continuation_span.semantic_unit_id,),
                "source_owner_structure_node_ids": (
                    continuation_span.owner_structure_node_id,
                ),
                "source_spans": (continuation_span,),
                "source_order": prediction.nodes[1].source_order,
            }
        )
        reviewed = initial.model_copy(
            update={"revision": 1, "nodes": (owner, continuation)}
        )

        delta = evaluate_llm_requirement_review(prediction, reviewed)

        self.assertEqual(delta.metrics.split_target_count, 1)
        self.assertEqual(delta.metrics.edited_target_count, 1)
        split = next(
            item
            for item in delta.operations
            if item.operation is LlmRequirementReviewOperationType.SPLIT
        )
        self.assertEqual(
            split.source_semantic_unit_ids,
            tuple(item.semantic_unit_id for item in prediction.nodes),
        )
        self.assertEqual(split.after, 2)

    def test_ai_review_can_atomize_long_multi_source_obligations_without_gold(
        self,
    ) -> None:
        prediction = _prediction_with_long_owned_continuation()
        initial = initial_llm_requirement_review(prediction)
        self.assertEqual(len(initial.nodes), 1)

        prediction_by_unit = {
            item.semantic_unit_id: item for item in prediction.nodes
        }
        unit_ids = tuple(item.semantic_unit_id for item in prediction.nodes)
        texts = tuple(item.review_text for item in prediction.nodes)
        first_period = texts[1].index(".") + 1
        second_period = texts[2].index(".") + 1
        third_period = texts[3].index(".") + 1
        fourth_period = texts[3].index(".", third_period) + 1
        label_end = len("4.2.1 ")

        def span(unit_index: int, start: int, end: int) -> LlmReviewSourceSpan:
            predicted = prediction.nodes[unit_index]
            return LlmReviewSourceSpan(
                semantic_unit_id=predicted.semantic_unit_id,
                owner_structure_node_id=predicted.owner_structure_node_id,
                start_offset=start,
                end_offset=end,
            )

        partitions = (
            (span(0, 0, label_end),),
            (
                span(0, label_end, len(texts[0])),
                span(1, 0, first_period),
            ),
            (
                span(1, first_period, len(texts[1])),
                span(2, 0, second_period),
            ),
            (
                span(2, second_period, len(texts[2])),
                span(3, 0, third_period),
            ),
            (span(3, third_period, fourth_period),),
            (
                span(3, fourth_period, len(texts[3])),
                span(4, 0, len(texts[4])),
            ),
            (span(5, 0, len(texts[5])),),
        )
        original = initial.nodes[0]
        reviewed_nodes = []
        parent_review_node_id = "ai-review-long-clause-rollup"
        for index, source_spans in enumerate(partitions):
            ordered_unit_ids = tuple(
                dict.fromkeys(item.semantic_unit_id for item in source_spans)
            )
            reviewed_nodes.append(
                original.model_copy(
                    update={
                        "review_node_id": (
                            parent_review_node_id
                            if index == 0
                            else f"ai-review-long-clause-atomic-{index}"
                        ),
                        "source_semantic_unit_ids": ordered_unit_ids,
                        "source_owner_structure_node_ids": tuple(
                            prediction_by_unit[unit_id].owner_structure_node_id
                            for unit_id in ordered_unit_ids
                        ),
                        "source_spans": source_spans,
                        "source_order": min(
                            prediction_by_unit[unit_id].source_order
                            for unit_id in ordered_unit_ids
                        ),
                        "role": (
                            RequirementProjectionRole.PRIMARY
                            if index == 0
                            else RequirementProjectionRole.CHILD
                        ),
                        "parent_review_node_id": (
                            None if index == 0 else parent_review_node_id
                        ),
                        "decision_mode": (
                            RequirementDecisionMode.ROLLUP
                            if index == 0
                            else RequirementDecisionMode.DIRECT
                        ),
                        "reason_codes": ("ai_review_atomic_partition",),
                    }
                )
            )

        reviewed = initial.model_copy(
            update={
                "revision": 1,
                "reviewer_label": "synthetic-ai-reviewer",
                "nodes": tuple(reviewed_nodes),
            }
        )
        validate_llm_requirement_review(prediction, reviewed)
        delta = evaluate_llm_requirement_review(prediction, reviewed)

        self.assertEqual(len(reviewed.nodes), 7)
        self.assertEqual(
            sum(
                item.decision_mode is RequirementDecisionMode.DIRECT
                for item in reviewed.nodes
            ),
            6,
        )
        self.assertEqual(
            sum(
                item.decision_mode is RequirementDecisionMode.ROLLUP
                for item in reviewed.nodes
            ),
            1,
        )
        self.assertEqual(delta.metrics.split_target_count, 1)
        self.assertEqual(delta.metrics.edited_target_count, 1)

        now = datetime.now(timezone.utc)
        candidate = build_llm_requirement_review_reference_candidate(
            LlmRequirementReviewSession(
                session_id="llm-review-" + "f" * 32,
                project_id="project-a",
                document_id="document-a",
                prediction=prediction,
                reviewed_result=reviewed,
                delta=delta,
                created_at_utc=now,
                updated_at_utc=now,
            )
        )
        self.assertTrue(candidate.reviewer_label_present)
        self.assertEqual(candidate.status.value, "NOT_CAPTURED")
        self.assertFalse(candidate.independent_review_verified)
        self.assertFalse(candidate.human_gold)
        self.assertFalse(candidate.serving_activation_eligible)
        self.assertEqual(set(unit_ids), set(prediction.semantic_unit_ids))

    def test_prediction_rejects_an_unknown_review_owner(self) -> None:
        payload = _prediction_with_owned_continuation().model_dump(mode="json")
        payload["nodes"][1]["review_owner_semantic_unit_id"] = (
            "semantic-unit-" + "f" * 32
        )
        payload["nodes"][1]["review_owner_structure_node_id"] = (
            "structure-node-" + "f" * 32
        )

        with self.assertRaisesRegex(
            ValueError,
            "review source owner must be another predicted target",
        ):
            LlmRequirementModelPrediction.model_validate(payload)

    def test_review_tracks_ordered_human_confirmations_separately_from_model_agreement(
        self,
    ) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        confirmed_id = initial.nodes[1].review_node_id

        reviewed = LlmRequirementReviewedResult(
            **{
                **initial.model_dump(),
                "revision": 1,
                "confirmed_review_node_ids": (confirmed_id,),
            }
        )

        self.assertEqual(reviewed.confirmed_review_node_ids, (confirmed_id,))
        self.assertEqual(
            evaluate_llm_requirement_review(
                self.prediction,
                reviewed,
            ).metrics.confirmed_target_count,
            3,
        )

    def test_review_rejects_confirmation_for_an_unknown_review_node(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)

        with self.assertRaisesRegex(ValueError, "must belong"):
            LlmRequirementReviewedResult(
                **{
                    **initial.model_dump(),
                    "confirmed_review_node_ids": ("unknown-review-node",),
                }
            )

    def test_review_can_reclassify_one_target_and_records_exact_delta(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        changed = initial.nodes[2].model_copy(
            update={
                "semantic_class": SemanticClass.REQUIREMENT,
                "requirement_subtype": RequirementSubtype.INTERFACE_REQUIREMENT,
                "role": RequirementProjectionRole.CHILD,
                "decision_mode": RequirementDecisionMode.DIRECT,
            }
        )
        reviewed = LlmRequirementReviewedResult(
            prediction_id=self.prediction.prediction_id,
            revision=1,
            status=LlmRequirementReviewStatus.HUMAN_REVIEWED,
            reviewer_label="reviewer-a",
            nodes=(*initial.nodes[:2], changed),
        )

        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        changed_operations = {
            item.operation
            for item in delta.operations
            if item.source_semantic_unit_ids
            == (self.prediction.nodes[2].semantic_unit_id,)
        }
        self.assertIn(
            LlmRequirementReviewOperationType.RECLASSIFIED,
            changed_operations,
        )
        self.assertIn(
            LlmRequirementReviewOperationType.SUBTYPE_CHANGED,
            changed_operations,
        )
        self.assertIn(
            LlmRequirementReviewOperationType.ROLE_CHANGED,
            changed_operations,
        )
        self.assertIn(
            LlmRequirementReviewOperationType.DECISION_MODE_CHANGED,
            changed_operations,
        )
        self.assertEqual(delta.metrics.confirmed_target_count, 2)
        self.assertEqual(delta.metrics.semantic_class_agreement, 2 / 3)
        self.assertEqual(delta.metrics.requirement_subtype_compared_target_count, 3)

    def test_review_can_promote_a_child_into_an_independent_requirement(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        parent = initial.nodes[0].model_copy(
            update={"decision_mode": RequirementDecisionMode.DIRECT}
        )
        split_child = initial.nodes[1].model_copy(
            update={
                "role": RequirementProjectionRole.PRIMARY,
                "parent_review_node_id": None,
            }
        )
        reviewed = initial.model_copy(
            update={
                "revision": 1,
                "nodes": (parent, split_child, initial.nodes[2]),
            }
        )

        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        self.assertEqual(delta.metrics.split_target_count, 0)
        self.assertEqual(delta.metrics.promoted_target_count, 1)
        self.assertIn(
            LlmRequirementReviewOperationType.PROMOTED,
            {item.operation for item in delta.operations},
        )

    def test_review_can_split_one_target_into_exact_grounded_spans(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        original = initial.nodes[2]
        unit_id = original.source_semantic_unit_ids[0]
        owner_id = original.source_owner_structure_node_ids[0]
        text_length = len(self.prediction.nodes[2].review_text)
        split_at = 16
        first = original.model_copy(
            update={
                "review_node_id": "review-span-first",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=0,
                        end_offset=split_at,
                    ),
                ),
            }
        )
        second = original.model_copy(
            update={
                "review_node_id": "review-span-second",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=split_at,
                        end_offset=text_length,
                    ),
                ),
            }
        )
        reviewed = initial.model_copy(
            update={
                "revision": 1,
                "nodes": (*initial.nodes[:2], first, second),
            }
        )

        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        self.assertEqual(delta.metrics.reviewed_node_count, 4)
        self.assertEqual(delta.metrics.split_target_count, 1)
        self.assertEqual(delta.metrics.edited_target_count, 1)
        split = next(
            item
            for item in delta.operations
            if item.operation is LlmRequirementReviewOperationType.SPLIT
        )
        self.assertEqual(split.source_semantic_unit_ids, (unit_id,))
        self.assertEqual(split.after, 2)

    def test_review_can_merge_source_targets_without_losing_grounding(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        child = initial.nodes[1]
        information = initial.nodes[2]
        merged = child.model_copy(
            update={
                "source_semantic_unit_ids": (
                    child.source_semantic_unit_ids[0],
                    information.source_semantic_unit_ids[0],
                ),
                "source_owner_structure_node_ids": (
                    child.source_owner_structure_node_ids[0],
                    information.source_owner_structure_node_ids[0],
                ),
                "source_spans": (*child.source_spans, *information.source_spans),
            }
        )
        reviewed = initial.model_copy(
            update={"revision": 1, "nodes": (initial.nodes[0], merged)}
        )

        delta = evaluate_llm_requirement_review(self.prediction, reviewed)

        self.assertEqual(delta.metrics.reviewed_node_count, 2)
        self.assertEqual(delta.metrics.merged_review_node_count, 1)
        self.assertIn(
            LlmRequirementReviewOperationType.MERGED,
            {item.operation for item in delta.operations},
        )
        self.assertEqual(
            set(merged.source_semantic_unit_ids),
            {item.semantic_unit_id for item in self.prediction.nodes[1:]},
        )

    def test_merge_delta_counts_distinct_review_owners_not_continuation_sources(
        self,
    ) -> None:
        prediction = _prediction_with_owned_continuation_and_peer()
        initial = initial_llm_requirement_review(prediction)
        owner, peer = initial.nodes
        merged = owner.model_copy(
            update={
                "source_semantic_unit_ids": (
                    *owner.source_semantic_unit_ids,
                    *peer.source_semantic_unit_ids,
                ),
                "source_owner_structure_node_ids": (
                    *owner.source_owner_structure_node_ids,
                    *peer.source_owner_structure_node_ids,
                ),
                "source_spans": (*owner.source_spans, *peer.source_spans),
            }
        )
        reviewed = initial.model_copy(update={"revision": 1, "nodes": (merged,)})

        delta = evaluate_llm_requirement_review(prediction, reviewed)

        operation = next(
            item
            for item in delta.operations
            if item.operation is LlmRequirementReviewOperationType.MERGED
        )
        self.assertEqual(operation.before, 2)
        self.assertEqual(operation.after, 1)
        self.assertEqual(
            operation.source_semantic_unit_ids,
            tuple(item.semantic_unit_id for item in prediction.nodes),
        )

    def test_review_rejects_overlapping_source_spans(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        duplicate = initial.nodes[2].model_copy(
            update={
                "review_node_id": "review-overlapping-span",
                "source_semantic_unit_ids": (
                    initial.nodes[1].source_semantic_unit_ids[0],
                ),
                "source_owner_structure_node_ids": (
                    initial.nodes[1].source_owner_structure_node_ids[0],
                ),
                "source_spans": initial.nodes[1].source_spans,
                "source_order": initial.nodes[1].source_order,
            }
        )
        with self.assertRaisesRegex(ValueError, "exactly partition"):
            validate_llm_requirement_review(
                self.prediction,
                initial.model_copy(
                    update={
                        "nodes": (
                            initial.nodes[0],
                            initial.nodes[1],
                            duplicate,
                            initial.nodes[2],
                        )
                    }
                ),
            )

    def test_review_rejects_a_gap_between_source_spans(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        original = initial.nodes[2]
        unit_id = original.source_semantic_unit_ids[0]
        owner_id = original.source_owner_structure_node_ids[0]
        text_length = len(self.prediction.nodes[2].review_text)
        first = original.model_copy(
            update={
                "review_node_id": "review-gap-first",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=0,
                        end_offset=10,
                    ),
                ),
            }
        )
        second = original.model_copy(
            update={
                "review_node_id": "review-gap-second",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=11,
                        end_offset=text_length,
                    ),
                ),
            }
        )

        with self.assertRaisesRegex(ValueError, "exactly partition"):
            validate_llm_requirement_review(
                self.prediction,
                initial.model_copy(
                    update={"nodes": (*initial.nodes[:2], first, second)}
                ),
            )

    def test_review_rejects_interleaved_source_span_owners(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)
        original = initial.nodes[2]
        unit_id = original.source_semantic_unit_ids[0]
        owner_id = original.source_owner_structure_node_ids[0]
        text_length = len(self.prediction.nodes[2].review_text)
        outer = original.model_copy(
            update={
                "review_node_id": "review-interleaved-outer",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=0,
                        end_offset=10,
                    ),
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=20,
                        end_offset=text_length,
                    ),
                ),
            }
        )
        inner = original.model_copy(
            update={
                "review_node_id": "review-interleaved-inner",
                "source_spans": (
                    LlmReviewSourceSpan(
                        semantic_unit_id=unit_id,
                        owner_structure_node_id=owner_id,
                        start_offset=10,
                        end_offset=20,
                    ),
                ),
            }
        )

        with self.assertRaisesRegex(ValueError, "cannot interleave"):
            validate_llm_requirement_review(
                self.prediction,
                initial.model_copy(
                    update={"nodes": (*initial.nodes[:2], outer, inner)}
                ),
            )

    def test_completed_review_requires_reviewer_identity(self) -> None:
        initial = initial_llm_requirement_review(self.prediction)

        with self.assertRaisesRegex(ValueError, "requires a reviewer label"):
            validate_llm_requirement_review(
                self.prediction,
                initial.model_copy(
                    update={"status": LlmRequirementReviewStatus.HUMAN_REVIEWED}
                ),
            )


def _prediction() -> LlmRequirementModelPrediction:
    source_ir_id = "sir-" + "a" * 32
    targets = tuple("structure-node-" + char * 32 for char in ("1", "2", "3"))
    refs = tuple(
        StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + char * 32,
        )
        for char in ("4", "5", "6")
    )
    nodes = (
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=targets[0],
            source_order=0,
            source_ref=refs[0],
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            role=RequirementProjectionRole.PRIMARY,
            parent_id=None,
            decision_mode=RequirementDecisionMode.ROLLUP,
        ),
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=targets[1],
            source_order=1,
            source_ref=refs[1],
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=(RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT),
            role=RequirementProjectionRole.CHILD,
            parent_id=targets[0],
            decision_mode=RequirementDecisionMode.DIRECT,
        ),
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=targets[2],
            source_order=2,
            source_ref=refs[2],
            semantic_class=SemanticClass.TECHNICAL_INFORMATION,
            requirement_subtype=None,
            role=RequirementProjectionRole.CONTENT,
            parent_id=targets[0],
            decision_mode=RequirementDecisionMode.NONE,
        ),
    )
    kwargs = {
        "source_ir_id": source_ir_id,
        "source_sha256": "b" * 64,
        "structure_snapshot_id": "structure-" + "c" * 32,
        "scope_locator_sha256": "d" * 64,
        "provider_id": "bailian-qwen-semantic",
        "provider_version": "2.11.0",
        "model_id": "qwen3.7-plus",
        "classification_prompt_version": "semantic-classification-v12",
        "assembly_prompt_version": "requirement-assembly-v11",
        "semantic_unit_plan_ids": (_semantic_unit_plan_id(source_ir_id),),
        "semantic_unit_ids": tuple(node.semantic_unit_id for node in nodes),
        "provider_run_ids": ("provider-run-a", "provider-run-b"),
        "nodes": nodes,
    }
    return LlmRequirementModelPrediction(
        prediction_id=stable_llm_requirement_prediction_id(**kwargs),
        **kwargs,
    )


def _prediction_node(
    *,
    source_ir_id: str,
    target_id: str,
    source_order: int,
    source_ref: StructureSourceRef,
    semantic_class: SemanticClass,
    requirement_subtype: RequirementSubtype | None,
    role: RequirementProjectionRole | None,
    parent_id: str | None,
    decision_mode: RequirementDecisionMode,
    include_in_view: bool = True,
    review_owner_id: str | None = None,
    review_text: str | None = None,
) -> LlmRequirementPredictionNode:
    source_refs = (source_ref,)
    review_text = review_text or (
        f"review target {source_order} first clause\nsecond clause"
    )
    semantic_unit_id = _semantic_unit_id(target_id)
    return LlmRequirementPredictionNode(
        semantic_unit_id=semantic_unit_id,
        source_semantic_unit_ids=(semantic_unit_id,),
        owner_structure_node_id=target_id,
        source_owner_structure_node_ids=(target_id,),
        review_owner_semantic_unit_id=(
            _semantic_unit_id(review_owner_id) if review_owner_id is not None else None
        ),
        review_owner_structure_node_id=review_owner_id,
        unit_order=source_order,
        source_order=source_order,
        semantic_class=semantic_class,
        requirement_subtype=requirement_subtype,
        include_in_view=include_in_view,
        role=role,
        parent_semantic_unit_id=(
            _semantic_unit_id(parent_id) if parent_id is not None else None
        ),
        parent_owner_structure_node_id=parent_id,
        decision_mode=decision_mode,
        source_refs=source_refs,
        source_binding_sha256=source_binding_sha256(
            source_ir_id=source_ir_id,
            semantic_unit_id=semantic_unit_id,
            source_semantic_unit_ids=(semantic_unit_id,),
            owner_structure_node_id=target_id,
            source_owner_structure_node_ids=(target_id,),
            source_refs=source_refs,
        ),
        review_text=review_text,
        review_text_sha256=hashlib.sha256(review_text.encode("utf-8")).hexdigest(),
    )


def _prediction_with_owned_continuation() -> LlmRequirementModelPrediction:
    source_ir_id = "sir-" + "9" * 32
    owner_id = "structure-node-" + "7" * 32
    continuation_id = "structure-node-" + "8" * 32
    nodes = (
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=owner_id,
            source_order=0,
            source_ref=StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id="node-" + "7" * 32,
            ),
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            role=RequirementProjectionRole.PRIMARY,
            parent_id=None,
            decision_mode=RequirementDecisionMode.DIRECT,
        ),
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=continuation_id,
            source_order=1,
            source_ref=StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id="node-" + "8" * 32,
            ),
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
            role=None,
            parent_id=None,
            decision_mode=RequirementDecisionMode.NONE,
            include_in_view=False,
            review_owner_id=owner_id,
        ),
    )
    kwargs = {
        "source_ir_id": source_ir_id,
        "source_sha256": "9" * 64,
        "structure_snapshot_id": "structure-" + "9" * 32,
        "scope_locator_sha256": "8" * 64,
        "provider_id": "bailian-qwen-semantic",
        "provider_version": "2.16.0",
        "model_id": "qwen3.7-plus",
        "classification_prompt_version": "semantic-classification-v15",
        "assembly_prompt_version": "requirement-assembly-v12",
        "semantic_unit_plan_ids": (_semantic_unit_plan_id(source_ir_id),),
        "semantic_unit_ids": tuple(node.semantic_unit_id for node in nodes),
        "provider_run_ids": ("provider-run-continuation",),
        "nodes": nodes,
    }
    return LlmRequirementModelPrediction(
        prediction_id=stable_llm_requirement_prediction_id(**kwargs),
        **kwargs,
    )


def _prediction_with_owned_continuation_and_peer() -> LlmRequirementModelPrediction:
    base = _prediction_with_owned_continuation()
    peer_id = "structure-node-" + "6" * 32
    peer = _prediction_node(
        source_ir_id=base.source_ir_id,
        target_id=peer_id,
        source_order=2,
        source_ref=StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "6" * 32,
        ),
        semantic_class=SemanticClass.REQUIREMENT,
        requirement_subtype=RequirementSubtype.PRODUCT_REQUIREMENT,
        role=RequirementProjectionRole.PRIMARY,
        parent_id=None,
        decision_mode=RequirementDecisionMode.DIRECT,
    )
    nodes = (*base.nodes, peer)
    kwargs = {
        "source_ir_id": base.source_ir_id,
        "source_sha256": base.source_sha256,
        "structure_snapshot_id": base.structure_snapshot_id,
        "scope_locator_sha256": base.scope_locator_sha256,
        "provider_id": base.provider_id,
        "provider_version": base.provider_version,
        "model_id": base.model_id,
        "classification_prompt_version": base.classification_prompt_version,
        "assembly_prompt_version": base.assembly_prompt_version,
        "semantic_unit_plan_ids": base.semantic_unit_plan_ids,
        "semantic_unit_ids": tuple(node.semantic_unit_id for node in nodes),
        "provider_run_ids": base.provider_run_ids,
        "nodes": nodes,
    }
    return LlmRequirementModelPrediction(
        prediction_id=stable_llm_requirement_prediction_id(**kwargs),
        **kwargs,
    )


def _prediction_with_long_owned_continuation() -> LlmRequirementModelPrediction:
    source_ir_id = "sir-" + "a" * 32
    target_ids = tuple(
        f"structure-node-{character * 32}"
        for character in ("a", "b", "c", "d", "e", "f")
    )
    texts = (
        "4.2.1 When an interface changes, the supplier shall complete",
        "validation at an approved laboratory. The supplier shall pay",
        "the test fee. Test items shall follow Standard X and require",
        "customer confirmation. Execution shall follow Standard X. If",
        "a supplier-caused test fails, the supplier shall improve it,",
        "and shall bear the retest fee.",
    )
    owner_id = target_ids[0]
    nodes = tuple(
        _prediction_node(
            source_ir_id=source_ir_id,
            target_id=target_id,
            source_order=index,
            source_ref=StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=f"node-{character * 32}",
            ),
            semantic_class=SemanticClass.REQUIREMENT,
            requirement_subtype=RequirementSubtype.TEST_METHOD_REQUIREMENT,
            role=RequirementProjectionRole.PRIMARY if index == 0 else None,
            parent_id=None,
            decision_mode=(
                RequirementDecisionMode.DIRECT
                if index == 0
                else RequirementDecisionMode.NONE
            ),
            include_in_view=index == 0,
            review_owner_id=owner_id if index else None,
            review_text=texts[index],
        )
        for index, (character, target_id) in enumerate(
            zip(("a", "b", "c", "d", "e", "f"), target_ids, strict=True)
        )
    )
    kwargs = {
        "source_ir_id": source_ir_id,
        "source_sha256": "a" * 64,
        "structure_snapshot_id": "structure-" + "a" * 32,
        "scope_locator_sha256": "b" * 64,
        "provider_id": "synthetic-ai-reviewer",
        "provider_version": "1.0.0",
        "model_id": "review-only",
        "classification_prompt_version": "semantic-classification-v15",
        "assembly_prompt_version": "requirement-assembly-v12",
        "semantic_unit_plan_ids": (_semantic_unit_plan_id(source_ir_id),),
        "semantic_unit_ids": tuple(node.semantic_unit_id for node in nodes),
        "provider_run_ids": ("provider-run-long-continuation",),
        "nodes": nodes,
    }
    return LlmRequirementModelPrediction(
        prediction_id=stable_llm_requirement_prediction_id(**kwargs),
        **kwargs,
    )


def _semantic_unit_id(owner_structure_node_id: str) -> str:
    digest = hashlib.sha256(owner_structure_node_id.encode("utf-8")).hexdigest()
    return f"semantic-unit-{digest[:32]}"


def _semantic_unit_plan_id(source_ir_id: str) -> str:
    digest = hashlib.sha256(source_ir_id.encode("utf-8")).hexdigest()
    return f"semantic-unit-plan-{digest[:32]}"


if __name__ == "__main__":
    unittest.main()
