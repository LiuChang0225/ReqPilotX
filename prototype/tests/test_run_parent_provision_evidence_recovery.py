from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from dataclasses import dataclass
import unittest

from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceGap,
    ParentProvisionEvidenceRecoverability,
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
    build_parent_provision_evidence_recovery_session,
    stable_parent_provision_evidence_recovery_feedback_sha256,
)
from app.application.agent_workflow.run_parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryOrchestrator,
)
from app.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ParentProvisionEvidenceRecoveryExecutionResultV1,
    ParentProvisionEvidenceRecoveryExecutionTargetV1,
)

if __package__:
    from .test_parent_provision_evidence_recovery import (
        NEW_STAGE_INPUT_SHA256,
        RECOVERY_OBJECT_ID,
        _object_plans,
        _recovery_plan,
    )
else:
    from test_parent_provision_evidence_recovery import (
        NEW_STAGE_INPUT_SHA256,
        RECOVERY_OBJECT_ID,
        _object_plans,
        _recovery_plan,
    )


class _ScriptedExecutor:
    def __init__(self, script):  # type: ignore[no-untyped-def]
        self.script = list(script)
        self.requests: list[ParentProvisionEvidenceRecoveryExecutionRequestV1] = []

    def execute(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
        *,
        cancellation_requested,  # type: ignore[no-untyped-def]
    ) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
        self.requests.append(request)
        return self.script[len(self.requests) - 1](request)


@dataclass
class _StaticReplanner:
    new_plan: object
    call_count: int = 0

    def rebuild_object_plan(self, **_kwargs):  # type: ignore[no-untyped-def]
        self.call_count += 1
        return self.new_plan


class ParentProvisionEvidenceRecoveryOrchestratorTests(unittest.TestCase):
    def test_failed_strategy_advances_with_feedback_then_replans_success(self) -> None:
        old_plan, new_plan = _object_plans()
        session = build_parent_provision_evidence_recovery_session(
            plan=_recovery_plan(old_plan)
        )
        executor = _ScriptedExecutor(
            (
                lambda request: _failure(request, "NATIVE_TEXT_STILL_EMPTY"),
                lambda request: _success(
                    request,
                    new_target_evidence_sha256=(
                        new_plan.targets[0].target_evidence_sha256
                    ),
                ),
            )
        )
        replanner = _StaticReplanner(new_plan)
        result = ParentProvisionEvidenceRecoveryOrchestrator(
            executor=executor,
            replanner=replanner,
        ).run(
            old_plan=old_plan,
            session=session,
            target=_target(old_plan),
        )

        self.assertEqual(len(executor.requests), 2)
        self.assertEqual(
            executor.requests[1].feedback_sha256,
            stable_parent_provision_evidence_recovery_feedback_sha256(
                previous_attempt=result.recovery_receipt.attempts[0]
            ),
        )
        self.assertEqual(
            tuple(item.strategy for item in result.recovery_receipt.attempts),
            session.plan.ordered_strategies,
        )
        self.assertEqual(
            result.recovery_receipt.outcome,
            ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
        )
        self.assertEqual(replanner.call_count, 1)
        self.assertEqual(
            result.replan_receipt.invalidated_logical_object_ids,
            (RECOVERY_OBJECT_ID,),
        )

    def test_all_distinct_strategies_fail_to_exhausted_without_semantic_output(
        self,
    ) -> None:
        old_plan, new_plan = _object_plans()
        session = build_parent_provision_evidence_recovery_session(
            plan=_recovery_plan(old_plan)
        )
        executor = _ScriptedExecutor(
            (
                lambda request: _failure(request, "NATIVE_TEXT_STILL_EMPTY"),
                lambda request: _failure(request, "TARGET_EVIDENCE_UNCHANGED"),
            )
        )
        result = ParentProvisionEvidenceRecoveryOrchestrator(
            executor=executor,
            replanner=_StaticReplanner(new_plan),
        ).run(
            old_plan=old_plan,
            session=session,
            target=_target(old_plan),
        )

        self.assertEqual(
            result.recovery_receipt.state,
            ParentProvisionEvidenceRecoveryState.EXHAUSTED,
        )
        self.assertEqual(
            result.recovery_receipt.reason_codes,
            ("ATTEMPT_BUDGET_EXHAUSTED",),
        )
        self.assertIsNone(result.new_plan)
        self.assertFalse(
            hasattr(result.recovery_receipt, "semantic_unresolved_scope_count")
        )

    def test_cancel_blocked_and_indeterminate_stop_only_this_session(self) -> None:
        old_plan, new_plan = _object_plans()
        session = build_parent_provision_evidence_recovery_session(
            plan=_recovery_plan(old_plan)
        )

        cancelled_executor = _ScriptedExecutor(())
        cancelled = ParentProvisionEvidenceRecoveryOrchestrator(
            executor=cancelled_executor,
            replanner=_StaticReplanner(new_plan),
        ).run(
            old_plan=old_plan,
            session=session,
            target=_target(old_plan),
            cancellation_requested=lambda: True,
        )
        self.assertEqual(cancelled.recovery_receipt.attempts, ())
        self.assertEqual(
            cancelled.recovery_receipt.state,
            ParentProvisionEvidenceRecoveryState.CANCELLED,
        )
        self.assertEqual(cancelled_executor.requests, [])

        for state, outcome, code in (
            (
                ParentProvisionEvidenceRecoveryState.BLOCKED,
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
                "RECOVERY_RUNTIME_BLOCKED",
            ),
            (
                ParentProvisionEvidenceRecoveryState.INDETERMINATE,
                ParentProvisionEvidenceRecoveryOutcome.INDETERMINATE,
                "RECOVERY_RUNTIME_INDETERMINATE",
            ),
        ):
            with self.subTest(state=state):
                executor = _ScriptedExecutor(
                    (
                        lambda request, state=state, outcome=outcome, code=code: (
                            _terminal(request, state, outcome, code)
                        ),
                    )
                )
                observed = ParentProvisionEvidenceRecoveryOrchestrator(
                    executor=executor,
                    replanner=_StaticReplanner(new_plan),
                ).run(
                    old_plan=old_plan,
                    session=session,
                    target=_target(old_plan),
                )
                self.assertEqual(observed.recovery_receipt.state, state)
                self.assertEqual(len(observed.recovery_receipt.attempts), 1)

    def test_external_integrity_corrupt_and_unsupported_never_call_executor(
        self,
    ) -> None:
        old_plan, new_plan = _object_plans()
        cases = (
            (
                ParentProvisionEvidenceGap.INTEGRITY_DRIFT,
                ParentProvisionEvidenceRecoverability.INTEGRITY_REVIEW_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.STOP_FOR_INTEGRITY_REVIEW,
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
            ),
            (
                ParentProvisionEvidenceGap.EXTERNAL_SOURCE_MISSING,
                ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.REQUEST_EXTERNAL_SOURCE,
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
            ),
            (
                ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED,
                ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED,
                ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
            ),
            (
                ParentProvisionEvidenceGap.UNSUPPORTED_EVIDENCE_FORMAT,
                ParentProvisionEvidenceRecoverability.NOT_RECOVERABLE,
                ParentProvisionEvidenceRecoveryStrategy.STOP_UNSUPPORTED_FORMAT,
                ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
            ),
        )
        for gap, recoverability, strategy, outcome in cases:
            with self.subTest(gap=gap):
                session = build_parent_provision_evidence_recovery_session(
                    plan=_recovery_plan(
                        old_plan,
                        gap=gap,
                        recoverability=recoverability,
                        strategies=(strategy,),
                        max_attempts=1,
                    )
                )
                executor = _ScriptedExecutor(())
                result = ParentProvisionEvidenceRecoveryOrchestrator(
                    executor=executor,
                    replanner=_StaticReplanner(new_plan),
                ).run(
                    old_plan=old_plan,
                    session=session,
                    target=_target(old_plan),
                )
                self.assertEqual(executor.requests, [])
                self.assertEqual(result.recovery_receipt.attempts, ())
                self.assertEqual(result.recovery_receipt.outcome, outcome)

    def test_unchanged_stage_bundle_claim_becomes_indeterminate(self) -> None:
        old_plan, new_plan = _object_plans()
        session = build_parent_provision_evidence_recovery_session(
            plan=_recovery_plan(old_plan)
        )
        executor = _ScriptedExecutor(
            (
                lambda request: ParentProvisionEvidenceRecoveryExecutionResultV1(
                    request_sha256=request.request_sha256,
                    attempt_id=request.attempt_id,
                    strategy=request.strategy,
                    state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                    new_stage_input_bundle_sha256=(
                        request.session.plan.original_stage_input_bundle_sha256
                    ),
                    new_target_evidence_sha256=(
                        request.session.plan.original_target_evidence_sha256
                    ),
                ),
            )
        )
        result = ParentProvisionEvidenceRecoveryOrchestrator(
            executor=executor,
            replanner=_StaticReplanner(new_plan),
        ).run(
            old_plan=old_plan,
            session=session,
            target=_target(old_plan),
        )

        self.assertEqual(
            result.recovery_receipt.state,
            ParentProvisionEvidenceRecoveryState.INDETERMINATE,
        )
        self.assertEqual(
            result.recovery_receipt.reason_codes,
            ("RECOVERY_EXECUTOR_RESULT_MISMATCH",),
        )


def _target(old_plan):  # type: ignore[no-untyped-def]
    return ParentProvisionEvidenceRecoveryExecutionTargetV1(
        source_handle="source-handle-" + "f" * 32,
        workspace_bundle_sha256=old_plan.stage_input_bundle_sha256,
        logical_object_id=RECOVERY_OBJECT_ID,
        page_numbers=(1,),
    )


def _failure(
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    code: str,
) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
    return _terminal(
        request,
        ParentProvisionEvidenceRecoveryState.FAILED,
        ParentProvisionEvidenceRecoveryOutcome.FAILED,
        code,
    )


def _terminal(
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    code: str,
) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
    return ParentProvisionEvidenceRecoveryExecutionResultV1(
        request_sha256=request.request_sha256,
        attempt_id=request.attempt_id,
        strategy=request.strategy,
        state=state,
        outcome=outcome,
        failure_code=code,
    )


def _success(
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    *,
    new_target_evidence_sha256: str,
) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
    return ParentProvisionEvidenceRecoveryExecutionResultV1(
        request_sha256=request.request_sha256,
        attempt_id=request.attempt_id,
        strategy=request.strategy,
        state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
        outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
        new_stage_input_bundle_sha256=NEW_STAGE_INPUT_SHA256,
        new_target_evidence_sha256=new_target_evidence_sha256,
    )


if __name__ == "__main__":
    unittest.main()
