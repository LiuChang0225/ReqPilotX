from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from reqpilotx_server.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryAttemptReceiptV1,
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoveryReceiptV1,
    ParentProvisionEvidenceRecoverySessionV1,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
    ParentProvisionEvidenceReplanReceiptV1,
    build_parent_provision_evidence_recovery_attempt_receipt,
    build_parent_provision_evidence_recovery_receipt,
    build_parent_provision_evidence_replan_receipt,
    stable_parent_provision_evidence_recovery_attempt_id,
    stable_parent_provision_evidence_recovery_feedback_sha256,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectExecutionMode,
)
from reqpilotx_server.ports.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryExecutionRequestV1,
    ParentProvisionEvidenceRecoveryExecutionResultV1,
    ParentProvisionEvidenceRecoveryExecutionTargetV1,
    ParentProvisionEvidenceRecoveryExecutorError,
    ParentProvisionEvidenceRecoveryExecutorPort,
    ParentProvisionEvidenceRecoveryReplannerPort,
)


_NON_EXECUTOR_STRATEGIES = {
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_USER_EVIDENCE,
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_EXTERNAL_SOURCE,
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
    ParentProvisionEvidenceRecoveryStrategy.STOP_FOR_INTEGRITY_REVIEW,
    ParentProvisionEvidenceRecoveryStrategy.STOP_UNSUPPORTED_FORMAT,
}

_NON_EXECUTOR_TERMINALS = {
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_USER_EVIDENCE: (
        ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
        "USER_EVIDENCE_REQUIRED",
    ),
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_EXTERNAL_SOURCE: (
        ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
        "EXTERNAL_SOURCE_REQUIRED",
    ),
    ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE: (
        ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
        "REPLACEMENT_SOURCE_REQUIRED",
    ),
    ParentProvisionEvidenceRecoveryStrategy.STOP_FOR_INTEGRITY_REVIEW: (
        ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
        "INTEGRITY_REVIEW_REQUIRED",
    ),
    ParentProvisionEvidenceRecoveryStrategy.STOP_UNSUPPORTED_FORMAT: (
        ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
        "UNSUPPORTED_EVIDENCE_FORMAT",
    ),
}


class ParentProvisionEvidenceRecoveryOrchestrationError(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ParentProvisionEvidenceRecoveryRunResult:
    old_plan: ParentProvisionObjectBatchPlanV1
    recovery_receipt: ParentProvisionEvidenceRecoveryReceiptV1
    new_plan: ParentProvisionObjectBatchPlanV1 | None = None
    replan_receipt: ParentProvisionEvidenceReplanReceiptV1 | None = None

    def __post_init__(self) -> None:
        recovered = (
            self.recovery_receipt.outcome
            is ParentProvisionEvidenceRecoveryOutcome.RECOVERED
        )
        if recovered != (self.new_plan is not None and self.replan_receipt is not None):
            raise ValueError("recovery run result must close successful replanning")
        if not recovered and (
            self.new_plan is not None or self.replan_receipt is not None
        ):
            raise ValueError("failed recovery cannot expose a replacement plan")


class ParentProvisionEvidenceRecoveryOrchestrator:
    """Execute finite object-level evidence recovery before semantic judgment."""

    def __init__(
        self,
        *,
        executor: ParentProvisionEvidenceRecoveryExecutorPort,
        replanner: ParentProvisionEvidenceRecoveryReplannerPort,
    ) -> None:
        self._executor = executor
        self._replanner = replanner

    def run(
        self,
        *,
        old_plan: ParentProvisionObjectBatchPlanV1,
        session: ParentProvisionEvidenceRecoverySessionV1,
        target: ParentProvisionEvidenceRecoveryExecutionTargetV1,
        cancellation_requested: Callable[[], bool] = lambda: False,
    ) -> ParentProvisionEvidenceRecoveryRunResult:
        plan = session.plan
        _require_run_lineage(old_plan=old_plan, session=session, target=target)
        attempts: list[ParentProvisionEvidenceRecoveryAttemptReceiptV1] = []

        for attempt_ordinal, strategy in enumerate(
            plan.ordered_strategies,
            start=1,
        ):
            if cancellation_requested():
                return _terminal_result(
                    old_plan=old_plan,
                    session=session,
                    attempts=tuple(attempts),
                    state=ParentProvisionEvidenceRecoveryState.CANCELLED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.CANCELLED,
                    reason_codes=("RECOVERY_CANCELLED",),
                )
            if strategy in _NON_EXECUTOR_STRATEGIES:
                outcome, reason_code = _NON_EXECUTOR_TERMINALS[strategy]
                return _terminal_result(
                    old_plan=old_plan,
                    session=session,
                    attempts=tuple(attempts),
                    state=ParentProvisionEvidenceRecoveryState.BLOCKED,
                    outcome=outcome,
                    reason_codes=(reason_code,),
                )

            feedback_sha256 = (
                None
                if not attempts
                else stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=attempts[-1]
                )
            )
            attempt_id = stable_parent_provision_evidence_recovery_attempt_id(
                recovery_session_id=session.recovery_session_id,
                recovery_plan_sha256=plan.recovery_plan_sha256,
                attempt_ordinal=attempt_ordinal,
                strategy=strategy,
                feedback_sha256=feedback_sha256,
            )
            request = ParentProvisionEvidenceRecoveryExecutionRequestV1(
                session=session,
                target=target,
                attempt_id=attempt_id,
                attempt_ordinal=attempt_ordinal,
                strategy=strategy,
                feedback_sha256=feedback_sha256,
            )
            execution_result = self._execute(
                request,
                cancellation_requested=cancellation_requested,
            )
            attempt = _attempt_receipt(
                request=request,
                result=execution_result,
            )
            attempts.append(attempt)

            if (
                execution_result.outcome
                is ParentProvisionEvidenceRecoveryOutcome.FAILED
            ):
                continue
            if (
                execution_result.outcome
                is ParentProvisionEvidenceRecoveryOutcome.RECOVERED
            ):
                recovery_receipt = build_parent_provision_evidence_recovery_receipt(
                    session=session,
                    attempts=tuple(attempts),
                    state=ParentProvisionEvidenceRecoveryState.SUCCEEDED,
                    outcome=ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                    accepted_attempt_id=attempt.attempt_id,
                    new_stage_input_bundle_sha256=(
                        attempt.new_stage_input_bundle_sha256
                    ),
                    new_target_evidence_sha256=(attempt.new_target_evidence_sha256),
                    reason_codes=(),
                )
                try:
                    new_plan = self._replanner.rebuild_object_plan(
                        old_plan=old_plan,
                        session=session,
                        execution_result=execution_result,
                    )
                    replan_receipt = build_parent_provision_evidence_replan_receipt(
                        recovery_receipt=recovery_receipt,
                        old_plan=old_plan,
                        new_plan=new_plan,
                    )
                except Exception as exc:
                    raise ParentProvisionEvidenceRecoveryOrchestrationError(
                        "RECOVERY_REPLAN_FAILED"
                    ) from exc
                return ParentProvisionEvidenceRecoveryRunResult(
                    old_plan=old_plan,
                    recovery_receipt=recovery_receipt,
                    new_plan=new_plan,
                    replan_receipt=replan_receipt,
                )

            return _terminal_result(
                old_plan=old_plan,
                session=session,
                attempts=tuple(attempts),
                state=execution_result.state,
                outcome=execution_result.outcome,
                reason_codes=(execution_result.failure_code or "RECOVERY_FAILED",),
            )

        return _terminal_result(
            old_plan=old_plan,
            session=session,
            attempts=tuple(attempts),
            state=ParentProvisionEvidenceRecoveryState.EXHAUSTED,
            outcome=ParentProvisionEvidenceRecoveryOutcome.FAILED,
            reason_codes=("ATTEMPT_BUDGET_EXHAUSTED",),
        )

    def _execute(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
        try:
            result = self._executor.execute(
                request,
                cancellation_requested=cancellation_requested,
            )
        except ParentProvisionEvidenceRecoveryExecutorError as exc:
            return _indeterminate_result(request, failure_code=exc.reason_code)
        except Exception:
            return _indeterminate_result(
                request,
                failure_code="RECOVERY_EXECUTOR_UNHANDLED_EXCEPTION",
            )
        if (
            result.request_sha256 != request.request_sha256
            or result.attempt_id != request.attempt_id
            or result.strategy is not request.strategy
            or (
                result.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED
                and result.new_stage_input_bundle_sha256
                == request.session.plan.original_stage_input_bundle_sha256
            )
        ):
            return _indeterminate_result(
                request,
                failure_code="RECOVERY_EXECUTOR_RESULT_MISMATCH",
            )
        return result


def _attempt_receipt(
    *,
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    result: ParentProvisionEvidenceRecoveryExecutionResultV1,
) -> ParentProvisionEvidenceRecoveryAttemptReceiptV1:
    plan = request.session.plan
    return build_parent_provision_evidence_recovery_attempt_receipt(
        recovery_session_id=request.session.recovery_session_id,
        recovery_plan_id=plan.recovery_plan_id,
        recovery_plan_sha256=plan.recovery_plan_sha256,
        attempt_ordinal=request.attempt_ordinal,
        strategy=request.strategy,
        feedback_sha256=request.feedback_sha256,
        state=result.state,
        outcome=result.outcome,
        failure_code=result.failure_code,
        input_stage_input_bundle_sha256=(plan.original_stage_input_bundle_sha256),
        input_target_evidence_sha256=plan.original_target_evidence_sha256,
        new_stage_input_bundle_sha256=result.new_stage_input_bundle_sha256,
        new_target_evidence_sha256=result.new_target_evidence_sha256,
    )


def _indeterminate_result(
    request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
    *,
    failure_code: str,
) -> ParentProvisionEvidenceRecoveryExecutionResultV1:
    return ParentProvisionEvidenceRecoveryExecutionResultV1(
        request_sha256=request.request_sha256,
        attempt_id=request.attempt_id,
        strategy=request.strategy,
        state=ParentProvisionEvidenceRecoveryState.INDETERMINATE,
        outcome=ParentProvisionEvidenceRecoveryOutcome.INDETERMINATE,
        failure_code=failure_code,
    )


def _terminal_result(
    *,
    old_plan: ParentProvisionObjectBatchPlanV1,
    session: ParentProvisionEvidenceRecoverySessionV1,
    attempts: tuple[ParentProvisionEvidenceRecoveryAttemptReceiptV1, ...],
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    reason_codes: tuple[str, ...],
) -> ParentProvisionEvidenceRecoveryRunResult:
    receipt = build_parent_provision_evidence_recovery_receipt(
        session=session,
        attempts=attempts,
        state=state,
        outcome=outcome,
        reason_codes=tuple(sorted(set(reason_codes))),
    )
    return ParentProvisionEvidenceRecoveryRunResult(
        old_plan=old_plan,
        recovery_receipt=receipt,
    )


def _require_run_lineage(
    *,
    old_plan: ParentProvisionObjectBatchPlanV1,
    session: ParentProvisionEvidenceRecoverySessionV1,
    target: ParentProvisionEvidenceRecoveryExecutionTargetV1,
) -> None:
    recovery_plan = session.plan
    object_target = next(
        (
            item
            for item in old_plan.targets
            if item.logical_object_id == recovery_plan.logical_object_id
        ),
        None,
    )
    if (
        old_plan.plan_id != recovery_plan.object_plan_id
        or old_plan.plan_sha256 != recovery_plan.object_plan_sha256
        or old_plan.stage_input_bundle_sha256
        != recovery_plan.original_stage_input_bundle_sha256
        or target.workspace_bundle_sha256 != old_plan.stage_input_bundle_sha256
        or target.logical_object_id != recovery_plan.logical_object_id
        or object_target is None
        or object_target.batch_id != recovery_plan.batch_id
        or object_target.target_scope_sha256 != recovery_plan.target_scope_sha256
        or object_target.target_evidence_sha256
        != recovery_plan.original_target_evidence_sha256
        or object_target.execution_mode
        is not ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
    ):
        raise ParentProvisionEvidenceRecoveryOrchestrationError(
            "RECOVERY_RUN_LINEAGE_MISMATCH"
        )


__all__ = [
    "ParentProvisionEvidenceRecoveryOrchestrationError",
    "ParentProvisionEvidenceRecoveryOrchestrator",
    "ParentProvisionEvidenceRecoveryRunResult",
]
