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

from app.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAggregateExecutionReceiptV2,
    ParentProvisionRecognitionAggregateStatus,
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionAttemptReceiptV1,
    ParentProvisionRecognitionBatchReceiptV1,
    ParentProvisionRecognitionBatchStatus,
    ParentProvisionRecognitionCleanupStatus,
    build_parent_provision_recognition_aggregate_execution_receipt,
    build_parent_provision_recognition_attempt_receipt,
    build_parent_provision_recognition_batch_receipt,
    canonical_json_sha256,
)


RUN_ID = "agent-run-" + "1" * 32
SOURCE_SHA256 = "2" * 64
PREDECESSOR_ID = "agent-artifact-" + "3" * 32
PREDECESSOR_SHA256 = "4" * 64
KIT_SHA256 = "5" * 64
PLAN_SHA256 = "6" * 64
PLAN_ID = "parent-provision-object-plan-" + PLAN_SHA256[:32]
POLICY_SHA256 = "7" * 64
RUNNER_SHA256 = "8" * 64


class ParentProvisionBatchReceiptTests(unittest.TestCase):
    def test_validated_attempt_has_stable_content_free_identity(self) -> None:
        attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
            semantic_unresolved_scope_count=2,
        )

        self.assertTrue(attempt.content_free)
        self.assertEqual(attempt.semantic_unresolved_scope_count, 2)
        self.assertTrue(attempt.receipt_id.endswith(attempt.receipt_sha256[:32]))
        self.assertNotIn("source_text", attempt.model_dump_json())

        tampered = attempt.model_dump(mode="json")
        tampered["tool_receipt_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "receipt SHA drifted"):
            ParentProvisionRecognitionAttemptReceiptV1.model_validate(tampered)

    def test_failure_cancel_and_indeterminate_cannot_claim_semantic_unresolved(
        self,
    ) -> None:
        for outcome, cleanup, reasons in (
            (
                ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
                ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
                ("APP_SERVER_TIMEOUT",),
            ),
            (
                ParentProvisionRecognitionAttemptOutcome.CANCELLED,
                ParentProvisionRecognitionCleanupStatus.SUCCEEDED,
                ("USER_CANCELLED",),
            ),
            (
                ParentProvisionRecognitionAttemptOutcome.BLOCKED,
                ParentProvisionRecognitionCleanupStatus.FAILED,
                ("CLEANUP_FAILED",),
            ),
            (
                ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
                ParentProvisionRecognitionCleanupStatus.UNKNOWN,
                ("PROCESS_STATE_UNKNOWN",),
            ),
        ):
            with self.subTest(outcome=outcome):
                values = _attempt_values(
                    batch_index=0,
                    attempt_ordinal=1,
                    outcome=outcome,
                    semantic_unresolved_scope_count=1,
                    cleanup_status=cleanup,
                    workspace_unchanged=(
                        outcome is not ParentProvisionRecognitionAttemptOutcome.BLOCKED
                    ),
                    reason_codes=reasons,
                )
                with self.assertRaisesRegex(
                    ValidationError,
                    "cannot manufacture semantic unresolved",
                ):
                    build_parent_provision_recognition_attempt_receipt(**values)

    def test_output_rejection_preserves_transport_proof_but_is_not_accepted(
        self,
    ) -> None:
        attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
        )

        self.assertIsNotNone(attempt.transport_result_sha256)
        self.assertIsNotNone(attempt.candidate_sha256)
        self.assertIsNone(attempt.batch_compilation_result_sha256)
        self.assertEqual(attempt.semantic_unresolved_scope_count, 0)

    def test_receipt_binds_existing_object_plan_and_target_without_new_batch_identity(
        self,
    ) -> None:
        attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
        )
        self.assertEqual(attempt.plan_id, PLAN_ID)
        self.assertEqual(
            attempt.batch_id,
            "parent-provision-object-batch-" + attempt.target_scope_sha256[:32],
        )

        values = _attempt_values(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
        )
        values["plan_id"] = "parent-provision-object-plan-" + "f" * 32
        with self.assertRaisesRegex(ValueError, "object batch plan identity drifted"):
            build_parent_provision_recognition_attempt_receipt(**values)

    def test_batch_accepts_first_host_validated_retry_and_stops(self) -> None:
        rejected = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
        )
        accepted = _attempt(
            batch_index=0,
            attempt_ordinal=2,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
            semantic_unresolved_scope_count=1,
        )
        batch = _batch(
            batch_index=0,
            attempts=(rejected, accepted),
            status=(
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
            ),
            accepted=accepted,
            semantic_unresolved_scope_count=1,
        )

        self.assertEqual(batch.accepted_attempt_id, accepted.attempt_id)
        self.assertEqual(batch.attempts, (rejected, accepted))
        self.assertEqual(batch.selection_policy, "FIRST_HOST_VALIDATED_RETRY/v1")

        later = _attempt(
            batch_index=0,
            attempt_ordinal=3,
            outcome=ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "first Host-validated attempt and stop retrying",
        ):
            _batch(
                batch_index=0,
                attempts=(rejected, accepted, later),
                status=(
                    ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
                ),
                accepted=accepted,
                semantic_unresolved_scope_count=1,
            )

    def test_failed_and_cancelled_batches_have_no_accepted_or_unresolved_claim(
        self,
    ) -> None:
        failed_attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        )
        failed = _batch(
            batch_index=0,
            attempts=(failed_attempt,),
            status=ParentProvisionRecognitionBatchStatus.FAILED,
            reason_codes=("RETRY_BUDGET_EXHAUSTED",),
        )
        cancelled = _batch(
            batch_index=1,
            attempts=(),
            status=ParentProvisionRecognitionBatchStatus.CANCELLED,
            reason_codes=("USER_CANCELLED_BEFORE_ATTEMPT",),
        )

        self.assertIsNone(failed.accepted_attempt_id)
        self.assertEqual(failed.semantic_unresolved_scope_count, 0)
        self.assertEqual(cancelled.attempts, ())

        payload = cancelled.model_dump(mode="json")
        payload["semantic_unresolved_scope_count"] = 1
        with self.assertRaisesRegex(
            ValidationError,
            "cannot claim acceptance or semantic unresolved",
        ):
            ParentProvisionRecognitionBatchReceiptV1.model_validate(payload)

    def test_host_empty_container_has_a_receipt_without_model_attempt(self) -> None:
        receipt = _batch(
            batch_index=0,
            attempts=(),
            status=(
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
            ),
            reason_codes=("HOST_EMPTY_CONTAINER",),
        )

        self.assertEqual(receipt.attempts, ())
        self.assertIsNone(receipt.accepted_attempt_id)
        self.assertEqual(receipt.semantic_unresolved_scope_count, 0)

        aggregate = _aggregate(
            batches=(receipt, _accepted_batch(batch_index=1)),
            planned_batch_count=2,
            status=ParentProvisionRecognitionAggregateStatus.COMPLETE,
            semantic_unresolved_scope_count=0,
            final_hashes=True,
        )
        self.assertEqual(
            aggregate.status,
            ParentProvisionRecognitionAggregateStatus.COMPLETE,
        )
        self.assertEqual(len(aggregate.accepted_attempt_ids), 1)

    def test_validated_reconciliation_candidate_blocks_final_decision(self) -> None:
        attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
        )
        batch = _batch(
            batch_index=0,
            attempts=(attempt,),
            accepted=attempt,
            status=(
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
            ),
            reason_codes=("ADJACENT_RECONCILIATION_REQUIRED",),
        )
        aggregate = _aggregate(
            batches=(batch,),
            planned_batch_count=2,
            status=ParentProvisionRecognitionAggregateStatus.BLOCKED,
            semantic_unresolved_scope_count=0,
            final_hashes=False,
            reason_codes=("ADJACENT_RECONCILIATION_REQUIRED",),
        )

        self.assertEqual(
            aggregate.status, ParentProvisionRecognitionAggregateStatus.BLOCKED
        )
        self.assertIsNone(aggregate.parent_provision_decision_sha256)

    def test_complete_aggregate_binds_every_batch_attempt_and_final_decision(
        self,
    ) -> None:
        batch0 = _accepted_batch(batch_index=0)
        batch1 = _accepted_batch(batch_index=1)
        aggregate = _aggregate(
            batches=(batch0, batch1),
            planned_batch_count=2,
            status=ParentProvisionRecognitionAggregateStatus.COMPLETE,
            semantic_unresolved_scope_count=0,
            final_hashes=True,
        )

        self.assertEqual(
            aggregate.accepted_attempt_ids,
            (batch0.accepted_attempt_id, batch1.accepted_attempt_id),
        )
        expected_inventory = canonical_json_sha256(
            tuple(
                attempt.receipt_sha256
                for batch in (batch0, batch1)
                for attempt in batch.attempts
            )
        )
        self.assertEqual(aggregate.attempt_inventory_sha256, expected_inventory)
        self.assertTrue(aggregate.execution_id.endswith(aggregate.receipt_sha256[:32]))

        tampered = aggregate.model_dump(mode="json")
        tampered["accepted_attempts_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "receipt roots drifted"):
            ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
                tampered
            )

    def test_semantic_partial_requires_accepted_validated_candidate(self) -> None:
        batch0 = _accepted_batch(batch_index=0, semantic_unresolved_scope_count=2)
        aggregate = _aggregate(
            batches=(batch0,),
            planned_batch_count=1,
            status=ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
            semantic_unresolved_scope_count=2,
            final_hashes=True,
        )
        self.assertEqual(aggregate.status.value, "PARTIAL_SEMANTIC")

        payload = aggregate.model_dump(mode="json")
        payload["semantic_unresolved_scope_count"] = 0
        with self.assertRaisesRegex(
            ValidationError,
            "semantic unresolved counts drifted",
        ):
            ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
                payload
            )

    def test_unresolved_entry_makes_aggregate_semantically_partial(self) -> None:
        batch = _accepted_batch(
            batch_index=0,
            semantic_unresolved_entry_count=1,
        )
        aggregate = _aggregate(
            batches=(batch,),
            planned_batch_count=1,
            status=ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
            semantic_unresolved_scope_count=0,
            semantic_unresolved_entry_count=1,
            final_hashes=True,
        )

        self.assertEqual(aggregate.semantic_unresolved_scope_count, 0)
        self.assertEqual(aggregate.semantic_unresolved_entry_count, 1)

    def test_partial_predecessor_keeps_aggregate_partial(self) -> None:
        batch = _accepted_batch(batch_index=0)
        aggregate = _aggregate(
            batches=(batch,),
            planned_batch_count=1,
            status=ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
            semantic_unresolved_scope_count=0,
            final_hashes=True,
            predecessor_partial=True,
        )

        self.assertTrue(aggregate.predecessor_partial)

    def test_cancelled_aggregate_keeps_audit_history_but_has_no_final_decision(
        self,
    ) -> None:
        accepted = _accepted_batch(batch_index=0)
        cancelled = _batch(
            batch_index=1,
            attempts=(),
            status=ParentProvisionRecognitionBatchStatus.CANCELLED,
            reason_codes=("USER_CANCELLED_BEFORE_ATTEMPT",),
        )
        aggregate = _aggregate(
            batches=(accepted, cancelled),
            planned_batch_count=3,
            status=ParentProvisionRecognitionAggregateStatus.CANCELLED,
            semantic_unresolved_scope_count=0,
            final_hashes=False,
            reason_codes=("USER_CANCELLED",),
        )

        self.assertEqual(
            aggregate.accepted_attempt_ids, (accepted.accepted_attempt_id,)
        )
        self.assertIsNone(aggregate.parent_provision_decision_sha256)

        payload = aggregate.model_dump(mode="json")
        payload["semantic_unresolved_scope_count"] = 1
        with self.assertRaisesRegex(
            ValidationError,
            "cannot become semantic unresolved",
        ):
            ParentProvisionRecognitionAggregateExecutionReceiptV2.model_validate(
                payload
            )

    def test_object_failure_does_not_hide_later_completed_batches(self) -> None:
        failed_attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        )
        failed = _batch(
            batch_index=0,
            attempts=(failed_attempt,),
            status=ParentProvisionRecognitionBatchStatus.FAILED,
            reason_codes=("RETRY_BUDGET_EXHAUSTED",),
        )
        later = _accepted_batch(batch_index=1)

        aggregate = _aggregate(
            batches=(failed, later),
            planned_batch_count=2,
            status=ParentProvisionRecognitionAggregateStatus.FAILED,
            semantic_unresolved_scope_count=0,
            final_hashes=False,
            reason_codes=("BATCH_FAILED",),
        )

        self.assertEqual(len(aggregate.batches), 2)
        self.assertEqual(
            aggregate.accepted_attempt_ids,
            (later.accepted_attempt_id,),
        )

    def test_multiple_object_issues_have_deterministic_aggregate_precedence(
        self,
    ) -> None:
        reconciliation_attempt = _attempt(
            batch_index=0,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
        )
        reconciliation = _batch(
            batch_index=0,
            attempts=(reconciliation_attempt,),
            accepted=reconciliation_attempt,
            status=(
                ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
            ),
            reason_codes=("ADJACENT_RECONCILIATION_REQUIRED",),
        )
        failed_attempt = _attempt(
            batch_index=1,
            attempt_ordinal=1,
            outcome=ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
        )
        failed = _batch(
            batch_index=1,
            attempts=(failed_attempt,),
            status=ParentProvisionRecognitionBatchStatus.FAILED,
            reason_codes=("RETRY_BUDGET_EXHAUSTED",),
        )
        later = _accepted_batch(batch_index=2)

        aggregate = _aggregate(
            batches=(reconciliation, failed, later),
            planned_batch_count=3,
            status=ParentProvisionRecognitionAggregateStatus.BLOCKED,
            semantic_unresolved_scope_count=0,
            final_hashes=False,
            reason_codes=(
                "ADJACENT_RECONCILIATION_REQUIRED",
                "BATCH_FAILED",
            ),
        )

        self.assertEqual(
            aggregate.status,
            ParentProvisionRecognitionAggregateStatus.BLOCKED,
        )
        self.assertEqual(len(aggregate.accepted_attempt_ids), 2)


def _scope_sha256(batch_index: int) -> str:
    return canonical_json_sha256({"batch_index": batch_index, "scope": "synthetic"})


def _batch_id(batch_index: int) -> str:
    return "parent-provision-object-batch-" + _scope_sha256(batch_index)[:32]


def _attempt_values(
    *,
    batch_index: int,
    attempt_ordinal: int,
    outcome: ParentProvisionRecognitionAttemptOutcome,
    semantic_unresolved_scope_count: int = 0,
    semantic_unresolved_entry_count: int = 0,
    cleanup_status: ParentProvisionRecognitionCleanupStatus | None = None,
    workspace_unchanged: bool = True,
    reason_codes: tuple[str, ...] | None = None,
) -> dict[str, object]:
    validated = outcome is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE
    rejected = outcome is ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED
    transport_completed = validated or rejected
    if cleanup_status is None:
        cleanup_status = ParentProvisionRecognitionCleanupStatus.SUCCEEDED
    if reason_codes is None:
        reason_codes = (
            ()
            if validated
            else {
                ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED: (
                    "APP_SERVER_TIMEOUT",
                ),
                ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED: (
                    "HOST_OUTPUT_REJECTED",
                ),
                ParentProvisionRecognitionAttemptOutcome.CANCELLED: ("USER_CANCELLED",),
                ParentProvisionRecognitionAttemptOutcome.BLOCKED: (
                    "EXECUTION_BLOCKED",
                ),
                ParentProvisionRecognitionAttemptOutcome.INDETERMINATE: (
                    "PROCESS_STATE_UNKNOWN",
                ),
            }[outcome]
        )
    seed = f"{batch_index}-{attempt_ordinal}"
    return {
        "run_id": RUN_ID,
        "source_sha256": SOURCE_SHA256,
        "predecessor_artifact_id": PREDECESSOR_ID,
        "predecessor_artifact_sha256": PREDECESSOR_SHA256,
        "kit_bundle_sha256": KIT_SHA256,
        "plan_id": PLAN_ID,
        "plan_sha256": PLAN_SHA256,
        "batch_id": _batch_id(batch_index),
        "batch_order": batch_index,
        "target_scope_sha256": _scope_sha256(batch_index),
        "attempt_ordinal": attempt_ordinal,
        "task_binding_sha256": canonical_json_sha256({"task": seed}),
        "stage_session_attestation_sha256": "9" * 64,
        "stage_context_sha256": "a" * 64,
        "skill_sha256": "b" * 64,
        "runner_output_schema_sha256": "c" * 64,
        "model_id": "gpt-5.6-sol",
        "reasoning_effort": "high",
        "outcome": outcome,
        "transport_result_sha256": "d" * 64 if transport_completed else None,
        "response_sha256": "e" * 64 if transport_completed else None,
        "tool_receipt_sha256": "f" * 64 if transport_completed else None,
        "runtime_thread_id_sha256": "0" * 64 if transport_completed else None,
        "runtime_turn_id_sha256": "1" * 64 if transport_completed else None,
        "candidate_sha256": "2" * 64 if transport_completed else None,
        "batch_compilation_result_sha256": "3" * 64 if validated else None,
        "semantic_unresolved_scope_count": semantic_unresolved_scope_count,
        "semantic_unresolved_entry_count": semantic_unresolved_entry_count,
        "cleanup_status": cleanup_status,
        "workspace_unchanged": workspace_unchanged,
        "reason_codes": reason_codes,
    }


def _attempt(
    *,
    batch_index: int,
    attempt_ordinal: int,
    outcome: ParentProvisionRecognitionAttemptOutcome,
    semantic_unresolved_scope_count: int = 0,
    semantic_unresolved_entry_count: int = 0,
) -> ParentProvisionRecognitionAttemptReceiptV1:
    values = _attempt_values(
        batch_index=batch_index,
        attempt_ordinal=attempt_ordinal,
        outcome=outcome,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
    )
    return build_parent_provision_recognition_attempt_receipt(**values)


def _batch(
    *,
    batch_index: int,
    attempts: tuple[ParentProvisionRecognitionAttemptReceiptV1, ...],
    status: ParentProvisionRecognitionBatchStatus,
    accepted: ParentProvisionRecognitionAttemptReceiptV1 | None = None,
    semantic_unresolved_scope_count: int = 0,
    semantic_unresolved_entry_count: int = 0,
    reason_codes: tuple[str, ...] = (),
) -> ParentProvisionRecognitionBatchReceiptV1:
    return build_parent_provision_recognition_batch_receipt(
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        predecessor_artifact_id=PREDECESSOR_ID,
        predecessor_artifact_sha256=PREDECESSOR_SHA256,
        kit_bundle_sha256=KIT_SHA256,
        plan_id=PLAN_ID,
        plan_sha256=PLAN_SHA256,
        batch_id=_batch_id(batch_index),
        batch_order=batch_index,
        target_scope_sha256=_scope_sha256(batch_index),
        attempts=attempts,
        accepted_attempt_id=accepted.attempt_id if accepted is not None else None,
        accepted_attempt_receipt_sha256=(
            accepted.receipt_sha256 if accepted is not None else None
        ),
        status=status,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
        reason_codes=reason_codes,
    )


def _accepted_batch(
    *,
    batch_index: int,
    semantic_unresolved_scope_count: int = 0,
    semantic_unresolved_entry_count: int = 0,
) -> ParentProvisionRecognitionBatchReceiptV1:
    attempt = _attempt(
        batch_index=batch_index,
        attempt_ordinal=1,
        outcome=ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
    )
    return _batch(
        batch_index=batch_index,
        attempts=(attempt,),
        status=(
            ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
            if semantic_unresolved_scope_count or semantic_unresolved_entry_count
            else ParentProvisionRecognitionBatchStatus.ACCEPTED
        ),
        accepted=attempt,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
    )


def _aggregate(
    *,
    batches: tuple[ParentProvisionRecognitionBatchReceiptV1, ...],
    planned_batch_count: int,
    status: ParentProvisionRecognitionAggregateStatus,
    semantic_unresolved_scope_count: int,
    semantic_unresolved_entry_count: int = 0,
    final_hashes: bool,
    reason_codes: tuple[str, ...] = (),
    predecessor_partial: bool = False,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    return build_parent_provision_recognition_aggregate_execution_receipt(
        run_id=RUN_ID,
        source_sha256=SOURCE_SHA256,
        predecessor_artifact_id=PREDECESSOR_ID,
        predecessor_artifact_sha256=PREDECESSOR_SHA256,
        kit_bundle_sha256=KIT_SHA256,
        policy_closure_sha256=POLICY_SHA256,
        runner_profile_sha256=RUNNER_SHA256,
        model_id="gpt-5.6-sol",
        reasoning_effort="high",
        plan_id=PLAN_ID,
        plan_sha256=PLAN_SHA256,
        planned_batch_count=planned_batch_count,
        batches=batches,
        status=status,
        semantic_unresolved_scope_count=semantic_unresolved_scope_count,
        semantic_unresolved_entry_count=semantic_unresolved_entry_count,
        predecessor_partial=predecessor_partial,
        object_batch_aggregate_sha256="a" * 64 if final_hashes else None,
        decision_candidate_sha256="b" * 64 if final_hashes else None,
        parent_provision_decision_sha256="c" * 64 if final_hashes else None,
        evidence_refs_sha256="d" * 64 if final_hashes else None,
        reason_codes=reason_codes,
    )


if __name__ == "__main__":
    unittest.main()
