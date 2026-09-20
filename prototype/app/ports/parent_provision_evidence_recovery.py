from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Callable, Protocol, runtime_checkable

from app.application.agent_workflow.parent_provision_evidence_recovery import (
    ParentProvisionEvidenceRecoveryOutcome,
    ParentProvisionEvidenceRecoverySessionV1,
    ParentProvisionEvidenceRecoveryState,
    ParentProvisionEvidenceRecoveryStrategy,
    stable_parent_provision_evidence_recovery_attempt_id,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
    ParentProvisionObjectBatchPlanV1,
)


_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
_LOGICAL_OBJECT_PATTERN = re.compile(
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
)


class ParentProvisionEvidenceRecoveryExecutorError(RuntimeError):
    """Stable, content-free failure raised by a recovery executor adapter."""

    def __init__(self, reason_code: str) -> None:
        if _MACHINE_CODE_PATTERN.fullmatch(reason_code) is None:
            raise ValueError("recovery executor reason must be a machine code")
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True, slots=True)
class ParentProvisionEvidenceRecoveryExecutionTargetV1:
    """Exact source/workspace target authorized for one recovery session."""

    source_handle: str
    workspace_bundle_sha256: str
    logical_object_id: str
    page_numbers: tuple[int, ...]
    table_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_handle.strip() or len(self.source_handle) > 240:
            raise ValueError("recovery source handle must be bounded and non-empty")
        _require_sha256(self.workspace_bundle_sha256, subject="recovery workspace")
        if _LOGICAL_OBJECT_PATTERN.fullmatch(self.logical_object_id) is None:
            raise ValueError("recovery logical object identity is invalid")
        if (
            not self.page_numbers
            or self.page_numbers != tuple(sorted(set(self.page_numbers)))
            or any(
                isinstance(page_number, bool) or page_number < 1
                for page_number in self.page_numbers
            )
        ):
            raise ValueError("recovery target pages must be positive and sorted")
        _require_distinct_strings(self.table_ids, subject="recovery table IDs")
        _require_distinct_strings(self.asset_ids, subject="recovery asset IDs")

    @property
    def target_binding_sha256(self) -> str:
        return _canonical_sha256(
            {
                "source_handle": self.source_handle,
                "workspace_bundle_sha256": self.workspace_bundle_sha256,
                "logical_object_id": self.logical_object_id,
                "page_numbers": self.page_numbers,
                "table_ids": self.table_ids,
                "asset_ids": self.asset_ids,
            }
        )


@dataclass(frozen=True, slots=True)
class ParentProvisionEvidenceRecoveryExecutionRequestV1:
    """Content-free command sent to one evidence recovery executor."""

    session: ParentProvisionEvidenceRecoverySessionV1
    target: ParentProvisionEvidenceRecoveryExecutionTargetV1
    attempt_id: str
    attempt_ordinal: int
    strategy: ParentProvisionEvidenceRecoveryStrategy
    feedback_sha256: str | None

    def __post_init__(self) -> None:
        plan = self.session.plan
        if (
            isinstance(self.attempt_ordinal, bool)
            or not 1 <= self.attempt_ordinal <= plan.max_attempts
        ):
            raise ValueError("recovery execution attempt ordinal is invalid")
        if plan.ordered_strategies[self.attempt_ordinal - 1] is not self.strategy:
            raise ValueError("recovery execution strategy drifted from its plan")
        if self.target.logical_object_id != plan.logical_object_id:
            raise ValueError("recovery execution target drifted from its plan")
        if (
            self.target.workspace_bundle_sha256
            != plan.original_stage_input_bundle_sha256
        ):
            raise ValueError("recovery execution workspace drifted from its plan")
        if self.feedback_sha256 is not None:
            _require_sha256(self.feedback_sha256, subject="recovery feedback")
        if (self.attempt_ordinal == 1) != (self.feedback_sha256 is None):
            raise ValueError("only the first recovery attempt may omit feedback")
        expected_attempt_id = stable_parent_provision_evidence_recovery_attempt_id(
            recovery_session_id=self.session.recovery_session_id,
            recovery_plan_sha256=plan.recovery_plan_sha256,
            attempt_ordinal=self.attempt_ordinal,
            strategy=self.strategy,
            feedback_sha256=self.feedback_sha256,
        )
        if self.attempt_id != expected_attempt_id:
            raise ValueError("recovery execution attempt identity drifted")

    @property
    def request_sha256(self) -> str:
        return _canonical_sha256(
            {
                "recovery_session_id": self.session.recovery_session_id,
                "recovery_plan_id": self.session.plan.recovery_plan_id,
                "recovery_plan_sha256": self.session.plan.recovery_plan_sha256,
                "target_binding_sha256": self.target.target_binding_sha256,
                "attempt_id": self.attempt_id,
                "attempt_ordinal": self.attempt_ordinal,
                "strategy": self.strategy.value,
                "feedback_sha256": self.feedback_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class ParentProvisionEvidenceRecoveryExecutionResultV1:
    """Content-free terminal result returned by a recovery executor."""

    request_sha256: str
    attempt_id: str
    strategy: ParentProvisionEvidenceRecoveryStrategy
    state: ParentProvisionEvidenceRecoveryState
    outcome: ParentProvisionEvidenceRecoveryOutcome
    failure_code: str | None = None
    new_stage_input_bundle_sha256: str | None = None
    new_target_evidence_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_sha256(self.request_sha256, subject="recovery execution request")
        if (
            re.fullmatch(
                r"parent-provision-evidence-recovery-attempt-[0-9a-f]{32}",
                self.attempt_id,
            )
            is None
        ):
            raise ValueError("recovery execution result attempt identity is invalid")
        allowed = {
            ParentProvisionEvidenceRecoveryState.SUCCEEDED: {
                ParentProvisionEvidenceRecoveryOutcome.RECOVERED
            },
            ParentProvisionEvidenceRecoveryState.FAILED: {
                ParentProvisionEvidenceRecoveryOutcome.FAILED
            },
            ParentProvisionEvidenceRecoveryState.CANCELLED: {
                ParentProvisionEvidenceRecoveryOutcome.CANCELLED
            },
            ParentProvisionEvidenceRecoveryState.BLOCKED: {
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
                ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
            },
            ParentProvisionEvidenceRecoveryState.INDETERMINATE: {
                ParentProvisionEvidenceRecoveryOutcome.INDETERMINATE
            },
        }
        if self.state not in allowed or self.outcome not in allowed[self.state]:
            raise ValueError("recovery execution result state and outcome disagree")
        if self.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED:
            if (
                self.failure_code is not None
                or self.new_stage_input_bundle_sha256 is None
                or self.new_target_evidence_sha256 is None
            ):
                raise ValueError("recovered execution must bind its new evidence")
            _require_sha256(
                self.new_stage_input_bundle_sha256,
                subject="recovered stage input",
            )
            _require_sha256(
                self.new_target_evidence_sha256,
                subject="recovered target evidence",
            )
        else:
            if (
                self.failure_code is None
                or _MACHINE_CODE_PATTERN.fullmatch(self.failure_code) is None
                or self.new_stage_input_bundle_sha256 is not None
                or self.new_target_evidence_sha256 is not None
            ):
                raise ValueError(
                    "non-recovered execution requires one machine failure only"
                )

    @property
    def result_sha256(self) -> str:
        return _canonical_sha256(
            {
                "request_sha256": self.request_sha256,
                "attempt_id": self.attempt_id,
                "strategy": self.strategy.value,
                "state": self.state.value,
                "outcome": self.outcome.value,
                "failure_code": self.failure_code,
                "new_stage_input_bundle_sha256": (self.new_stage_input_bundle_sha256),
                "new_target_evidence_sha256": self.new_target_evidence_sha256,
            }
        )


@runtime_checkable
class ParentProvisionEvidenceRecoveryExecutorPort(Protocol):
    def execute(
        self,
        request: ParentProvisionEvidenceRecoveryExecutionRequestV1,
        *,
        cancellation_requested: Callable[[], bool],
    ) -> ParentProvisionEvidenceRecoveryExecutionResultV1: ...


@runtime_checkable
class ParentProvisionEvidenceRecoveryReplannerPort(Protocol):
    def rebuild_object_plan(
        self,
        *,
        old_plan: ParentProvisionObjectBatchPlanV1,
        session: ParentProvisionEvidenceRecoverySessionV1,
        execution_result: ParentProvisionEvidenceRecoveryExecutionResultV1,
    ) -> ParentProvisionObjectBatchPlanV1: ...


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _require_sha256(value: str, *, subject: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{subject} SHA-256 is invalid")


def _require_distinct_strings(value: tuple[str, ...], *, subject: str) -> None:
    if value != tuple(sorted(set(value))) or any(
        not item.strip() or len(item) > 200 for item in value
    ):
        raise ValueError(f"{subject} must be sorted, unique, and bounded")


__all__ = [
    "ParentProvisionEvidenceRecoveryExecutionRequestV1",
    "ParentProvisionEvidenceRecoveryExecutionResultV1",
    "ParentProvisionEvidenceRecoveryExecutionTargetV1",
    "ParentProvisionEvidenceRecoveryExecutorError",
    "ParentProvisionEvidenceRecoveryExecutorPort",
    "ParentProvisionEvidenceRecoveryReplannerPort",
]
