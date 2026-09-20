from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from pydantic import ValidationError

from app.application.agent_workflow.d3_parent_provisions import (
    ParentProvisionSourceRangeV3,
    canonical_json_sha256,
)
from app.application.agent_workflow.parent_provision_nested_evidence import (
    ParentProvisionNestedEvidenceAssignmentClosureV1,
    ParentProvisionNestedEvidenceAssignmentStatus,
    ParentProvisionNestedEvidenceEligibilityV1,
    ParentProvisionNestedEvidenceError,
    build_parent_provision_nested_evidence_assignment_closure,
    validate_parent_provision_nested_evidence_assignment_closure,
)

if __package__:
    from .test_parent_provision_professional_aggregate import _finalize, _scenario
else:
    from test_parent_provision_professional_aggregate import _finalize, _scenario


class ParentProvisionNestedEvidenceTests(unittest.TestCase):
    def test_unique_full_owner_segment_builds_stable_content_free_closure(
        self,
    ) -> None:
        scenario = _scenario()
        segment = scenario.segments[0]
        eligibility = _eligibility(segment)

        first = _closure(scenario, (eligibility,))
        second = _closure(scenario, (eligibility,))

        self.assertEqual(first, second)
        self.assertEqual(
            first.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )
        self.assertEqual(len(first.assignments), 1)
        self.assertEqual(
            first.assignments[0].selected_parent_provision_id,
            scenario.aggregate.compiled_decision.entries[0].parent_provision_id,
        )
        self.assertEqual(first.unassigned_evidence_ids, ())
        serialized = first.model_dump_json()
        self.assertNotIn(segment.source_text, serialized)
        self.assertNotIn("Controller shall stop", serialized)

        replayed = validate_parent_provision_nested_evidence_assignment_closure(
            expected_stage_input_bundle_sha256=scenario.plan.stage_input_bundle_sha256,
            expected_stage_input_manifest_sha256="7" * 64,
            decision=scenario.aggregate.compiled_decision,
            source_segments=scenario.segments,
            expected_eligibilities=(eligibility,),
            closure=first,
        )
        self.assertEqual(replayed, first)

    def test_split_owner_segment_remains_incomplete_and_cannot_validate(
        self,
    ) -> None:
        scenario = _scenario()
        segment = scenario.segments[0]
        decision = _split_first_parent(scenario)
        eligibility = _eligibility(segment)

        closure = build_parent_provision_nested_evidence_assignment_closure(
            stage_input_bundle_sha256=scenario.plan.stage_input_bundle_sha256,
            stage_input_manifest_sha256="7" * 64,
            decision=decision,
            source_segments=scenario.segments,
            eligibilities=(eligibility,),
        )

        self.assertEqual(
            closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.INCOMPLETE,
        )
        self.assertEqual(closure.assignments, ())
        self.assertEqual(closure.unassigned_evidence_ids, (eligibility.evidence_id,))
        with self.assertRaisesRegex(
            ParentProvisionNestedEvidenceError,
            "ASSIGNMENT_INCOMPLETE",
        ):
            validate_parent_provision_nested_evidence_assignment_closure(
                expected_stage_input_bundle_sha256=(
                    scenario.plan.stage_input_bundle_sha256
                ),
                expected_stage_input_manifest_sha256="7" * 64,
                decision=decision,
                source_segments=scenario.segments,
                expected_eligibilities=(eligibility,),
                closure=closure,
            )

    def test_owner_segment_and_assignment_tampering_fail_closed(self) -> None:
        scenario = _scenario()
        eligibility = _eligibility(scenario.segments[0])
        bad_values = eligibility.model_dump(
            mode="json",
            exclude={"eligibility_sha256"},
        )
        bad_values["owner_segment_ids"] = (scenario.segments[1].segment_id,)
        bad_draft = ParentProvisionNestedEvidenceEligibilityV1.model_construct(
            **bad_values,
            eligibility_sha256="0" * 64,
        )
        bad_owner = ParentProvisionNestedEvidenceEligibilityV1(
            **bad_values,
            eligibility_sha256=canonical_json_sha256(
                bad_draft.model_dump(
                    mode="json",
                    exclude={"eligibility_sha256"},
                )
            ),
        )
        with self.assertRaisesRegex(
            ParentProvisionNestedEvidenceError,
            "OWNER_SEGMENT_DRIFT",
        ):
            _closure(scenario, (bad_owner,))

        closure = _closure(scenario, (eligibility,))
        payload = closure.model_dump(mode="json")
        payload["assignments"][0]["selected_parent_provision_id"] = (
            "parent-provision-" + "f" * 32
        )
        with self.assertRaisesRegex(ValueError, "assignment SHA drifted"):
            ParentProvisionNestedEvidenceAssignmentClosureV1.model_validate(payload)

    def test_blank_owner_segment_has_zero_obligation_but_all_blank_stays_unassigned(
        self,
    ) -> None:
        scenario = _scenario()
        non_blank = scenario.segments[0]
        blank = _blank_segment(non_blank)
        mixed = _eligibility(
            non_blank,
            owner_segment_ids=(non_blank.segment_id, blank.segment_id),
        )

        mixed_closure = build_parent_provision_nested_evidence_assignment_closure(
            stage_input_bundle_sha256=scenario.plan.stage_input_bundle_sha256,
            stage_input_manifest_sha256="7" * 64,
            decision=scenario.aggregate.compiled_decision,
            source_segments=(*scenario.segments, blank),
            eligibilities=(mixed,),
        )

        self.assertEqual(
            mixed_closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )
        self.assertEqual(len(mixed_closure.assignments), 1)

        all_blank = _eligibility(blank, owner_segment_ids=(blank.segment_id,))
        blank_closure = build_parent_provision_nested_evidence_assignment_closure(
            stage_input_bundle_sha256=scenario.plan.stage_input_bundle_sha256,
            stage_input_manifest_sha256="7" * 64,
            decision=scenario.aggregate.compiled_decision,
            source_segments=(*scenario.segments, blank),
            eligibilities=(all_blank,),
        )
        self.assertEqual(
            blank_closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.INCOMPLETE,
        )
        self.assertEqual(blank_closure.assignments, ())

    def test_path_or_free_text_public_evidence_ids_are_rejected(self) -> None:
        scenario = _scenario()
        valid = _eligibility(scenario.segments[0])
        for field_name, unsafe in (
            ("evidence_id", "/Users/customer/secret.pdf"),
            ("evidence_locator_id", "customer requirement text"),
        ):
            with self.subTest(field_name=field_name):
                payload = valid.model_dump(mode="json")
                payload[field_name] = unsafe
                with self.assertRaises(ValidationError):
                    ParentProvisionNestedEvidenceEligibilityV1.model_validate(payload)

    def test_empty_region_eligibility_still_has_explicit_complete_closure(self) -> None:
        scenario = _scenario()

        closure = _closure(scenario, ())

        self.assertEqual(
            closure.status,
            ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE,
        )
        self.assertEqual(closure.eligibilities, ())
        self.assertEqual(closure.assignments, ())
        self.assertEqual(
            closure.eligibility_ledger_sha256,
            canonical_json_sha256(()),
        )

    def test_legacy_v1_receipt_and_proof_serialization_identity_do_not_drift(
        self,
    ) -> None:
        scenario = _scenario()
        result = _finalize(scenario)

        receipt_payload = scenario.finalized_receipt.model_dump(mode="json")
        proof_payload = result.professional_proof.model_dump(mode="json")
        self.assertNotIn(
            "nested_evidence_assignment_closure_sha256",
            receipt_payload,
        )
        self.assertNotIn(
            "nested_evidence_assignment_closure_sha256",
            proof_payload,
        )
        self.assertEqual(
            scenario.finalized_receipt.receipt_sha256,
            canonical_json_sha256(
                {
                    key: value
                    for key, value in receipt_payload.items()
                    if key not in {"execution_id", "receipt_sha256"}
                }
            ),
        )
        self.assertEqual(
            result.professional_proof.proof_sha256,
            canonical_json_sha256(
                {
                    key: value
                    for key, value in proof_payload.items()
                    if key not in {"proof_id", "proof_sha256"}
                }
            ),
        )
        replay_scenario = _scenario()
        replay_result = _finalize(replay_scenario)
        self.assertEqual(
            scenario.finalized_receipt.receipt_sha256,
            replay_scenario.finalized_receipt.receipt_sha256,
        )
        self.assertEqual(
            scenario.finalized_receipt.final_execution_trace_sha256,
            replay_scenario.finalized_receipt.final_execution_trace_sha256,
        )
        self.assertEqual(
            result.professional_proof.proof_sha256,
            replay_result.professional_proof.proof_sha256,
        )


def _eligibility(
    segment,
    *,
    owner_segment_ids: tuple[str, ...] | None = None,
) -> ParentProvisionNestedEvidenceEligibilityV1:  # type: ignore[no-untyped-def]
    values = {
        "evidence_id": "d2-table-region-excluded-evidence-" + "5" * 32,
        "evidence_kind": "NESTED_REGION",
        "evidence_locator_id": "d2-table-region-" + "6" * 32,
        "ultimate_owner_logical_object_id": segment.logical_object_id,
        "owner_segment_ids": owner_segment_ids or (segment.segment_id,),
        "source_evidence_sha256": "8" * 64,
        "source_ledger_sha256": "9" * 64,
    }
    draft = ParentProvisionNestedEvidenceEligibilityV1.model_construct(
        **values,
        eligibility_sha256="0" * 64,
    )
    return ParentProvisionNestedEvidenceEligibilityV1(
        **values,
        eligibility_sha256=canonical_json_sha256(
            draft.model_dump(mode="json", exclude={"eligibility_sha256"})
        ),
    )


def _blank_segment(segment):  # type: ignore[no-untyped-def]
    text = " \t "
    return segment.model_copy(
        update={
            "segment_id": "d2-table-region-source-segment-" + "e" * 32,
            "source_handle": "table-cell:blank",
            "handle_start_offset": 0,
            "handle_end_offset": len(text),
            "source_text": text,
            "source_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }
    )


def _closure(scenario, eligibilities):  # type: ignore[no-untyped-def]
    return build_parent_provision_nested_evidence_assignment_closure(
        stage_input_bundle_sha256=scenario.plan.stage_input_bundle_sha256,
        stage_input_manifest_sha256="7" * 64,
        decision=scenario.aggregate.compiled_decision,
        source_segments=scenario.segments,
        eligibilities=eligibilities,
    )


def _split_first_parent(scenario):  # type: ignore[no-untyped-def]
    decision = scenario.aggregate.compiled_decision
    original = decision.entries[0]
    segment = scenario.segments[0]
    cut = segment.source_text.index("shall")
    first_text = segment.source_text[:cut]
    second_text = segment.source_text[cut:]

    def source_range(start: int, text: str) -> ParentProvisionSourceRangeV3:
        return ParentProvisionSourceRangeV3(
            segment_id=segment.segment_id,
            start_offset=start,
            end_offset=start + len(text),
            verbatim_text=text,
            verbatim_text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    first = original.model_copy(
        update={
            "parent_provision_id": "parent-provision-" + "a" * 32,
            "source_order": 0,
            "source_ranges": (source_range(0, first_text),),
        }
    )
    second = original.model_copy(
        update={
            "parent_provision_id": "parent-provision-" + "b" * 32,
            "source_order": 1,
            "source_ranges": (source_range(cut, second_text),),
        }
    )
    remaining = tuple(
        item.model_copy(update={"source_order": item.source_order + 1})
        for item in decision.entries[1:]
    )
    return decision.model_copy(update={"entries": (first, second, *remaining)})


if __name__ == "__main__":
    unittest.main()
