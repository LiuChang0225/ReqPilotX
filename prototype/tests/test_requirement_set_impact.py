from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from app.application.requirement_set_impact import compare_requirement_set_impact
from app.domain.requirement_set_impact import (
    RequirementSetImpactInputNodeV1,
    RequirementSetImpactOutcome,
    RequirementSetImpactReason,
    RequirementSetImpactReceiptV1,
    canonical_requirement_set_impact_receipt_sha256,
)
from pydantic import ValidationError


def _node(
    node_id: str,
    *,
    requirement_id: str | None = None,
    has_result: bool = False,
    intrinsic: str | None = "a" * 64,
    analysis_input: str | None = "b" * 64,
    metadata: dict | None = None,
) -> RequirementSetImpactInputNodeV1:
    return RequirementSetImpactInputNodeV1(
        node_id=node_id,
        requirement_id=requirement_id or f"requirement-{node_id}",
        has_current_result=has_result,
        intrinsic_sha256=intrinsic,
        analysis_input_sha256=analysis_input,
        result_metadata=metadata or {},
    )


def _compare(old_nodes, new_nodes):
    return compare_requirement_set_impact(
        project_id="project-1",
        document_id="document-1",
        old_requirement_set_id="set-old",
        new_requirement_set_id="set-new",
        old_tree_sha256="1" * 64,
        new_tree_sha256="2" * 64,
        matrix_state_schema_version="requirement-matrix-state/v1",
        old_matrix_state_sha256="3" * 64,
        new_matrix_state_sha256="4" * 64,
        old_nodes=old_nodes,
        new_nodes=new_nodes,
    )


class RequirementSetImpactTests(unittest.TestCase):
    def test_full_equivalence_retains_result_across_row_identity_change(self) -> None:
        receipt = _compare(
            [
                _node(
                    "node-a",
                    requirement_id="old-row",
                    has_result=True,
                    metadata={"assessment_id": "assessment-1"},
                )
            ],
            [_node("node-a", requirement_id="new-row")],
        )

        self.assertEqual(receipt.retained_result_node_ids, ("node-a",))
        self.assertEqual(receipt.stale_result_node_ids, ())
        self.assertEqual(receipt.changed_node_ids, ())
        self.assertEqual(
            receipt.node_results[0].outcome,
            RequirementSetImpactOutcome.RETAINED,
        )
        self.assertEqual(
            receipt.node_results[0].source_result_metadata,
            {"assessment_id": "assessment-1"},
        )
        self.assertEqual(
            receipt.reason_counts,
            {RequirementSetImpactReason.FULL_EQUIVALENCE.value: 1},
        )

    def test_intrinsic_and_dependency_changes_are_reported_separately(self) -> None:
        receipt = _compare(
            [
                _node("changed", has_result=True),
                _node("dependent", has_result=True),
                _node("unrelated", has_result=True),
            ],
            [
                _node("changed", intrinsic="c" * 64),
                _node("dependent", analysis_input="d" * 64),
                _node("unrelated"),
            ],
        )

        self.assertEqual(receipt.changed_node_ids, ("changed",))
        self.assertEqual(receipt.stale_result_node_ids, ("changed", "dependent"))
        self.assertEqual(receipt.retained_result_node_ids, ("unrelated",))
        self.assertEqual(
            receipt.reason_counts,
            {
                "ANALYSIS_INPUT_CHANGED": 1,
                "FULL_EQUIVALENCE": 1,
                "INTRINSIC_CHANGED": 1,
            },
        )

    def test_missing_hash_fails_closed_without_claiming_input_change(self) -> None:
        receipt = _compare(
            [_node("legacy", has_result=True, analysis_input=None)],
            [_node("legacy")],
        )

        result = receipt.node_results[0]
        self.assertEqual(result.outcome, RequirementSetImpactOutcome.STALE)
        self.assertEqual(
            result.reasons,
            (RequirementSetImpactReason.UNPROVEN_EQUIVALENCE,),
        )
        self.assertFalse(result.intrinsic_changed)
        self.assertEqual(receipt.changed_node_ids, ())

    def test_missing_intrinsic_hash_is_conservatively_changed_and_stale(self) -> None:
        receipt = _compare(
            [_node("legacy", has_result=True, intrinsic=None)],
            [_node("legacy")],
        )

        self.assertEqual(receipt.stale_result_node_ids, ("legacy",))
        self.assertEqual(receipt.changed_node_ids, ("legacy",))
        self.assertEqual(
            receipt.node_results[0].reasons,
            (RequirementSetImpactReason.UNPROVEN_EQUIVALENCE,),
        )

    def test_no_prior_result_is_not_mislabeled_as_retained_or_stale(self) -> None:
        receipt = _compare([_node("node-a")], [_node("node-a")])

        self.assertEqual(
            receipt.node_results[0].outcome,
            RequirementSetImpactOutcome.NO_PRIOR_RESULT,
        )
        self.assertEqual(receipt.retained_result_node_ids, ())
        self.assertEqual(receipt.stale_result_node_ids, ())
        self.assertEqual(receipt.reason_counts, {"NO_PRIOR_RESULT": 1})

    def test_added_and_removed_nodes_are_deterministic(self) -> None:
        receipt = _compare(
            [_node("removed", has_result=True, metadata={"run_id": "run-1"})],
            [_node("added")],
        )

        self.assertEqual(
            tuple(item.node_id for item in receipt.node_results),
            ("added", "removed"),
        )
        self.assertEqual(receipt.new_node_ids, ("added",))
        self.assertEqual(receipt.removed_node_ids, ("removed",))
        self.assertEqual(receipt.changed_node_ids, ("added", "removed"))
        self.assertEqual(receipt.retained_result_node_ids, ())
        self.assertEqual(receipt.stale_result_node_ids, ())
        self.assertEqual(
            receipt.node_results[1].source_result_metadata,
            {"run_id": "run-1"},
        )

    def test_zero_node_scope_produces_a_valid_empty_receipt(self) -> None:
        receipt = _compare([], [])

        self.assertEqual(receipt.node_results, ())
        self.assertEqual(receipt.retained_result_node_ids, ())
        self.assertEqual(receipt.stale_result_node_ids, ())
        self.assertEqual(receipt.changed_node_ids, ())
        self.assertEqual(receipt.reason_counts, {})

    def test_receipt_and_hash_are_stable_across_input_order_and_mapping_input(self) -> None:
        first = _compare(
            [_node("b", has_result=True), _node("a", has_result=True)],
            [_node("a"), _node("b")],
        )
        second = _compare(
            [
                _node("a", has_result=True).model_dump(mode="json"),
                _node("b", has_result=True).model_dump(mode="json"),
            ],
            [
                _node("b").model_dump(mode="json"),
                _node("a").model_dump(mode="json"),
            ],
        )

        self.assertEqual(first, second)
        self.assertEqual(
            first.payload_sha256,
            canonical_requirement_set_impact_receipt_sha256(first),
        )

    def test_duplicate_node_identity_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate old impact node_id"):
            _compare([_node("duplicate"), _node("duplicate")], [])

    def test_invalid_result_binding_and_non_json_metadata_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            RequirementSetImpactInputNodeV1(
                node_id="node-a",
                has_current_result=True,
                intrinsic_sha256="a" * 64,
                analysis_input_sha256="b" * 64,
            )
        with self.assertRaises(ValidationError):
            _node(
                "node-a",
                has_result=True,
                metadata={"invalid": object()},
            )

    def test_tampered_receipt_summary_and_payload_are_rejected(self) -> None:
        receipt = _compare(
            [_node("node-a", has_result=True)],
            [_node("node-a")],
        )
        payload = receipt.model_dump(mode="json")
        payload["retained_result_node_ids"] = []
        with self.assertRaises(ValidationError):
            RequirementSetImpactReceiptV1.model_validate(payload)

        payload = receipt.model_dump(mode="json")
        payload["payload_sha256"] = "0" * 64
        with self.assertRaises(ValidationError):
            RequirementSetImpactReceiptV1.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
