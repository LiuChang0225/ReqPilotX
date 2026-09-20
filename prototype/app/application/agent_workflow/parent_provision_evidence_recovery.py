"""Immutable evidence-recovery contracts for parent-provision object targets."""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
    ParentProvisionObjectBatchPlanV1,
    ParentProvisionObjectExecutionMode,
)


PARENT_PROVISION_EVIDENCE_RECOVERY_PLAN_SCHEMA_VERSION = (
    "parent-provision-evidence-recovery-plan/v1"
)
PARENT_PROVISION_EVIDENCE_RECOVERY_ATTEMPT_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-evidence-recovery-attempt-receipt/v1"
)
PARENT_PROVISION_EVIDENCE_RECOVERY_SESSION_SCHEMA_VERSION = (
    "parent-provision-evidence-recovery-session/v1"
)
PARENT_PROVISION_EVIDENCE_RECOVERY_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-evidence-recovery-receipt/v1"
)
PARENT_PROVISION_EVIDENCE_REPLAN_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-evidence-replan-receipt/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
_LOGICAL_OBJECT_PATTERN = re.compile(
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionEvidenceGap(StrEnum):
    SCANNED_OR_NO_NATIVE_TEXT = "SCANNED_OR_NO_NATIVE_TEXT"
    MISSING_PAGE_RENDER = "MISSING_PAGE_RENDER"
    MISSING_TABLE_EVIDENCE = "MISSING_TABLE_EVIDENCE"
    MISSING_FIGURE_EVIDENCE = "MISSING_FIGURE_EVIDENCE"
    MISSING_ASSET_PLACEMENT = "MISSING_ASSET_PLACEMENT"
    EXISTING_EVIDENCE_UNBOUND = "EXISTING_EVIDENCE_UNBOUND"
    AMBIGUOUS_OBJECT_EVIDENCE = "AMBIGUOUS_OBJECT_EVIDENCE"
    INTEGRITY_DRIFT = "INTEGRITY_DRIFT"
    EXTERNAL_SOURCE_MISSING = "EXTERNAL_SOURCE_MISSING"
    SOURCE_CORRUPTED_OR_ENCRYPTED = "SOURCE_CORRUPTED_OR_ENCRYPTED"
    UNSUPPORTED_EVIDENCE_FORMAT = "UNSUPPORTED_EVIDENCE_FORMAT"


class ParentProvisionEvidenceRecoverability(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    ASSISTED = "ASSISTED"
    EXTERNAL_INPUT_REQUIRED = "EXTERNAL_INPUT_REQUIRED"
    INTEGRITY_REVIEW_REQUIRED = "INTEGRITY_REVIEW_REQUIRED"
    NOT_RECOVERABLE = "NOT_RECOVERABLE"


class ParentProvisionEvidenceRecoveryStrategy(StrEnum):
    RECAPTURE_NATIVE_TEXT = "RECAPTURE_NATIVE_TEXT"
    AUTHORIZED_VISUAL_TEXT_OCR = "AUTHORIZED_VISUAL_TEXT_OCR"
    RERENDER_SOURCE_PAGES = "RERENDER_SOURCE_PAGES"
    REBUILD_TABLE_EVIDENCE = "REBUILD_TABLE_EVIDENCE"
    VISUAL_RECONSTRUCT_TABLE = "VISUAL_RECONSTRUCT_TABLE"
    RECONSTRUCT_FIGURE_EVIDENCE = "RECONSTRUCT_FIGURE_EVIDENCE"
    REBUILD_ASSET_PLACEMENTS = "REBUILD_ASSET_PLACEMENTS"
    REBIND_EXISTING_EVIDENCE = "REBIND_EXISTING_EVIDENCE"
    RECOMPUTE_TARGET_EVIDENCE = "RECOMPUTE_TARGET_EVIDENCE"
    REQUEST_USER_EVIDENCE = "REQUEST_USER_EVIDENCE"
    REQUEST_EXTERNAL_SOURCE = "REQUEST_EXTERNAL_SOURCE"
    REQUEST_REPLACEMENT_SOURCE = "REQUEST_REPLACEMENT_SOURCE"
    STOP_FOR_INTEGRITY_REVIEW = "STOP_FOR_INTEGRITY_REVIEW"
    STOP_UNSUPPORTED_FORMAT = "STOP_UNSUPPORTED_FORMAT"


class ParentProvisionEvidenceRecoveryState(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"
    EXHAUSTED = "EXHAUSTED"


class ParentProvisionEvidenceRecoveryOutcome(StrEnum):
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"
    NOT_RECOVERABLE = "NOT_RECOVERABLE"


_EXPECTED_RECOVERABILITY = {
    ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.MISSING_PAGE_RENDER: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.MISSING_TABLE_EVIDENCE: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.MISSING_FIGURE_EVIDENCE: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.MISSING_ASSET_PLACEMENT: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.EXISTING_EVIDENCE_UNBOUND: (
        ParentProvisionEvidenceRecoverability.AUTOMATIC
    ),
    ParentProvisionEvidenceGap.AMBIGUOUS_OBJECT_EVIDENCE: (
        ParentProvisionEvidenceRecoverability.ASSISTED
    ),
    ParentProvisionEvidenceGap.INTEGRITY_DRIFT: (
        ParentProvisionEvidenceRecoverability.INTEGRITY_REVIEW_REQUIRED
    ),
    ParentProvisionEvidenceGap.EXTERNAL_SOURCE_MISSING: (
        ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED
    ),
    ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED: (
        ParentProvisionEvidenceRecoverability.EXTERNAL_INPUT_REQUIRED
    ),
    ParentProvisionEvidenceGap.UNSUPPORTED_EVIDENCE_FORMAT: (
        ParentProvisionEvidenceRecoverability.NOT_RECOVERABLE
    ),
}

_ALLOWED_STRATEGIES = {
    ParentProvisionEvidenceGap.SCANNED_OR_NO_NATIVE_TEXT: {
        ParentProvisionEvidenceRecoveryStrategy.RECAPTURE_NATIVE_TEXT,
        ParentProvisionEvidenceRecoveryStrategy.AUTHORIZED_VISUAL_TEXT_OCR,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.MISSING_PAGE_RENDER: {
        ParentProvisionEvidenceRecoveryStrategy.RERENDER_SOURCE_PAGES,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.MISSING_TABLE_EVIDENCE: {
        ParentProvisionEvidenceRecoveryStrategy.REBUILD_TABLE_EVIDENCE,
        ParentProvisionEvidenceRecoveryStrategy.VISUAL_RECONSTRUCT_TABLE,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.MISSING_FIGURE_EVIDENCE: {
        ParentProvisionEvidenceRecoveryStrategy.RECONSTRUCT_FIGURE_EVIDENCE,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.MISSING_ASSET_PLACEMENT: {
        ParentProvisionEvidenceRecoveryStrategy.REBUILD_ASSET_PLACEMENTS,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.EXISTING_EVIDENCE_UNBOUND: {
        ParentProvisionEvidenceRecoveryStrategy.REBIND_EXISTING_EVIDENCE,
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
    },
    ParentProvisionEvidenceGap.AMBIGUOUS_OBJECT_EVIDENCE: {
        ParentProvisionEvidenceRecoveryStrategy.RECOMPUTE_TARGET_EVIDENCE,
        ParentProvisionEvidenceRecoveryStrategy.REQUEST_USER_EVIDENCE,
    },
    ParentProvisionEvidenceGap.INTEGRITY_DRIFT: {
        ParentProvisionEvidenceRecoveryStrategy.STOP_FOR_INTEGRITY_REVIEW,
    },
    ParentProvisionEvidenceGap.EXTERNAL_SOURCE_MISSING: {
        ParentProvisionEvidenceRecoveryStrategy.REQUEST_EXTERNAL_SOURCE,
        ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
        ParentProvisionEvidenceRecoveryStrategy.REQUEST_USER_EVIDENCE,
    },
    ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED: {
        ParentProvisionEvidenceRecoveryStrategy.REQUEST_REPLACEMENT_SOURCE,
    },
    ParentProvisionEvidenceGap.UNSUPPORTED_EVIDENCE_FORMAT: {
        ParentProvisionEvidenceRecoveryStrategy.STOP_UNSUPPORTED_FORMAT,
    },
}

_NON_AUTOMATIC_GAPS = {
    ParentProvisionEvidenceGap.INTEGRITY_DRIFT,
    ParentProvisionEvidenceGap.EXTERNAL_SOURCE_MISSING,
    ParentProvisionEvidenceGap.SOURCE_CORRUPTED_OR_ENCRYPTED,
    ParentProvisionEvidenceGap.UNSUPPORTED_EVIDENCE_FORMAT,
}


class ParentProvisionEvidenceRecoveryPlanV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-evidence-recovery-plan/v1"] = (
        PARENT_PROVISION_EVIDENCE_RECOVERY_PLAN_SCHEMA_VERSION
    )
    recovery_plan_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-plan-[0-9a-f]{32}$"
    )
    recovery_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    object_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0, le=8191)
    logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    target_execution_mode: Literal["RECOVERY_REQUIRED"] = "RECOVERY_REQUIRED"
    original_stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_target_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    gap: ParentProvisionEvidenceGap
    recoverability: ParentProvisionEvidenceRecoverability
    trigger_failure_code: str
    ordered_strategies: tuple[ParentProvisionEvidenceRecoveryStrategy, ...] = Field(
        min_length=1,
        max_length=16,
    )
    max_attempts: StrictInt = Field(ge=1, le=16)

    @field_validator("trigger_failure_code")
    @classmethod
    def validate_trigger_failure_code(cls, value: str) -> str:
        _require_machine_code(value, subject="recovery trigger")
        return value

    @model_validator(mode="after")
    def validate_plan(self) -> ParentProvisionEvidenceRecoveryPlanV1:
        _require_object_plan_binding(
            plan_id=self.object_plan_id,
            plan_sha256=self.object_plan_sha256,
            batch_id=self.batch_id,
            target_scope_sha256=self.target_scope_sha256,
        )
        if len(self.ordered_strategies) != len(set(self.ordered_strategies)):
            raise ValueError("recovery strategies must be ordered and distinct")
        if self.max_attempts != len(self.ordered_strategies):
            raise ValueError("recovery attempt budget must equal its strategy sequence")
        if self.recoverability is not _EXPECTED_RECOVERABILITY[self.gap]:
            raise ValueError("evidence gap recoverability is not honest")
        if any(
            strategy not in _ALLOWED_STRATEGIES[self.gap]
            for strategy in self.ordered_strategies
        ):
            raise ValueError("recovery strategy is not valid for the evidence gap")
        _validate_hashed_identity(
            self,
            id_field="recovery_plan_id",
            sha_field="recovery_plan_sha256",
            prefix="parent-provision-evidence-recovery-plan-",
        )
        return self


class ParentProvisionEvidenceRecoverySessionV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-evidence-recovery-session/v1"] = (
        PARENT_PROVISION_EVIDENCE_RECOVERY_SESSION_SCHEMA_VERSION
    )
    recovery_session_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-session-[0-9a-f]{32}$"
    )
    content_free: Literal[True] = True
    plan: ParentProvisionEvidenceRecoveryPlanV1
    initial_state: Literal[ParentProvisionEvidenceRecoveryState.PLANNED] = (
        ParentProvisionEvidenceRecoveryState.PLANNED
    )

    @model_validator(mode="after")
    def validate_session(self) -> ParentProvisionEvidenceRecoverySessionV1:
        expected = canonical_json_sha256(
            {
                "schema_version": self.schema_version,
                "recovery_plan_id": self.plan.recovery_plan_id,
                "recovery_plan_sha256": self.plan.recovery_plan_sha256,
                "run_id": self.plan.run_id,
                "batch_id": self.plan.batch_id,
            }
        )
        if self.recovery_session_id != (
            f"parent-provision-evidence-recovery-session-{expected[:32]}"
        ):
            raise ValueError("evidence recovery session identity drifted")
        return self


class ParentProvisionEvidenceRecoveryAttemptReceiptV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-evidence-recovery-attempt-receipt/v1"] = (
        PARENT_PROVISION_EVIDENCE_RECOVERY_ATTEMPT_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-attempt-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    recovery_session_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-session-[0-9a-f]{32}$"
    )
    recovery_plan_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-plan-[0-9a-f]{32}$"
    )
    recovery_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-attempt-[0-9a-f]{32}$"
    )
    attempt_ordinal: StrictInt = Field(ge=1, le=16)
    strategy: ParentProvisionEvidenceRecoveryStrategy
    feedback_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    state: ParentProvisionEvidenceRecoveryState
    outcome: ParentProvisionEvidenceRecoveryOutcome
    failure_code: str | None = None
    input_stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_target_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_stage_input_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    new_target_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    @field_validator("failure_code")
    @classmethod
    def validate_failure_code(cls, value: str | None) -> str | None:
        if value is not None:
            _require_machine_code(value, subject="recovery attempt failure")
        return value

    @model_validator(mode="after")
    def validate_attempt(self) -> ParentProvisionEvidenceRecoveryAttemptReceiptV1:
        if self.state in {
            ParentProvisionEvidenceRecoveryState.PLANNED,
            ParentProvisionEvidenceRecoveryState.RUNNING,
            ParentProvisionEvidenceRecoveryState.EXHAUSTED,
        }:
            raise ValueError("attempt receipt requires a terminal attempt state")
        expected_outcome = {
            ParentProvisionEvidenceRecoveryState.SUCCEEDED: (
                ParentProvisionEvidenceRecoveryOutcome.RECOVERED
            ),
            ParentProvisionEvidenceRecoveryState.FAILED: (
                ParentProvisionEvidenceRecoveryOutcome.FAILED
            ),
            ParentProvisionEvidenceRecoveryState.CANCELLED: (
                ParentProvisionEvidenceRecoveryOutcome.CANCELLED
            ),
            ParentProvisionEvidenceRecoveryState.BLOCKED: {
                ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
                ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
            },
            ParentProvisionEvidenceRecoveryState.INDETERMINATE: (
                ParentProvisionEvidenceRecoveryOutcome.INDETERMINATE
            ),
        }[self.state]
        allowed = (
            expected_outcome
            if isinstance(expected_outcome, set)
            else {expected_outcome}
        )
        if self.outcome not in allowed:
            raise ValueError("recovery attempt state and outcome disagree")
        if self.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED:
            if self.failure_code is not None:
                raise ValueError("recovered attempt cannot claim a failure")
            if (
                self.new_stage_input_bundle_sha256 is None
                or self.new_target_evidence_sha256 is None
                or self.new_stage_input_bundle_sha256
                == self.input_stage_input_bundle_sha256
            ):
                raise ValueError(
                    "recovery success requires a changed stage input and bound target evidence"
                )
        elif (
            self.failure_code is None
            or self.new_stage_input_bundle_sha256 is not None
            or self.new_target_evidence_sha256 is not None
        ):
            raise ValueError(
                "non-recovered attempt requires one exact failure and no output"
            )
        expected_attempt_id = stable_parent_provision_evidence_recovery_attempt_id(
            recovery_session_id=self.recovery_session_id,
            recovery_plan_sha256=self.recovery_plan_sha256,
            attempt_ordinal=self.attempt_ordinal,
            strategy=self.strategy,
            feedback_sha256=self.feedback_sha256,
        )
        if self.attempt_id != expected_attempt_id:
            raise ValueError("evidence recovery attempt identity drifted")
        _validate_hashed_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            prefix="parent-provision-evidence-recovery-attempt-receipt-",
        )
        return self


class ParentProvisionEvidenceRecoveryReceiptV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-evidence-recovery-receipt/v1"] = (
        PARENT_PROVISION_EVIDENCE_RECOVERY_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    session: ParentProvisionEvidenceRecoverySessionV1
    attempts: tuple[ParentProvisionEvidenceRecoveryAttemptReceiptV1, ...] = Field(
        default=(),
        max_length=16,
    )
    state: ParentProvisionEvidenceRecoveryState
    outcome: ParentProvisionEvidenceRecoveryOutcome
    accepted_attempt_id: str | None = Field(
        default=None,
        pattern=r"^parent-provision-evidence-recovery-attempt-[0-9a-f]{32}$",
    )
    new_stage_input_bundle_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    new_target_evidence_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason_codes: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value, subject="recovery receipt")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> ParentProvisionEvidenceRecoveryReceiptV1:
        plan = self.session.plan
        if len(self.attempts) > plan.max_attempts:
            raise ValueError("recovery session exceeded its finite attempt budget")
        if tuple(attempt.attempt_ordinal for attempt in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("recovery attempts must be contiguous and ordered")
        for offset, attempt in enumerate(self.attempts):
            if (
                attempt.recovery_session_id != self.session.recovery_session_id
                or attempt.recovery_plan_id != plan.recovery_plan_id
                or attempt.recovery_plan_sha256 != plan.recovery_plan_sha256
                or attempt.input_stage_input_bundle_sha256
                != plan.original_stage_input_bundle_sha256
                or attempt.input_target_evidence_sha256
                != plan.original_target_evidence_sha256
            ):
                raise ValueError("recovery attempt lineage drifted from its session")
            if attempt.strategy is not plan.ordered_strategies[offset]:
                raise ValueError(
                    "every retry must advance to the next recovery strategy"
                )
            expected_feedback = (
                None
                if offset == 0
                else stable_parent_provision_evidence_recovery_feedback_sha256(
                    previous_attempt=self.attempts[offset - 1]
                )
            )
            if attempt.feedback_sha256 != expected_feedback:
                raise ValueError("recovery feedback does not bind the previous failure")
            if (
                attempt.outcome
                in {
                    ParentProvisionEvidenceRecoveryOutcome.RECOVERED,
                    ParentProvisionEvidenceRecoveryOutcome.CANCELLED,
                    ParentProvisionEvidenceRecoveryOutcome.BLOCKED,
                    ParentProvisionEvidenceRecoveryOutcome.INDETERMINATE,
                    ParentProvisionEvidenceRecoveryOutcome.NOT_RECOVERABLE,
                }
                and offset != len(self.attempts) - 1
            ):
                raise ValueError("terminal recovery outcome must stop further attempts")

        recovered = tuple(
            attempt
            for attempt in self.attempts
            if attempt.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED
        )
        if self.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED:
            if plan.gap in _NON_AUTOMATIC_GAPS:
                raise ValueError(
                    "integrity or external evidence gap cannot auto-recover"
                )
            if len(recovered) != 1 or recovered[0] is not self.attempts[-1]:
                raise ValueError(
                    "recovered session requires one final successful attempt"
                )
            accepted = recovered[0]
            if (
                self.state is not ParentProvisionEvidenceRecoveryState.SUCCEEDED
                or self.accepted_attempt_id != accepted.attempt_id
                or self.new_stage_input_bundle_sha256
                != accepted.new_stage_input_bundle_sha256
                or self.new_target_evidence_sha256
                != accepted.new_target_evidence_sha256
                or self.reason_codes
            ):
                raise ValueError(
                    "recovery receipt does not bind its successful attempt"
                )
        else:
            if recovered:
                raise ValueError(
                    "successful recovery cannot be hidden by a failure receipt"
                )
            if (
                self.accepted_attempt_id is not None
                or self.new_stage_input_bundle_sha256 is not None
                or self.new_target_evidence_sha256 is not None
            ):
                raise ValueError(
                    "non-recovered session cannot claim recovered evidence"
                )
            if not self.reason_codes:
                raise ValueError("non-recovered session requires a machine reason")
            _validate_terminal_state_and_outcome(
                state=self.state,
                outcome=self.outcome,
                attempt_count=len(self.attempts),
                max_attempts=plan.max_attempts,
            )
        _validate_hashed_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            prefix="parent-provision-evidence-recovery-receipt-",
        )
        return self


class ParentProvisionTargetEvidenceBindingV1(_StrictFrozenModel):
    logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    target_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ParentProvisionEvidenceReplanReceiptV1(_StrictFrozenModel):
    schema_version: Literal["parent-provision-evidence-replan-receipt/v1"] = (
        PARENT_PROVISION_EVIDENCE_REPLAN_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^parent-provision-evidence-replan-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    recovery_receipt_id: str = Field(
        pattern=r"^parent-provision-evidence-recovery-receipt-[0-9a-f]{32}$"
    )
    recovery_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    old_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    new_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    new_stage_input_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    old_target_evidence: tuple[ParentProvisionTargetEvidenceBindingV1, ...] = Field(
        min_length=1
    )
    new_target_evidence: tuple[ParentProvisionTargetEvidenceBindingV1, ...] = Field(
        min_length=1
    )
    recovered_logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    reusable_logical_object_ids: tuple[str, ...]
    invalidated_logical_object_ids: tuple[str, ...]

    @field_validator(
        "reusable_logical_object_ids",
        "invalidated_logical_object_ids",
    )
    @classmethod
    def validate_object_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _LOGICAL_OBJECT_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("replan object IDs must be unique and valid")
        return value

    @model_validator(mode="after")
    def validate_replan(self) -> ParentProvisionEvidenceReplanReceiptV1:
        if (
            self.old_plan_id
            != f"parent-provision-object-plan-{self.old_plan_sha256[:32]}"
            or self.new_plan_id
            != f"parent-provision-object-plan-{self.new_plan_sha256[:32]}"
        ):
            raise ValueError("replan object plan identity drifted")
        old_ids = tuple(item.logical_object_id for item in self.old_target_evidence)
        new_ids = tuple(item.logical_object_id for item in self.new_target_evidence)
        if old_ids != new_ids or len(old_ids) != len(set(old_ids)):
            raise ValueError(
                "replan target evidence must preserve object order and identity"
            )
        old_by_id = {
            item.logical_object_id: item.target_evidence_sha256
            for item in self.old_target_evidence
        }
        new_by_id = {
            item.logical_object_id: item.target_evidence_sha256
            for item in self.new_target_evidence
        }
        expected_reusable = tuple(
            object_id
            for object_id in old_ids
            if object_id != self.recovered_logical_object_id
            and old_by_id[object_id] == new_by_id[object_id]
        )
        expected_invalidated = tuple(
            object_id
            for object_id in old_ids
            if object_id == self.recovered_logical_object_id
            or old_by_id[object_id] != new_by_id[object_id]
        )
        if self.recovered_logical_object_id not in old_by_id:
            raise ValueError("replan recovered object is not in the target ledger")
        if (
            self.reusable_logical_object_ids != expected_reusable
            or self.invalidated_logical_object_ids != expected_invalidated
            or set(expected_reusable) & set(expected_invalidated)
        ):
            raise ValueError("replan reusable/invalidated object partition drifted")
        if (
            self.old_stage_input_bundle_sha256 == self.new_stage_input_bundle_sha256
            or not expected_invalidated
        ):
            raise ValueError("replan must bind changed stage input and target evidence")
        _validate_hashed_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            prefix="parent-provision-evidence-replan-receipt-",
        )
        return self


def stable_parent_provision_evidence_recovery_feedback_sha256(
    *,
    previous_attempt: ParentProvisionEvidenceRecoveryAttemptReceiptV1,
) -> str:
    if previous_attempt.failure_code is None or (
        previous_attempt.outcome is ParentProvisionEvidenceRecoveryOutcome.RECOVERED
    ):
        raise ValueError("recovery feedback requires a prior failed attempt")
    return canonical_json_sha256(
        {
            "previous_attempt_id": previous_attempt.attempt_id,
            "previous_attempt_receipt_sha256": previous_attempt.receipt_sha256,
            "previous_failure_code": previous_attempt.failure_code,
            "previous_outcome": previous_attempt.outcome.value,
        }
    )


def stable_parent_provision_evidence_recovery_attempt_id(
    *,
    recovery_session_id: str,
    recovery_plan_sha256: str,
    attempt_ordinal: int,
    strategy: ParentProvisionEvidenceRecoveryStrategy,
    feedback_sha256: str | None,
) -> str:
    if (
        re.fullmatch(
            r"parent-provision-evidence-recovery-session-[0-9a-f]{32}",
            recovery_session_id,
        )
        is None
    ):
        raise ValueError("recovery session identity is invalid")
    _require_sha256(recovery_plan_sha256, subject="recovery plan")
    if isinstance(attempt_ordinal, bool) or not 1 <= attempt_ordinal <= 16:
        raise ValueError("recovery attempt ordinal is invalid")
    if feedback_sha256 is not None:
        _require_sha256(feedback_sha256, subject="recovery feedback")
    digest = canonical_json_sha256(
        {
            "recovery_session_id": recovery_session_id,
            "recovery_plan_sha256": recovery_plan_sha256,
            "attempt_ordinal": attempt_ordinal,
            "strategy": strategy,
            "feedback_sha256": feedback_sha256,
        }
    )
    return f"parent-provision-evidence-recovery-attempt-{digest[:32]}"


def build_parent_provision_evidence_recovery_plan(
    **values: Any,
) -> ParentProvisionEvidenceRecoveryPlanV1:
    values = dict(values)
    values.setdefault(
        "schema_version", PARENT_PROVISION_EVIDENCE_RECOVERY_PLAN_SCHEMA_VERSION
    )
    values.setdefault("content_free", True)
    return _build_hashed_model(
        ParentProvisionEvidenceRecoveryPlanV1,
        values,
        id_field="recovery_plan_id",
        sha_field="recovery_plan_sha256",
        prefix="parent-provision-evidence-recovery-plan-",
    )


def build_parent_provision_evidence_recovery_session(
    *,
    plan: ParentProvisionEvidenceRecoveryPlanV1,
) -> ParentProvisionEvidenceRecoverySessionV1:
    payload = {
        "schema_version": PARENT_PROVISION_EVIDENCE_RECOVERY_SESSION_SCHEMA_VERSION,
        "recovery_plan_id": plan.recovery_plan_id,
        "recovery_plan_sha256": plan.recovery_plan_sha256,
        "run_id": plan.run_id,
        "batch_id": plan.batch_id,
    }
    digest = canonical_json_sha256(payload)
    return ParentProvisionEvidenceRecoverySessionV1(
        recovery_session_id=(
            f"parent-provision-evidence-recovery-session-{digest[:32]}"
        ),
        plan=plan,
    )


def build_parent_provision_evidence_recovery_attempt_receipt(
    **values: Any,
) -> ParentProvisionEvidenceRecoveryAttemptReceiptV1:
    values = dict(values)
    values.setdefault(
        "schema_version",
        PARENT_PROVISION_EVIDENCE_RECOVERY_ATTEMPT_RECEIPT_SCHEMA_VERSION,
    )
    values.setdefault("content_free", True)
    values["attempt_id"] = stable_parent_provision_evidence_recovery_attempt_id(
        recovery_session_id=values["recovery_session_id"],
        recovery_plan_sha256=values["recovery_plan_sha256"],
        attempt_ordinal=values["attempt_ordinal"],
        strategy=values["strategy"],
        feedback_sha256=values.get("feedback_sha256"),
    )
    return _build_hashed_model(
        ParentProvisionEvidenceRecoveryAttemptReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        prefix="parent-provision-evidence-recovery-attempt-receipt-",
    )


def build_parent_provision_evidence_recovery_receipt(
    **values: Any,
) -> ParentProvisionEvidenceRecoveryReceiptV1:
    values = dict(values)
    values.setdefault(
        "schema_version",
        PARENT_PROVISION_EVIDENCE_RECOVERY_RECEIPT_SCHEMA_VERSION,
    )
    values.setdefault("content_free", True)
    values.setdefault("attempts", ())
    values.setdefault("reason_codes", ())
    return _build_hashed_model(
        ParentProvisionEvidenceRecoveryReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        prefix="parent-provision-evidence-recovery-receipt-",
    )


def build_parent_provision_evidence_replan_receipt(
    *,
    recovery_receipt: ParentProvisionEvidenceRecoveryReceiptV1,
    old_plan: ParentProvisionObjectBatchPlanV1,
    new_plan: ParentProvisionObjectBatchPlanV1,
) -> ParentProvisionEvidenceReplanReceiptV1:
    if recovery_receipt.outcome is not ParentProvisionEvidenceRecoveryOutcome.RECOVERED:
        raise ValueError("only a recovered session can produce a replan receipt")
    recovery_plan = recovery_receipt.session.plan
    if (
        old_plan.plan_id != recovery_plan.object_plan_id
        or old_plan.plan_sha256 != recovery_plan.object_plan_sha256
        or old_plan.run_id != new_plan.run_id
        or old_plan.source_sha256 != new_plan.source_sha256
        or old_plan.kit_bundle_sha256 != new_plan.kit_bundle_sha256
        or old_plan.predecessor_artifact_id != new_plan.predecessor_artifact_id
        or old_plan.predecessor_artifact_sha256 != new_plan.predecessor_artifact_sha256
    ):
        raise ValueError("replan lineage drifted from recovery and old plan")
    old_bindings = _target_evidence_bindings(old_plan)
    new_bindings = _target_evidence_bindings(new_plan)
    old_ids = tuple(item.logical_object_id for item in old_bindings)
    if old_ids != tuple(item.logical_object_id for item in new_bindings):
        raise ValueError("replan changed the logical-object target set")
    old_map = {
        item.logical_object_id: item.target_evidence_sha256 for item in old_bindings
    }
    new_map = {
        item.logical_object_id: item.target_evidence_sha256 for item in new_bindings
    }
    old_target = next(
        (
            item
            for item in old_plan.targets
            if item.logical_object_id == recovery_plan.logical_object_id
        ),
        None,
    )
    new_target = next(
        (
            item
            for item in new_plan.targets
            if item.logical_object_id == recovery_plan.logical_object_id
        ),
        None,
    )
    if (
        old_target is None
        or new_target is None
        or old_target.batch_id != recovery_plan.batch_id
        or old_target.target_scope_sha256 != recovery_plan.target_scope_sha256
        or old_target.execution_mode
        is not ParentProvisionObjectExecutionMode.RECOVERY_REQUIRED
        or new_target.execution_mode
        is not ParentProvisionObjectExecutionMode.MODEL_TARGET
        or old_plan.stage_input_bundle_sha256
        != recovery_plan.original_stage_input_bundle_sha256
        or old_target.target_evidence_sha256
        != recovery_plan.original_target_evidence_sha256
        or new_plan.plan_sha256 == old_plan.plan_sha256
        or new_plan.stage_input_bundle_sha256
        != recovery_receipt.new_stage_input_bundle_sha256
        or new_map.get(recovery_plan.logical_object_id)
        != recovery_receipt.new_target_evidence_sha256
    ):
        raise ValueError("replan does not consume the recovered evidence identities")
    values = {
        "schema_version": PARENT_PROVISION_EVIDENCE_REPLAN_RECEIPT_SCHEMA_VERSION,
        "content_free": True,
        "recovery_receipt_id": recovery_receipt.receipt_id,
        "recovery_receipt_sha256": recovery_receipt.receipt_sha256,
        "old_plan_id": old_plan.plan_id,
        "old_plan_sha256": old_plan.plan_sha256,
        "new_plan_id": new_plan.plan_id,
        "new_plan_sha256": new_plan.plan_sha256,
        "old_stage_input_bundle_sha256": old_plan.stage_input_bundle_sha256,
        "new_stage_input_bundle_sha256": new_plan.stage_input_bundle_sha256,
        "old_target_evidence": old_bindings,
        "new_target_evidence": new_bindings,
        "recovered_logical_object_id": recovery_plan.logical_object_id,
        "reusable_logical_object_ids": tuple(
            object_id
            for object_id in old_ids
            if object_id != recovery_plan.logical_object_id
            and old_map[object_id] == new_map[object_id]
        ),
        "invalidated_logical_object_ids": tuple(
            object_id
            for object_id in old_ids
            if object_id == recovery_plan.logical_object_id
            or old_map[object_id] != new_map[object_id]
        ),
    }
    return _build_hashed_model(
        ParentProvisionEvidenceReplanReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        prefix="parent-provision-evidence-replan-receipt-",
    )


def canonical_json_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _target_evidence_bindings(
    plan: ParentProvisionObjectBatchPlanV1,
) -> tuple[ParentProvisionTargetEvidenceBindingV1, ...]:
    try:
        return tuple(
            ParentProvisionTargetEvidenceBindingV1(
                logical_object_id=target.logical_object_id,
                target_evidence_sha256=target.target_evidence_sha256,
            )
            for target in plan.targets
        )
    except AttributeError as exc:
        raise ValueError(
            "object batch plan does not expose target evidence identity"
        ) from exc


def _validate_terminal_state_and_outcome(
    *,
    state: ParentProvisionEvidenceRecoveryState,
    outcome: ParentProvisionEvidenceRecoveryOutcome,
    attempt_count: int,
    max_attempts: int,
) -> None:
    allowed = {
        ParentProvisionEvidenceRecoveryState.FAILED: {
            ParentProvisionEvidenceRecoveryOutcome.FAILED
        },
        ParentProvisionEvidenceRecoveryState.EXHAUSTED: {
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
    if state not in allowed or outcome not in allowed[state]:
        raise ValueError("recovery receipt state and outcome disagree")
    if state is ParentProvisionEvidenceRecoveryState.EXHAUSTED and (
        attempt_count != max_attempts
    ):
        raise ValueError("exhausted recovery must consume its finite attempt budget")


def _require_object_plan_binding(
    *,
    plan_id: str,
    plan_sha256: str,
    batch_id: str,
    target_scope_sha256: str,
) -> None:
    _require_sha256(plan_sha256, subject="object batch plan")
    _require_sha256(target_scope_sha256, subject="object target scope")
    if plan_id != f"parent-provision-object-plan-{plan_sha256[:32]}":
        raise ValueError("object batch plan identity drifted")
    if batch_id != f"parent-provision-object-batch-{target_scope_sha256[:32]}":
        raise ValueError("object batch target identity drifted")


def _build_hashed_model(
    model: type[_StrictFrozenModel],
    values: Mapping[str, Any],
    *,
    id_field: str,
    sha_field: str,
    prefix: str,
) -> Any:
    payload_values = dict(values)
    payload_values.pop(id_field, None)
    payload_values.pop(sha_field, None)
    draft = model.model_construct(
        **payload_values,
        **{id_field: prefix + "0" * 32, sha_field: "0" * 64},
    )
    payload = draft.model_dump(mode="json", exclude={id_field, sha_field})
    digest = canonical_json_sha256(payload)
    return model(
        **payload_values,
        **{id_field: prefix + digest[:32], sha_field: digest},
    )


def _validate_hashed_identity(
    value: _StrictFrozenModel,
    *,
    id_field: str,
    sha_field: str,
    prefix: str,
) -> None:
    expected = canonical_json_sha256(
        value.model_dump(mode="json", exclude={id_field, sha_field})
    )
    if getattr(value, sha_field) != expected:
        raise ValueError("evidence recovery receipt SHA drifted")
    if getattr(value, id_field) != prefix + expected[:32]:
        raise ValueError("evidence recovery receipt identity drifted")


def _require_machine_code(value: str, *, subject: str) -> None:
    if _MACHINE_CODE_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{subject} must be a bounded machine code")


def _require_machine_codes(value: tuple[str, ...], *, subject: str) -> None:
    if value != tuple(sorted(set(value))) or any(
        _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
    ):
        raise ValueError(f"{subject} reason codes must be sorted machine codes")


def _require_sha256(value: str, *, subject: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{subject} SHA-256 is invalid")


__all__ = [
    "PARENT_PROVISION_EVIDENCE_RECOVERY_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_EVIDENCE_RECOVERY_PLAN_SCHEMA_VERSION",
    "PARENT_PROVISION_EVIDENCE_RECOVERY_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_EVIDENCE_RECOVERY_SESSION_SCHEMA_VERSION",
    "PARENT_PROVISION_EVIDENCE_REPLAN_RECEIPT_SCHEMA_VERSION",
    "ParentProvisionEvidenceGap",
    "ParentProvisionEvidenceRecoverability",
    "ParentProvisionEvidenceRecoveryAttemptReceiptV1",
    "ParentProvisionEvidenceRecoveryOutcome",
    "ParentProvisionEvidenceRecoveryPlanV1",
    "ParentProvisionEvidenceRecoveryReceiptV1",
    "ParentProvisionEvidenceRecoverySessionV1",
    "ParentProvisionEvidenceRecoveryState",
    "ParentProvisionEvidenceRecoveryStrategy",
    "ParentProvisionEvidenceReplanReceiptV1",
    "ParentProvisionTargetEvidenceBindingV1",
    "build_parent_provision_evidence_recovery_attempt_receipt",
    "build_parent_provision_evidence_recovery_plan",
    "build_parent_provision_evidence_recovery_receipt",
    "build_parent_provision_evidence_recovery_session",
    "build_parent_provision_evidence_replan_receipt",
    "canonical_json_sha256",
    "stable_parent_provision_evidence_recovery_attempt_id",
    "stable_parent_provision_evidence_recovery_feedback_sha256",
]
