from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.adapters.agents.structure_failure_pattern_host import (
    StructureFailurePatternHost,
)
from app.application.agent_workflow.structure_tool_loop import (
    NativeStructureHandoffAttemptRequestV1,
    StructureAgentCandidateRevisionV1,
    StructureHandoffAttemptStatus,
    StructureMaterializationObservationV1,
    StructureProjectionOperationKind,
    StructureProjectionOperationV1,
    StructureRevisionBasisRole,
    StructureToolCallReceiptV1,
    StructureToolIntent,
    StructureToolOutcome,
    StructureToolResultRole,
    StructureWorkItemDispositionV1,
    build_native_structure_handoff_attempt_receipt,
    stable_structure_agent_revision_id,
)


def _sha(character: str) -> str:
    return character * 64


def _closed_observation() -> StructureMaterializationObservationV1:
    return StructureMaterializationObservationV1.model_validate(
        {
            "schema_version": "structure-materialization-observation/v1",
            "confirmed_actions": [
                {
                    "action_id": "action-1",
                    "materialization_receipt_id": "materialized-1",
                    "verification_receipt_id": "verified-1",
                    "rendered_output_reopened": True,
                }
            ],
            "parent_direct_source_refs": ["source-parent"],
            "child_owned_source_refs": ["source-child"],
            "node_title": "One title",
            "visible_title_lines": ["One title"],
            "visual_scope_bbox": [0, 0, 10, 10],
            "rendered_cells": [
                {"cell_id": "cell-1", "bbox": [1, 1, 9, 9], "text": "value"}
            ],
            "following_heading_texts": ["Later heading"],
            "continuation_segments": [
                {"segment_id": "segment-1", "canonical_columns": ["a", "b"]},
                {"segment_id": "segment-2", "canonical_columns": ["a", "b"]},
            ],
            "expected_image_ids": ["image-1"],
            "image_dispositions": [
                {"image_id": "image-1", "disposition": "FLOATING_IN_TABLE"}
            ],
            "navigation_children": [
                {
                    "child_id": "child-1",
                    "source_kind": "HEADING",
                    "independently_navigable": True,
                }
            ],
        }
    )


class StructureToolLoopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = StructureFailurePatternHost()

    def test_evidence_complete_scope_does_not_require_a_tool_call(self) -> None:
        request = self._request(
            observation=_closed_observation(),
            work_item_dispositions=(
                StructureWorkItemDispositionV1(
                    input_kind="FEEDBACK",
                    input_id="feedback-1",
                    outcome="RESOLVED",
                    reason_code="source_scope_verified",
                ),
            ),
        )
        result = self.host.validate_and_select(request.materialization_observation)
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=result.validation_report,
            selected_failure_patterns=result.selected_failure_patterns,
            kit_version=result.kit_version,
            kit_bundle_sha256=result.kit_bundle_sha256,
        )

        self.assertEqual(receipt.status, StructureHandoffAttemptStatus.VERIFIED)
        self.assertEqual(receipt.tool_calls, ())
        self.assertEqual(len(receipt.accepted_work_item_dispositions), 1)

    def test_agent_can_replace_an_advisory_tool_candidate(self) -> None:
        call = StructureToolCallReceiptV1(
            call_id="tool-1",
            intent=StructureToolIntent.RESTORE_TABLE_CANDIDATE,
            reason_signals=("cell_text_contains_following_heading",),
            outcome=StructureToolOutcome.SUCCEEDED,
            result_role=StructureToolResultRole.ADVISORY_CANDIDATE,
            result_sha256=_sha("b"),
        )
        revision = self._revision(
            basis_role=StructureRevisionBasisRole.ADVISORY_CANDIDATE,
            basis_sha256=_sha("b"),
            revised_candidate_sha256=_sha("c"),
            tool_call_ids=("tool-1",),
        )
        request = self._request(
            observation=_closed_observation(),
            tool_calls=(call,),
            candidate_revisions=(revision,),
        )
        result = self.host.validate_and_select(request.materialization_observation)
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=result.validation_report,
            selected_failure_patterns=result.selected_failure_patterns,
            kit_version=result.kit_version,
            kit_bundle_sha256=result.kit_bundle_sha256,
        )

        self.assertEqual(receipt.status, StructureHandoffAttemptStatus.VERIFIED)
        self.assertEqual(receipt.candidate_revisions[0].basis_sha256, _sha("b"))
        self.assertEqual(
            receipt.candidate_revisions[0].revised_candidate_sha256,
            _sha("c"),
        )

    def test_raw_source_evidence_cannot_be_claimed_as_an_editable_candidate(
        self,
    ) -> None:
        call = StructureToolCallReceiptV1(
            call_id="tool-source",
            intent=StructureToolIntent.READ_NATIVE_TEXT,
            reason_signals=(),
            outcome=StructureToolOutcome.SUCCEEDED,
            result_role=StructureToolResultRole.SOURCE_EVIDENCE,
            result_sha256=_sha("d"),
        )
        revision = self._revision(
            basis_role=StructureRevisionBasisRole.ADVISORY_CANDIDATE,
            basis_sha256=_sha("d"),
            revised_candidate_sha256=_sha("e"),
            tool_call_ids=("tool-source",),
        )

        with self.assertRaises(ValidationError):
            self._request(
                observation=_closed_observation(),
                tool_calls=(call,),
                candidate_revisions=(revision,),
            )

    def test_failed_validation_returns_only_targeted_signals_and_no_consumption(
        self,
    ) -> None:
        observation = StructureMaterializationObservationV1.model_validate(
            {
                **_closed_observation().model_dump(mode="json"),
                "confirmed_actions": [
                    {
                        "action_id": "action-1",
                        "materialization_receipt_id": "",
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                ],
                "rendered_cells": [
                    {
                        "cell_id": "cell-1",
                        "bbox": [0, 0, 12, 5],
                        "text": "value Later heading",
                    }
                ],
                "expected_image_ids": ["image-1", "image-2"],
                "image_dispositions": [
                    {"image_id": "image-1", "disposition": "PLACED_IN_CELL"}
                ],
            }
        )
        request = self._request(
            observation=observation,
            work_item_dispositions=(
                StructureWorkItemDispositionV1(
                    input_kind="FEEDBACK",
                    input_id="feedback-1",
                    outcome="RESOLVED",
                    reason_code="candidate_rebuilt",
                ),
            ),
        )
        result = self.host.validate_and_select(observation)
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=result.validation_report,
            selected_failure_patterns=result.selected_failure_patterns,
            kit_version=result.kit_version,
            kit_bundle_sha256=result.kit_bundle_sha256,
        )

        self.assertEqual(
            receipt.status,
            StructureHandoffAttemptStatus.REVISION_REQUIRED,
        )
        self.assertEqual(receipt.accepted_work_item_dispositions, ())
        self.assertLessEqual(
            len(result.selected_failure_patterns["selected_patterns"]),
            3,
        )
        self.assertIn("rendered_output_not_reopened", result.observed_signals)
        self.assertIn("image_not_dispositioned", result.observed_signals)

    def test_materialized_candidate_waits_for_rendered_verification(self) -> None:
        observation = StructureMaterializationObservationV1.model_validate(
            {
                **_closed_observation().model_dump(mode="json"),
                "confirmed_actions": [
                    {
                        "action_id": "action-1",
                        "materialization_receipt_id": "materialized-1",
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                ],
            }
        )
        request = self._request(observation=observation)
        result = self.host.validate_and_select(observation)
        receipt = build_native_structure_handoff_attempt_receipt(
            request,
            validation_report=result.validation_report,
            selected_failure_patterns=result.selected_failure_patterns,
            kit_version=result.kit_version,
            kit_bundle_sha256=result.kit_bundle_sha256,
        )

        self.assertEqual(
            receipt.status,
            StructureHandoffAttemptStatus.VERIFICATION_REQUIRED,
        )
        self.assertEqual(receipt.accepted_work_item_dispositions, ())

    def test_retry_must_replace_the_failed_materialization(self) -> None:
        failed_observation = StructureMaterializationObservationV1.model_validate(
            {
                **_closed_observation().model_dump(mode="json"),
                "confirmed_actions": [
                    {
                        "action_id": "action-1",
                        "materialization_receipt_id": "",
                        "verification_receipt_id": "",
                        "rendered_output_reopened": False,
                    }
                ],
            }
        )
        first_request = self._request(observation=failed_observation)
        first_host = self.host.validate_and_select(failed_observation)
        first = build_native_structure_handoff_attempt_receipt(
            first_request,
            validation_report=first_host.validation_report,
            selected_failure_patterns=first_host.selected_failure_patterns,
            kit_version=first_host.kit_version,
            kit_bundle_sha256=first_host.kit_bundle_sha256,
        )
        revision = self._revision(
            basis_role=StructureRevisionBasisRole.PREVIOUS_MATERIALIZATION,
            basis_sha256=first.materialized_candidate_sha256,
            revised_candidate_sha256=_sha("f"),
        )
        second_request = self._request(
            command_id="attempt-command-2",
            attempt_ordinal=2,
            previous_attempt_receipt_sha256=first.receipt_sha256,
            observation=_closed_observation(),
            candidate_revisions=(revision,),
            materialized_candidate_sha256=_sha("f"),
        )
        second_host = self.host.validate_and_select(
            second_request.materialization_observation
        )
        second = build_native_structure_handoff_attempt_receipt(
            second_request,
            validation_report=second_host.validation_report,
            selected_failure_patterns=second_host.selected_failure_patterns,
            kit_version=second_host.kit_version,
            kit_bundle_sha256=second_host.kit_bundle_sha256,
            previous_attempt=first,
        )

        self.assertEqual(second.status, StructureHandoffAttemptStatus.VERIFIED)
        self.assertEqual(
            second.previous_attempt_receipt_sha256,
            first.receipt_sha256,
        )

    def _request(
        self,
        *,
        observation: StructureMaterializationObservationV1,
        command_id: str = "attempt-command-1",
        attempt_ordinal: int = 1,
        previous_attempt_receipt_sha256: str | None = None,
        tool_calls: tuple[StructureToolCallReceiptV1, ...] = (),
        candidate_revisions: tuple[StructureAgentCandidateRevisionV1, ...] = (),
        materialized_candidate_sha256: str = _sha("a"),
        work_item_dispositions: tuple[StructureWorkItemDispositionV1, ...] = (),
    ) -> NativeStructureHandoffAttemptRequestV1:
        return NativeStructureHandoffAttemptRequestV1(
            command_id=command_id,
            expected_source_sha256=_sha("1"),
            expected_artifact_id="artifact-1",
            handoff_id="handoff-1",
            attempt_ordinal=attempt_ordinal,
            previous_attempt_receipt_sha256=previous_attempt_receipt_sha256,
            affected_scope_ids=("scope-1",),
            tool_calls=tool_calls,
            candidate_revisions=candidate_revisions,
            materialized_candidate_sha256=materialized_candidate_sha256,
            rendered_output_sha256=_sha("2"),
            materialization_observation=observation,
            work_item_dispositions=work_item_dispositions,
            current_action="正在复验局部结构。",
            public_summary="局部候选已重新物化并执行 Host 后置检查。",
        )

    def _revision(
        self,
        *,
        basis_role: StructureRevisionBasisRole,
        basis_sha256: str,
        revised_candidate_sha256: str,
        tool_call_ids: tuple[str, ...] = (),
    ) -> StructureAgentCandidateRevisionV1:
        values = {
            "target_scope_id": "scope-1",
            "basis_role": basis_role,
            "basis_sha256": basis_sha256,
            "revised_candidate_sha256": revised_candidate_sha256,
            "operations": (
                StructureProjectionOperationV1(
                    operation=(
                        StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES
                    ),
                    target_scope_id="scope-1",
                ),
            ),
            "evidence_refs": ("evidence-1",),
            "tool_call_ids": tool_call_ids,
        }
        return StructureAgentCandidateRevisionV1(
            revision_id=stable_structure_agent_revision_id(**values),
            **values,
        )


if __name__ == "__main__":
    unittest.main()
