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

from app.application.agent_workflow.d2_logical_structure import D2LogicalObjectKind
from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceGap,
    ParentProvisionEvidenceRecoverability,
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
    build_parent_provision_evidence_recovery_attempt_receipt,
    build_parent_provision_evidence_recovery_plan,
    build_parent_provision_evidence_recovery_receipt,
    build_parent_provision_evidence_recovery_session,
    build_parent_provision_evidence_replan_receipt,
    canonical_json_sha256,
    stable_parent_provision_evidence_recovery_feedback_sha256,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectBatchTargetV1,
    ParentProvisionObjectExecutionMode,
    ParentProvisionObjectSegmentBindingV1,
    _target_evidence_sha256,
    _target_scope_sha256,
    build_parent_provision_object_batch_plan,
    require_parent_provision_object_evidence_recovery,
)

if __package__:
    from .test_parent_provision_object_batches import _fixture
else:
    from test_parent_provision_object_batches import _fixture


RUN_ID = "agent-run-" + "1" * 32
SOURCE_SHA256 = "2" * 64
KIT_SHA256 = "3" * 64
PREDECESSOR_ID = "agent-artifact-" + "4" * 32
PREDECESSOR_SHA256 = "5" * 64
PREDECESSOR_RECEIPT_SHA256 = "6" * 64
SOURCE_SCOPE_SHA256 = "7" * 64
OLD_STAGE_INPUT_SHA256 = "8" * 64
NEW_STAGE_INPUT_SHA256 = "9" * 64
RECOVERY_OBJECT_ID = "d2-logical-object-" + "a" * 32
REUSABLE_OBJECT_ID = "d2-logical-object-" + "b" * 32


class ParentProvisionEvidenceRecoveryTests(unittest.TestCase):
    def test_plan_is_immutable_finite_and_gap_specific(self) -> None:
        old_plan, _new_plan = _object_plans()
        target = old_plan.targets[0]
        recovery = _recovery_plan(
            old_plan,
            gap=ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
            recoverability=ParentProvisionEvidenceRecoverability.AUTOMATIC,
            strategies=(
                ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
                ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            ),
            max_attempts=2,
        )

        self.assertEqual(recovery.batch_id, target.batch_id)
        self.assertEqual(
            recovery.original_target_evidence_sha256, target.target_evidence_sha256
        )
        self.assertTrue(
            recovery.recovery_plan_id.endswith(recovery.recovery_plan_sha256[:32])
        )
        tampered = recovery.model_dump(mode="json")
        tampered["trigger_failure_code"] = "DIFFERENT_FAILURE"
        with self.assertRaisesRegex(ValidationError, "receipt SHA drifted"):
            type(recovery).model_validate(tampered)

        values = _recovery_plan_values(old_plan)
        values["ordered_strategies"] = (
            ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
            ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
        )
        with self.assertRaisesRegex(ValidationError, "ordered and distinct"):
            build_parent_provision_evidence_recovery_plan(**values)

    def test_required_visual_table_figure_rebind_and_replacement_strategies_exist(
        self,
    ) -> None:
        old_plan, _new_plan = _object_plans()
        cases = (
            (
                ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
                ParentProvisionEvidenceRecoverability.AUTOMATIC,
                ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
            ),
            (
                ParentProvisionEvidenceGap.MISSING_TABLE_EVIDENCE,
                ParentProvisionEvidenceRecoverability.AUTOMATIC,
                ParentProvisionEvidenceRecoveryStrategy.VISUAL_RECONSTRUCT_TABLE,
            ),
            (
                ParentProvisionEvidenceGap.MISSING_FIGURE_EVIDENCE,
                ParentProvisionEvidenceRecoverability.AUTOMATIC,
                ParentProvisionEvidenceRecoveryStrategy.RECONSTRUCT_FIGURE_EVIDENCE,
            ),
            (
                ParentProvisionEvidenceGap.EXISTING_EVIDENCE_UNBOUND,
                ParentProvisionEvidenceRecoverability.AUTOMATIC,
                ParentProvisionEvidenceRecoveryStrategy.REBIND_EXISTING_EVIDENCE,
            ),
            (
                ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED,
                ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
            ),
        )
        for gap, recoverability, strategy in cases:
            with self.subTest(gap=gap):
                plan = _recovery_plan(
                    old_plan,
                    gap=gap,
                    recoverability=recoverability,
                    strategies=(strategy,),
                    max_attempts=1,
                )
                self.assertEqual(plan.ordered_strategies, (strategy,))

    def test_attempt_failure_has_exact_reason_and_success_binds_new_evidence(
        self,
    ) -> None:
        old_plan, new_plan = _object_plans()
        recovery_plan = _recovery_plan(old_plan)
        session = build_parent_provision_evidence_recovery_session(plan=recovery_plan)
        failed = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        new_evidence = new_plan.targets[0].target_evidence_sha256
        recovered = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            feedback_sha256=(
                stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=failed
                )
            ),
            new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            new_target_evidence_sha256=new_evidence,
        )

        self.assertEqual(failed.failure_code, "NATIVE_TEXT_STILL_EMPTY")
        self.assertEqual(recovered.new_target_evidence_sha256, new_evidence)
        with self.assertRaisesRegex(ValidationError, "requires a changed stage input"):
            _attempt(
                session,
                ordinal=2,
                strategy=(
                    ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE
                ),
                feedback_sha256=(
                    stable_parent_provision_evidence_recovery_feedback_sha256(
                        previous_attempt=failed
                    )
                ),
                state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                new_stage_input_bundle_sha256=OLD_STAGE_INPUT_SHA256,
                new_target_evidence_sha256=(
                    recovery_plan.original_target_evidence_sha256
                ),
            )

        same_evidence_success = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            new_target_evidence_sha256=(recovery_plan.original_target_evidence_sha256),
        )
        self.assertEqual(
            same_evidence_success.new_target_evidence_sha256,
            recovery_plan.original_target_evidence_sha256,
        )

    def test_retry_must_switch_strategy_and_bind_previous_failure(self) -> None:
        old_plan, _new_plan = _object_plans()
        recovery_plan = _recovery_plan(old_plan, max_attempts=2)
        session = build_parent_provision_evidence_recovery_session(plan=recovery_plan)
        first = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        repeated = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            feedback_sha256=(
                stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=first
                )
            ),
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        with self.assertRaisesRegex(ValidationError, "advance to the next"):
            _recovery_receipt(
                session,
                attempts=(first, repeated),
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                reason_codes=("RECOVERY_FAILED",),
            )

        wrong_feedback_retry = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            feedback_sha256="c" * 64,
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        with self.assertRaisesRegex(ValidationError, "bind the previous failure"):
            _recovery_receipt(
                session,
                attempts=(first, wrong_feedback_retry),
                state=ParentProvisionEvidenceRecoveryState.FAILED,
                outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
                reason_codes=("RECOVERY_FAILED",),
            )

        feedback_retry = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            feedback_sha256=(
                stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=first
                )
            ),
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="TARGET_EVIDENCE_UNCHANGED",
        )
        receipt = _recovery_receipt(
            session,
            attempts=(first, feedback_retry),
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            reason_codes=("RECOVERY_FAILED",),
        )
        self.assertEqual(len(receipt.attempts), 2)

    def test_recovery_session_accepts_only_final_success(self) -> None:
        old_plan, new_plan = _object_plans()
        recovery_plan = _recovery_plan(old_plan)
        session = build_parent_provision_evidence_recovery_session(plan=recovery_plan)
        failed = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        recovered = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            feedback_sha256=(
                stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=failed
                )
            ),
            new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            new_target_evidence_sha256=new_plan.targets[0].target_evidence_sha256,
        )
        receipt = _recovery_receipt(
            session,
            attempts=(failed, recovered),
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            accepted=recovered,
        )

        self.assertEqual(receipt.accepted_attempt_id, recovered.attempt_id)
        self.assertTrue(receipt.receipt_id.endswith(receipt.receipt_sha256[:32]))

    def test_integrity_external_and_corrupt_source_cannot_auto_recover(self) -> None:
        old_plan, new_plan = _object_plans()
        cases = (
            (
                ParentProvisionEvidenceGap.INTEGRITY_DRIFT,
                ParentProvisionEvidenceRecoverability.INTEGRITY_REVIEW_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.STOP_FOR_INTEGRITY_REVIEW,
            ),
            (
                ParentProvisionEvidenceGap.EXTERNAL_SOURCE_MISSING,
                ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
            ),
            (
                ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED,
                ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
            ),
        )
        for gap, recoverability, strategy in cases:
            with self.subTest(gap=gap):
                plan = _recovery_plan(
                    old_plan,
                    gap=gap,
                    recoverability=recoverability,
                    strategies=(strategy,),
                    max_attempts=1,
                )
                session = build_parent_provision_evidence_recovery_session(plan=plan)
                fake_success = _attempt(
                    session,
                    ordinal=1,
                    strategy=strategy,
                    state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                    new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
                    new_target_evidence_sha256=new_plan.targets[
                        0
                    ].target_evidence_sha256,
                )
                with self.assertRaisesRegex(
                    ValidationError,
                    "cannot auto-recover",
                ):
                    _recovery_receipt(
                        session,
                        attempts=(fake_success,),
                        state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
                        outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                        accepted=fake_success,
                    )

    def test_exhausted_session_consumes_finite_budget(self) -> None:
        old_plan, _new_plan = _object_plans()
        plan = _recovery_plan(old_plan, max_attempts=2)
        session = build_parent_provision_evidence_recovery_session(plan=plan)
        first = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="NATIVE_TEXT_STILL_EMPTY",
        )
        second = _attempt(
            session,
            ordinal=2,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            feedback_sha256=(
                stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=first
                )
            ),
            state=ParentProvisionEvidenceRecoveryState.FAILED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            failure_code="TARGET_EVIDENCE_UNCHANGED",
        )
        attempts = (first, second)
        receipt = _recovery_receipt(
            session,
            attempts=attempts,
            state=ParentProvisionEvidenceRecoveryState.EXHAUSTED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            reason_codes=("ATTEMPT_BUDGET_EXHAUSTED",),
        )
        self.assertEqual(receipt.state, ParentProvisionEvidenceRecoveryState.EXHAUSTED)

    def test_replan_compares_target_evidence_not_stage_revision_scope(self) -> None:
        old_plan, new_plan = _object_plans()
        recovery_plan = _recovery_plan(old_plan)
        session = build_parent_provision_evidence_recovery_session(plan=recovery_plan)
        recovered = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            new_target_evidence_sha256=new_plan.targets[0].target_evidence_sha256,
        )
        receipt = _recovery_receipt(
            session,
            attempts=(recovered,),
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            accepted=recovered,
        )
        replan = build_parent_provision_evidence_replan_receipt(
            recovery_receipt=receipt,
            old_plan=old_plan,
            new_plan=new_plan,
        )

        self.assertEqual(replan.invalidated_logical_object_ids, (RECOVERY_OBJECT_ID,))
        self.assertEqual(replan.reusable_logical_object_ids, (REUSABLE_OBJECT_ID,))
        self.assertNotEqual(
            old_plan.targets[1].target_scope_sha256,
            new_plan.targets[1].target_scope_sha256,
        )
        self.assertEqual(
            old_plan.targets[1].target_evidence_sha256,
            new_plan.targets[1].target_evidence_sha256,
        )

    def test_rerender_recovery_invalidates_target_even_when_text_evidence_is_same(
        self,
    ) -> None:
        fixture = _fixture()
        ready_plan = build_parent_provision_object_batch_plan(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
            stage_input_bundle_sha256=OLD_STAGE_INPUT_SHA256,
        )
        ready_target = next(
            item
            for item in ready_plan.targets
            if item.execution_mode is ParentProvisionObjectExecutionMode.MODEL_TARGET
        )
        recovery_object_plan = require_parent_provision_object_evidence_recovery(
            plan=ready_plan,
            expected_plan_sha256=ready_plan.plan_sha256,
            logical_object_id=ready_target.logical_object_id,
        )
        recovery_target = next(
            item
            for item in recovery_object_plan.targets
            if item.logical_object_id == ready_target.logical_object_id
        )
        new_plan = build_parent_provision_object_batch_plan(
            d2_identity=fixture.identity,
            logical_objects=fixture.objects,
            source_segments=fixture.segments,
            stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
        )
        new_target = next(
            item
            for item in new_plan.targets
            if item.logical_object_id == ready_target.logical_object_id
        )
        recovery_plan = build_parent_provision_evidence_recovery_plan(
            run_id=recovery_object_plan.run_id,
            source_sha256=recovery_object_plan.source_sha256,
            kit_bundle_sha256=recovery_object_plan.kit_bundle_sha256,
            predecessor_artifact_id=recovery_object_plan.predecessor_artifact_id,
            predecessor_artifact_sha256=(
                recovery_object_plan.predecessor_artifact_sha256
            ),
            object_plan_id=recovery_object_plan.plan_id,
            object_plan_sha256=recovery_object_plan.plan_sha256,
            batch_id=recovery_target.batch_id,
            batch_order=recovery_target.batch_order,
            logical_object_id=recovery_target.logical_object_id,
            target_scope_sha256=recovery_target.target_scope_sha256,
            target_execution_mode=recovery_target.execution_mode.value,
            original_stage_input_bundle_sha256=(
                recovery_object_plan.stage_input_bundle_sha256
            ),
            original_target_evidence_sha256=(recovery_target.target_evidence_sha256),
            gap=ParentProvisionEvidenceGap.MISSING_PAGE_RENDER,
            recoverability=ParentProvisionEvidenceRecoverability.AUTOMATIC,
            trigger_failure_code="PAGE_RENDER_MISSING",
            ordered_strategies=(
                ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
                ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
            ),
            max_attempts=2,
        )
        session = build_parent_provision_evidence_recovery_session(plan=recovery_plan)
        recovered = _attempt(
            session,
            ordinal=1,
            strategy=ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            new_target_evidence_sha256=new_target.target_evidence_sha256,
        )
        receipt = _recovery_receipt(
            session,
            attempts=(recovered,),
            state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
            accepted=recovered,
        )

        replan = build_parent_provision_evidence_replan_receipt(
            recovery_receipt=receipt,
            old_plan=recovery_object_plan,
            new_plan=new_plan,
        )

        self.assertEqual(
            recovery_target.target_evidence_sha256,
            new_target.target_evidence_sha256,
        )
        self.assertIn(
            recovery_target.logical_object_id,
            replan.invalidated_logical_object_ids,
        )
        self.assertNotIn(
            recovery_target.logical_object_id,
            replan.reusable_logical_object_ids,
        )


def _recovery_plan_values(
    old_plan: ParentProvisionObjectBatchPlanV1,
) -> dict[str, object]:
    target = old_plan.targets[0]
    return {
        "run_id": old_plan.run_id,
        "source_sha256": old_plan.source_sha256,
        "kit_bundle_sha256": old_plan.kit_bundle_sha256,
        "predecessor_artifact_id": old_plan.predecessor_artifact_id,
        "predecessor_artifact_sha256": old_plan.predecessor_artifact_sha256,
        "object_plan_id": old_plan.plan_id,
        "object_plan_sha256": old_plan.plan_sha256,
        "batch_id": target.batch_id,
        "batch_order": target.batch_order,
        "logical_object_id": target.logical_object_id,
        "target_scope_sha256": target.target_scope_sha256,
        "target_execution_mode": target.execution_mode.value,
        "original_stage_input_bundle_sha256": old_plan.stage_input_bundle_sha256,
        "original_target_evidence_sha256": target.target_evidence_sha256,
        "gap": ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT,
        "recoverability": ParentProvisionEvidenceRecoverability.AUTOMATIC,
        "trigger_failure_code": "OBJECT_EVIDENCE_MISSING",
        "ordered_strategies": (
            ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
            ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
        ),
        "max_attempts": 2,
    }


def _recovery_plan(
    old_plan: ParentProvisionObjectBatchPlanV1,
    *,
    gap: ParentProvisionEvidenceGap = (
        ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT
    ),
    recoverability: ParentProvisionEvidenceRecoverability = (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    strategies: tuple[ParentProvisionEvidenceRecoveryStrategy, ...] = (
        ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    ),
    max_attempts: int = 2,
):  # type: ignore[no-untyped-def]
    values = _recovery_plan_values(old_plan)
    values.update(
        gap=gap,
        recoverability=recoverability,
        ordered_strategies=strategies,
        max_attempts=max_attempts,
    )
    return build_parent_provision_evidence_recovery_plan(**values)


def _attempt(
    session,  # type: ignore[no-untyped-def]
    *,
    ordinal: int,
    strategy: ParentProvisionEvidenceRecoveryStrategy,
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    failure_code: str | None = None,
    feedback_sha256: str | None = None,
    new_stage_input_bundle_sha256: str | None = None,
    new_target_evidence_sha256: str | None = None,
):  # type: ignore[no-untyped-def]
    plan = session.plan
    return build_parent_provision_evidence_recovery_attempt_receipt(
        recovery_session_id=session.recovery_session_id,
        recovery_plan_id=plan.recovery_plan_id,
        recovery_plan_sha256=plan.recovery_plan_sha256,
        attempt_ordinal=ordinal,
        strategy=strategy,
        feedback_sha256=feedback_sha256,
        state=state,
        outcome=outcome,
        failure_code=failure_code,
        input_stage_input_bundle_sha256=plan.original_stage_input_bundle_sha256,
        input_target_evidence_sha256=plan.original_target_evidence_sha256,
        new_stage_input_bundle_sha256=new_stage_input_bundle_sha256,
        new_target_evidence_sha256=new_target_evidence_sha256,
    )


def _recovery_receipt(
    session,  # type: ignore[no-untyped-def]
    *,
    attempts,
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    accepted=None,
    reason_codes: tuple[str, ...] = (),
):  # type: ignore[no-untyped-def]
    return build_parent_provision_evidence_recovery_receipt(
        session=session,
        attempts=attempts,
        state=state,
        outcome=outcome,
        accepted_attempt_id=accepted.attempt_id if accepted is not None else None,
        new_stage_input_bundle_sha256=(
            accepted.new_stage_input_bundle_sha256 if accepted is not None else None
        ),
        new_target_evidence_sha256=(
            accepted.new_target_evidence_sha256 if accepted is not None else None
        ),
        reason_codes=reason_codes,
    )


def _object_plans() -> tuple[
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectBatchPlanV1,
]:
    old_recovery = _target(
        order=0,
        logical_object_id=RECOVERY_OBJECT_ID,
        object_kind=D2LogicalObjectKind.ATTACHMENT,
        parent_logical_object_id=REUSABLE_OBJECT_ID,
        stage_input_bundle_sha256=OLD_STAGE_INPUT_SHA256,
        execution_mode=ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED,
        segment_bindings=(),
    )
    old_reusable = _target(
        order=1,
        logical_object_id=REUSABLE_OBJECT_ID,
        object_kind=D2LogicalObjectKind.SECTION,
        parent_logical_object_id=None,
        stage_input_bundle_sha256=OLD_STAGE_INPUT_SHA256,
        execution_mode=ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER,
        segment_bindings=(),
    )
    recovered_binding = ParentProvisionObjectSegmentBindingV1(
        segment_ordinal=0,
        segment_id="d2-source-segment-" + "c" * 32,
        source_text_length=16,
        source_text_sha256="d" * 64,
        d2_segment_sha256="e" * 64,
    )
    new_recovered = _target(
        order=0,
        logical_object_id=RECOVERY_OBJECT_ID,
        object_kind=D2LogicalObjectKind.ATTACHMENT,
        parent_logical_object_id=REUSABLE_OBJECT_ID,
        stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
        execution_mode=ParentProvisionObjectExecutionMode.MODEL_TARGET,
        segment_bindings=(recovered_binding,),
    )
    new_reusable = _target(
        order=1,
        logical_object_id=REUSABLE_OBJECT_ID,
        object_kind=D2LogicalObjectKind.SECTION,
        parent_logical_object_id=None,
        stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
        execution_mode=ParentProvisionObjectExecutionMode.HOST_EMPTY_CONTAINER,
        segment_bindings=(),
    )
    return (
        _object_plan(
            stage_input_bundle_sha256=OLD_STAGE_INPUT_SHA256,
            targets=(old_recovery, old_reusable),
        ),
        _object_plan(
            stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
            targets=(new_recovered, new_reusable),
        ),
    )


def _target(
    *,
    order: int,
    logical_object_id: str,
    object_kind: D2LogicalObjectKind,
    parent_logical_object_id: str | None,
    stage_input_bundle_sha256: str,
    execution_mode: ParentProvisionObjectExecutionMode,
    segment_bindings: tuple[ParentProvisionObjectSegmentBindingV1, ...],
) -> ParentProvisionObjectBatchTargetV1:
    evidence_sha256 = _target_evidence_sha256(
        object_kind=object_kind,
        parent_logical_object_id=parent_logical_object_id,
        segment_bindings=segment_bindings,
    )
    scope_sha256 = _target_scope_sha256(
        batch_order=order,
        logical_object_id=logical_object_id,
        stage_input_bundle_sha256=stage_input_bundle_sha256,
        global_source_scope_sha256=SOURCE_SCOPE_SHA256,
        target_evidence_sha256=evidence_sha256,
        execution_mode=execution_mode,
    )
    return ParentProvisionObjectBatchTargetV1(
        batch_id="parent-provision-object-batch-" + scope_sha256[:32],
        batch_order=order,
        logical_object_id=logical_object_id,
        object_kind=object_kind,
        parent_logical_object_id=parent_logical_object_id,
        stage_input_bundle_sha256=stage_input_bundle_sha256,
        global_source_scope_sha256=SOURCE_SCOPE_SHA256,
        segment_ordinals=tuple(item.segment_ordinal for item in segment_bindings),
        segment_bindings=segment_bindings,
        execution_mode=execution_mode,
        target_evidence_sha256=evidence_sha256,
        target_scope_sha256=scope_sha256,
    )


def _object_plan(
    *,
    stage_input_bundle_sha256: str,
    targets: tuple[ParentProvisionObjectBatchTargetV1, ...],
) -> ParentProvisionObjectBatchPlanV1:
    payload = {
        "run_id": RUN_ID,
        "source_sha256": SOURCE_SHA256,
        "kit_bundle_sha256": KIT_SHA256,
        "predecessor_artifact_id": PREDECESSOR_ID,
        "predecessor_artifact_sha256": PREDECESSOR_SHA256,
        "predecessor_validator_receipt_sha256": PREDECESSOR_RECEIPT_SHA256,
        "source_scope_sha256": SOURCE_SCOPE_SHA256,
        "stage_input_bundle_sha256": stage_input_bundle_sha256,
        "segment_ordinal_ledger_sha256": canonical_json_sha256(
            tuple(
                binding.model_dump(mode="json")
                for target in targets
                for binding in target.segment_bindings
            )
        ),
        "targets": targets,
    }
    draft = ParentProvisionObjectBatchPlanV1.model_construct(
        plan_id="parent-provision-object-plan-" + "0" * 32,
        plan_sha256="0" * 64,
        **payload,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"plan_id", "plan_sha256"})
    )
    return ParentProvisionObjectBatchPlanV1(
        plan_id="parent-provision-object-plan-" + digest[:32],
        plan_sha256=digest,
        **payload,
    )


if __name__ == "__main__":
    unittest.main()
