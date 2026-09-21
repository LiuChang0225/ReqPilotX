"""Content-free execution receipts for batched parent-provision recognition.

The contracts keep execution failure separate from semantic uncertainty.  Only
a Host-validated candidate can carry semantic-unresolved scope; transport
failure, rejected output, cancellation, blocking, and indeterminate recovery
state never manufacture unresolved document content.
"""

from __future__ import annotations

from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


PARENT_PROVISION_RECOGNITION_ATTEMPT_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-recognition-attempt-receipt/v1"
)
PARENT_PROVISION_RECOGNITION_BATCH_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-recognition-batch-receipt/v1"
)
PARENT_PROVISION_RECOGNITION_AGGREGATE_EXECUTION_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-recognition-aggregate-execution-receipt/v2"
)
PARENT_PROVISION_RECOGNITION_SELECTION_POLICY = "FIRST_HOST_VALIDATED_RETRY/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionRecognitionAttemptOutcome(StrEnum):
    VALIDATED_CANDIDATE = "VALIDATED_CANDIDATE"
    TRANSPORT_FAILED = "TRANSPORT_FAILED"
    OUTPUT_REJECTED = "OUTPUT_REJECTED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ParentProvisionRecognitionCleanupStatus(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ParentProvisionRecognitionBatchStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_SEMANTIC_UNRESOLVED = "ACCEPTED_WITH_SEMANTIC_UNRESOLVED"
    ACCEPTED_RECONCILIATION_REQUIRED = "ACCEPTED_RECONCILIATION_REQUIRED"
    HOST_ACCEPTED_EMPTY_CONTAINER = "HOST_ACCEPTED_EMPTY_CONTAINER"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ParentProvisionRecognitionAggregateStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_SEMANTIC = "PARTIAL_SEMANTIC"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ParentProvisionRecognitionAttemptReceiptV1(_StrictFrozenModel):
    """One terminal, content-free App Server attempt for one immutable batch."""

    schema_version: Literal["parent-provision-recognition-attempt-receipt/v1"] = (
        PARENT_PROVISION_RECOGNITION_ATTEMPT_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^parent-provision-object-attempt-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    attempt_id: str = Field(pattern=r"^parent-provision-object-attempt-[0-9a-f]{32}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0, le=8191)
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_ordinal: StrictInt = Field(ge=1, le=64)
    task_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    outcome: ParentProvisionRecognitionAttemptOutcome
    transport_result_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tool_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runtime_thread_id_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    runtime_turn_id_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_compilation_result_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(default=0, ge=0, le=65536)
    cleanup_status: ParentProvisionRecognitionCleanupStatus
    workspace_unchanged: bool
    reason_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value, subject="attempt")
        return value

    @model_validator(mode="after")
    def validate_attempt(self) -> ParentProvisionRecognitionAttemptReceiptV1:
        _require_plan_and_batch_binding(
            plan_id=self.plan_id,
            plan_sha256=self.plan_sha256,
            batch_id=self.batch_id,
            target_scope_sha256=self.target_scope_sha256,
        )
        expected_attempt_id = stable_parent_provision_recognition_attempt_id(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            plan_id=self.plan_id,
            plan_sha256=self.plan_sha256,
            batch_id=self.batch_id,
            batch_order=self.batch_order,
            target_scope_sha256=self.target_scope_sha256,
            attempt_ordinal=self.attempt_ordinal,
            task_binding_sha256=self.task_binding_sha256,
        )
        if self.attempt_id != expected_attempt_id:
            raise ValueError("parent-recognition attempt identity drifted")

        success_fields = (
            self.transport_result_sha256,
            self.response_sha256,
            self.tool_receipt_sha256,
            self.runtime_thread_id_sha256,
            self.runtime_turn_id_sha256,
            self.candidate_sha256,
            self.batch_compilation_result_sha256,
        )
        if self.outcome is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE:
            if not all(success_fields):
                raise ValueError(
                    "validated attempt requires complete transport and Host proof"
                )
            if self.reason_codes:
                raise ValueError(
                    "validated attempt cannot carry execution failure reasons"
                )
            if (
                self.cleanup_status
                is not ParentProvisionRecognitionCleanupStatus.SUCCEEDED
                or not self.workspace_unchanged
            ):
                raise ValueError("validated attempt requires successful cleanup")
        else:
            if self.batch_compilation_result_sha256 is not None:
                raise ValueError("non-validated attempt cannot claim Host validation")
            if (
                self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
            ):
                raise ValueError(
                    "execution failure or cancellation cannot manufacture semantic unresolved results"
                )
            if not self.reason_codes:
                raise ValueError("non-validated attempt requires a machine reason")
            if (
                self.outcome
                is ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED
                and self.transport_result_sha256 is not None
            ):
                raise ValueError(
                    "transport failure cannot claim a successful transport result"
                )
            if (
                self.outcome is ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED
                and not all(
                    (
                        self.transport_result_sha256,
                        self.response_sha256,
                        self.tool_receipt_sha256,
                        self.runtime_thread_id_sha256,
                        self.runtime_turn_id_sha256,
                    )
                )
            ):
                raise ValueError(
                    "rejected output requires a completed transport result"
                )
            if (
                self.outcome is ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED
                and (
                    self.cleanup_status
                    is not ParentProvisionRecognitionCleanupStatus.SUCCEEDED
                    or not self.workspace_unchanged
                )
            ):
                raise ValueError("rejected output still requires successful cleanup")
            if (
                self.outcome
                in {
                    ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
                    ParentProvisionRecognitionAttemptOutcome.CANCELLED,
                    ParentProvisionRecognitionAttemptOutcome.BLOCKED,
                    ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
                }
                and self.candidate_sha256 is not None
            ):
                raise ValueError(
                    "attempt without accepted output cannot claim a candidate"
                )
            if (
                self.outcome
                in {
                    ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED,
                    ParentProvisionRecognitionAttemptOutcome.CANCELLED,
                    ParentProvisionRecognitionAttemptOutcome.BLOCKED,
                    ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
                }
                and self.response_sha256 is not None
            ):
                raise ValueError(
                    "attempt without completed output cannot claim a response"
                )
        if (
            self.cleanup_status is ParentProvisionRecognitionCleanupStatus.FAILED
            and self.outcome is not ParentProvisionRecognitionAttemptOutcome.BLOCKED
        ):
            raise ValueError("failed cleanup must block the attempt")
        if (
            self.cleanup_status is ParentProvisionRecognitionCleanupStatus.UNKNOWN
            and self.outcome
            is not ParentProvisionRecognitionAttemptOutcome.INDETERMINATE
        ):
            raise ValueError("unknown cleanup state must remain indeterminate")
        if not self.workspace_unchanged and (
            self.outcome is not ParentProvisionRecognitionAttemptOutcome.BLOCKED
            or self.cleanup_status is not ParentProvisionRecognitionCleanupStatus.FAILED
        ):
            raise ValueError("workspace drift must block the attempt and fail cleanup")

        _validate_receipt_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-object-attempt-receipt-",
        )
        return self


class ParentProvisionRecognitionBatchReceiptV1(_StrictFrozenModel):
    """All ordered attempts for one batch and its sole accepted attempt, if any."""

    schema_version: Literal["parent-provision-recognition-batch-receipt/v1"] = (
        PARENT_PROVISION_RECOGNITION_BATCH_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^parent-provision-object-batch-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    batch_id: str = Field(pattern=r"^parent-provision-object-batch-[0-9a-f]{32}$")
    batch_order: StrictInt = Field(ge=0, le=8191)
    target_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_policy: Literal["FIRST_HOST_VALIDATED_RETRY/v1"] = (
        PARENT_PROVISION_RECOGNITION_SELECTION_POLICY
    )
    attempts: tuple[ParentProvisionRecognitionAttemptReceiptV1, ...] = Field(
        default=(),
        max_length=64,
    )
    accepted_attempt_id: str | None = Field(
        default=None,
        pattern=r"^parent-provision-object-attempt-[0-9a-f]{32}$",
    )
    accepted_attempt_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: ParentProvisionRecognitionBatchStatus
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(default=0, ge=0, le=65536)
    reason_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value, subject="batch")
        return value

    @model_validator(mode="after")
    def validate_batch(self) -> ParentProvisionRecognitionBatchReceiptV1:
        _require_plan_and_batch_binding(
            plan_id=self.plan_id,
            plan_sha256=self.plan_sha256,
            batch_id=self.batch_id,
            target_scope_sha256=self.target_scope_sha256,
        )
        if tuple(item.attempt_ordinal for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError(
                "parent-recognition attempts must be contiguous and ordered"
            )
        if len({item.attempt_id for item in self.attempts}) != len(self.attempts):
            raise ValueError("parent-recognition attempt identities must be unique")
        for attempt in self.attempts:
            if (
                attempt.run_id != self.run_id
                or attempt.source_sha256 != self.source_sha256
                or attempt.predecessor_artifact_id != self.predecessor_artifact_id
                or attempt.predecessor_artifact_sha256
                != self.predecessor_artifact_sha256
                or attempt.kit_bundle_sha256 != self.kit_bundle_sha256
                or attempt.plan_id != self.plan_id
                or attempt.plan_sha256 != self.plan_sha256
                or attempt.batch_id != self.batch_id
                or attempt.batch_order != self.batch_order
                or attempt.target_scope_sha256 != self.target_scope_sha256
            ):
                raise ValueError(
                    "parent-recognition attempt lineage drifted from its batch"
                )
        terminal_stop_outcomes = {
            ParentProvisionRecognitionAttemptOutcome.CANCELLED,
            ParentProvisionRecognitionAttemptOutcome.BLOCKED,
            ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
        }
        if any(
            attempt.outcome in terminal_stop_outcomes for attempt in self.attempts[:-1]
        ):
            raise ValueError("cancelled, blocked, or indeterminate batch cannot retry")

        validated = tuple(
            item
            for item in self.attempts
            if item.outcome
            is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE
        )
        accepted_status = self.status in {
            ParentProvisionRecognitionBatchStatus.ACCEPTED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
        }
        host_accepted = (
            self.status
            is ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER
        )
        if host_accepted:
            if (
                self.attempts
                or self.accepted_attempt_id is not None
                or self.accepted_attempt_receipt_sha256 is not None
                or self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                or self.reason_codes != ("HOST_EMPTY_CONTAINER",)
            ):
                raise ValueError(
                    "Host-empty batch requires no attempts and its exact diagnostic"
                )
        elif accepted_status:
            if not validated:
                raise ValueError("accepted batch requires a validated attempt")
            first_validated = validated[0]
            if (
                self.accepted_attempt_id != first_validated.attempt_id
                or self.accepted_attempt_receipt_sha256
                != first_validated.receipt_sha256
                or self.attempts[-1].attempt_id != first_validated.attempt_id
                or len(validated) != 1
            ):
                raise ValueError(
                    "batch must accept the first Host-validated attempt and stop retrying"
                )
            if (
                self.semantic_unresolved_scope_count
                != first_validated.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                != first_validated.semantic_unresolved_entry_count
            ):
                raise ValueError(
                    "batch semantic unresolved count drifted from acceptance"
                )
            if (
                self.status
                is ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
            ):
                if (
                    self.semantic_unresolved_scope_count
                    or self.semantic_unresolved_entry_count
                    or self.reason_codes != ("ADJACENT_RECONCILIATION_REQUIRED",)
                ):
                    raise ValueError(
                        "reconciliation batch requires its exact non-semantic diagnostic"
                    )
            else:
                expected_status = (
                    ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
                    if (
                        self.semantic_unresolved_scope_count
                        or self.semantic_unresolved_entry_count
                    )
                    else ParentProvisionRecognitionBatchStatus.ACCEPTED
                )
                if self.status is not expected_status or self.reason_codes:
                    raise ValueError(
                        "accepted batch status does not match its semantic result"
                    )
        else:
            if validated:
                raise ValueError(
                    "a Host-validated attempt cannot be silently left unaccepted"
                )
            if (
                self.accepted_attempt_id is not None
                or self.accepted_attempt_receipt_sha256 is not None
                or self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
            ):
                raise ValueError(
                    "failed or cancelled batch cannot claim acceptance or semantic unresolved"
                )
            if not self.reason_codes:
                raise ValueError("non-accepted batch requires a machine reason")
            if self.attempts:
                expected_status = _batch_terminal_status(self.attempts)
                if self.status is not expected_status:
                    raise ValueError("batch status does not match its terminal attempt")
            elif self.status not in {
                ParentProvisionRecognitionBatchStatus.CANCELLED,
                ParentProvisionRecognitionBatchStatus.BLOCKED,
                ParentProvisionRecognitionBatchStatus.INDETERMINATE,
            }:
                raise ValueError(
                    "batch without attempts must be cancelled, blocked, or indeterminate"
                )

        _validate_receipt_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-object-batch-receipt-",
        )
        return self


class ParentProvisionRecognitionAggregateExecutionReceiptV2(_StrictFrozenModel):
    """Whole-document closure that preserves every independently processed batch."""

    schema_version: Literal[
        "parent-provision-recognition-aggregate-execution-receipt/v2"
    ] = PARENT_PROVISION_RECOGNITION_AGGREGATE_EXECUTION_RECEIPT_SCHEMA_VERSION
    execution_id: str = Field(
        pattern=r"^parent-provision-object-aggregate-execution-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    plan_id: str = Field(pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_batch_count: StrictInt = Field(ge=1, le=8192)
    batches: tuple[ParentProvisionRecognitionBatchReceiptV1, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    batch_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_attempt_ids: tuple[str, ...] = Field(default=(), max_length=8192)
    accepted_attempt_receipt_sha256s: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    accepted_attempts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregate_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ParentProvisionRecognitionAggregateStatus
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(default=0, ge=0, le=65536)
    predecessor_partial: bool = False
    object_batch_aggregate_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    decision_candidate_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    parent_provision_decision_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    evidence_refs_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    reason_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("accepted_attempt_ids")
    @classmethod
    def validate_accepted_attempt_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"parent-provision-object-attempt-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("accepted attempt identities must be unique and valid")
        return value

    @field_validator("accepted_attempt_receipt_sha256s")
    @classmethod
    def validate_accepted_receipt_hashes(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        if any(_SHA256_PATTERN.fullmatch(item) is None for item in value):
            raise ValueError("accepted attempt receipt hashes must be valid")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value, subject="aggregate")
        return value

    @model_validator(mode="after")
    def validate_aggregate(
        self,
    ) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
        if len(self.batches) > self.planned_batch_count:
            raise ValueError("aggregate contains more batches than its immutable plan")
        _require_plan_identity(plan_id=self.plan_id, plan_sha256=self.plan_sha256)
        if tuple(item.batch_order for item in self.batches) != tuple(
            range(len(self.batches))
        ):
            raise ValueError("aggregate batches must be a contiguous ordered prefix")
        if len({item.batch_id for item in self.batches}) != len(self.batches):
            raise ValueError("aggregate batch identities must be unique")
        for batch in self.batches:
            if (
                batch.run_id != self.run_id
                or batch.source_sha256 != self.source_sha256
                or batch.predecessor_artifact_id != self.predecessor_artifact_id
                or batch.predecessor_artifact_sha256 != self.predecessor_artifact_sha256
                or batch.kit_bundle_sha256 != self.kit_bundle_sha256
                or batch.plan_id != self.plan_id
                or batch.plan_sha256 != self.plan_sha256
            ):
                raise ValueError("aggregate batch lineage drifted")

        batch_hashes = tuple(item.receipt_sha256 for item in self.batches)
        all_attempt_hashes = tuple(
            attempt.receipt_sha256
            for batch in self.batches
            for attempt in batch.attempts
        )
        accepted_attempts = tuple(
            attempt
            for batch in self.batches
            if batch.accepted_attempt_id is not None
            for attempt in batch.attempts
            if attempt.attempt_id == batch.accepted_attempt_id
        )
        accepted_ids = tuple(item.attempt_id for item in accepted_attempts)
        accepted_hashes = tuple(item.receipt_sha256 for item in accepted_attempts)
        if (
            self.batch_receipts_sha256 != canonical_json_sha256(batch_hashes)
            or self.attempt_inventory_sha256
            != canonical_json_sha256(all_attempt_hashes)
            or self.accepted_attempt_ids != accepted_ids
            or self.accepted_attempt_receipt_sha256s != accepted_hashes
            or self.accepted_attempts_sha256 != canonical_json_sha256(accepted_hashes)
        ):
            raise ValueError("aggregate attempt or batch receipt roots drifted")

        expected_trace = canonical_json_sha256(
            {
                "run_id": self.run_id,
                "source_sha256": self.source_sha256,
                "predecessor_artifact_sha256": self.predecessor_artifact_sha256,
                "kit_bundle_sha256": self.kit_bundle_sha256,
                "policy_closure_sha256": self.policy_closure_sha256,
                "runner_profile_sha256": self.runner_profile_sha256,
                "model_id": self.model_id,
                "reasoning_effort": self.reasoning_effort,
                "plan_id": self.plan_id,
                "plan_sha256": self.plan_sha256,
                "batch_receipts_sha256": self.batch_receipts_sha256,
                "attempt_inventory_sha256": self.attempt_inventory_sha256,
                "accepted_attempts_sha256": self.accepted_attempts_sha256,
                "predecessor_partial": self.predecessor_partial,
            }
        )
        if self.aggregate_execution_trace_sha256 != expected_trace:
            raise ValueError("aggregate execution trace drifted")

        nonaccepted = tuple(
            batch
            for batch in self.batches
            if batch.status
            not in {
                ParentProvisionRecognitionBatchStatus.ACCEPTED,
                ParentProvisionRecognitionBatchStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
                ParentProvisionRecognitionBatchStatus.HOST_ACCEPTED_EMPTY_CONTAINER,
            }
        )
        final_hashes = (
            self.object_batch_aggregate_sha256,
            self.decision_candidate_sha256,
            self.parent_provision_decision_sha256,
            self.evidence_refs_sha256,
        )
        success_status = self.status in {
            ParentProvisionRecognitionAggregateStatus.COMPLETE,
            ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC,
        }
        if success_status:
            if nonaccepted or len(self.batches) != self.planned_batch_count:
                raise ValueError(
                    "successful aggregate requires every planned batch accepted"
                )
            if not all(final_hashes) or self.reason_codes:
                raise ValueError(
                    "successful aggregate requires complete final decision hashes"
                )
            expected_unresolved_scopes = sum(
                batch.semantic_unresolved_scope_count for batch in self.batches
            )
            expected_unresolved_entries = sum(
                batch.semantic_unresolved_entry_count for batch in self.batches
            )
            if (
                self.semantic_unresolved_scope_count != expected_unresolved_scopes
                or self.semantic_unresolved_entry_count != expected_unresolved_entries
            ):
                raise ValueError("aggregate semantic unresolved counts drifted")
            expected_status = (
                ParentProvisionRecognitionAggregateStatus.PARTIAL_SEMANTIC
                if (
                    expected_unresolved_scopes
                    or expected_unresolved_entries
                    or self.predecessor_partial
                )
                else ParentProvisionRecognitionAggregateStatus.COMPLETE
            )
            if self.status is not expected_status:
                raise ValueError("aggregate status does not match semantic capture")
        else:
            if not nonaccepted:
                raise ValueError(
                    "non-successful aggregate requires at least one non-accepted batch"
                )
            if any(final_hashes):
                raise ValueError(
                    "failed or cancelled aggregate cannot claim a final decision"
                )
            if (
                self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
            ):
                raise ValueError(
                    "execution failure or cancellation cannot become semantic unresolved"
                )
            if not self.reason_codes:
                raise ValueError("non-successful aggregate requires a machine reason")
            expected_status = _aggregate_terminal_status(
                tuple(item.status for item in nonaccepted)
            )
            if self.status is not expected_status:
                raise ValueError(
                    "aggregate status does not match its non-accepted batches"
                )

        _validate_receipt_identity(
            self,
            id_field="execution_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-object-aggregate-execution-",
        )
        return self


def stable_parent_provision_recognition_attempt_id(
    *,
    run_id: str,
    source_sha256: str,
    plan_id: str,
    plan_sha256: str,
    batch_id: str,
    batch_order: int,
    target_scope_sha256: str,
    attempt_ordinal: int,
    task_binding_sha256: str,
) -> str:
    if re.fullmatch(r"agent-run-[0-9a-f]{32}", run_id) is None:
        raise ValueError("run identity is invalid")
    for value, subject in (
        (source_sha256, "source"),
        (plan_sha256, "batch plan"),
        (target_scope_sha256, "target scope"),
        (task_binding_sha256, "task binding"),
    ):
        _require_sha256(value, subject=subject)
    _require_plan_and_batch_binding(
        plan_id=plan_id,
        plan_sha256=plan_sha256,
        batch_id=batch_id,
        target_scope_sha256=target_scope_sha256,
    )
    if (
        isinstance(batch_order, bool)
        or not isinstance(batch_order, int)
        or batch_order < 0
    ):
        raise ValueError("batch order must be a non-negative integer")
    if (
        isinstance(attempt_ordinal, bool)
        or not isinstance(attempt_ordinal, int)
        or attempt_ordinal < 1
    ):
        raise ValueError("attempt ordinal must be a positive integer")
    digest = canonical_json_sha256(
        {
            "run_id": run_id,
            "source_sha256": source_sha256,
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "batch_id": batch_id,
            "batch_order": batch_order,
            "target_scope_sha256": target_scope_sha256,
            "attempt_ordinal": attempt_ordinal,
            "task_binding_sha256": task_binding_sha256,
        }
    )
    return f"parent-provision-object-attempt-{digest[:32]}"


def build_parent_provision_recognition_attempt_receipt(
    **values: Any,
) -> ParentProvisionRecognitionAttemptReceiptV1:
    values = dict(values)
    values.setdefault(
        "schema_version",
        PARENT_PROVISION_RECOGNITION_ATTEMPT_RECEIPT_SCHEMA_VERSION,
    )
    values.setdefault("content_free", True)
    values.setdefault("reason_codes", ())
    values["attempt_id"] = stable_parent_provision_recognition_attempt_id(
        run_id=values["run_id"],
        source_sha256=values["source_sha256"],
        plan_id=values["plan_id"],
        plan_sha256=values["plan_sha256"],
        batch_id=values["batch_id"],
        batch_order=values["batch_order"],
        target_scope_sha256=values["target_scope_sha256"],
        attempt_ordinal=values["attempt_ordinal"],
        task_binding_sha256=values["task_binding_sha256"],
    )
    return _build_receipt(
        ParentProvisionRecognitionAttemptReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-object-attempt-receipt-",
    )


def build_parent_provision_recognition_batch_receipt(
    **values: Any,
) -> ParentProvisionRecognitionBatchReceiptV1:
    values = dict(values)
    values.setdefault(
        "schema_version",
        PARENT_PROVISION_RECOGNITION_BATCH_RECEIPT_SCHEMA_VERSION,
    )
    values.setdefault("content_free", True)
    values.setdefault(
        "selection_policy",
        PARENT_PROVISION_RECOGNITION_SELECTION_POLICY,
    )
    values.setdefault("attempts", ())
    values.setdefault("reason_codes", ())
    return _build_receipt(
        ParentProvisionRecognitionBatchReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-object-batch-receipt-",
    )


def build_parent_provision_recognition_aggregate_execution_receipt(
    **values: Any,
) -> ParentProvisionRecognitionAggregateExecutionReceiptV2:
    values = dict(values)
    values.setdefault(
        "schema_version",
        PARENT_PROVISION_RECOGNITION_AGGREGATE_EXECUTION_RECEIPT_SCHEMA_VERSION,
    )
    values.setdefault("content_free", True)
    values.setdefault("reason_codes", ())
    batches = tuple(values["batches"])
    values["batches"] = batches
    batch_hashes = tuple(item.receipt_sha256 for item in batches)
    attempt_hashes = tuple(
        attempt.receipt_sha256 for batch in batches for attempt in batch.attempts
    )
    accepted_attempts = tuple(
        attempt
        for batch in batches
        if batch.accepted_attempt_id is not None
        for attempt in batch.attempts
        if attempt.attempt_id == batch.accepted_attempt_id
    )
    accepted_ids = tuple(item.attempt_id for item in accepted_attempts)
    accepted_hashes = tuple(item.receipt_sha256 for item in accepted_attempts)
    values["batch_receipts_sha256"] = canonical_json_sha256(batch_hashes)
    values["attempt_inventory_sha256"] = canonical_json_sha256(attempt_hashes)
    values["accepted_attempt_ids"] = accepted_ids
    values["accepted_attempt_receipt_sha256s"] = accepted_hashes
    values["accepted_attempts_sha256"] = canonical_json_sha256(accepted_hashes)
    values["aggregate_execution_trace_sha256"] = canonical_json_sha256(
        {
            "run_id": values["run_id"],
            "source_sha256": values["source_sha256"],
            "predecessor_artifact_sha256": values["predecessor_artifact_sha256"],
            "kit_bundle_sha256": values["kit_bundle_sha256"],
            "policy_closure_sha256": values["policy_closure_sha256"],
            "runner_profile_sha256": values["runner_profile_sha256"],
            "model_id": values["model_id"],
            "reasoning_effort": values["reasoning_effort"],
            "plan_id": values["plan_id"],
            "plan_sha256": values["plan_sha256"],
            "batch_receipts_sha256": values["batch_receipts_sha256"],
            "attempt_inventory_sha256": values["attempt_inventory_sha256"],
            "accepted_attempts_sha256": values["accepted_attempts_sha256"],
            "predecessor_partial": values.get("predecessor_partial", False),
        }
    )
    return _build_receipt(
        ParentProvisionRecognitionAggregateExecutionReceiptV2,
        values,
        id_field="execution_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-object-aggregate-execution-",
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


def _batch_terminal_status(
    attempts: tuple[ParentProvisionRecognitionAttemptReceiptV1, ...],
) -> ParentProvisionRecognitionBatchStatus:
    if not attempts:
        raise ValueError(
            "batch without attempts must be CANCELLED, BLOCKED, or INDETERMINATE"
        )
    mapping = {
        ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED: (
            ParentProvisionRecognitionBatchStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED: (
            ParentProvisionRecognitionBatchStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.CANCELLED: (
            ParentProvisionRecognitionBatchStatus.CANCELLED
        ),
        ParentProvisionRecognitionAttemptOutcome.BLOCKED: (
            ParentProvisionRecognitionBatchStatus.BLOCKED
        ),
        ParentProvisionRecognitionAttemptOutcome.INDETERMINATE: (
            ParentProvisionRecognitionBatchStatus.INDETERMINATE
        ),
    }
    try:
        return mapping[attempts[-1].outcome]
    except KeyError as exc:
        raise ValueError("non-accepted batch has an invalid terminal attempt") from exc


def _aggregate_terminal_status(
    statuses: tuple[ParentProvisionRecognitionBatchStatus, ...],
) -> ParentProvisionRecognitionAggregateStatus:
    if not statuses:
        raise ValueError("aggregate requires one non-accepted batch status")
    invalid = set(statuses) - {
        ParentProvisionRecognitionBatchStatus.FAILED,
        ParentProvisionRecognitionBatchStatus.CANCELLED,
        ParentProvisionRecognitionBatchStatus.BLOCKED,
        ParentProvisionRecognitionBatchStatus.INDETERMINATE,
        ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED,
    }
    if invalid:
        raise ValueError("aggregate non-accepted batch status is invalid")
    if ParentProvisionRecognitionBatchStatus.INDETERMINATE in statuses:
        return ParentProvisionRecognitionAggregateStatus.INDETERMINATE
    if (
        ParentProvisionRecognitionBatchStatus.BLOCKED in statuses
        or ParentProvisionRecognitionBatchStatus.ACCEPTED_RECONCILIATION_REQUIRED
        in statuses
    ):
        return ParentProvisionRecognitionAggregateStatus.BLOCKED
    if ParentProvisionRecognitionBatchStatus.CANCELLED in statuses:
        return ParentProvisionRecognitionAggregateStatus.CANCELLED
    return ParentProvisionRecognitionAggregateStatus.FAILED


def _build_receipt(
    model: type[_StrictFrozenModel],
    values: dict[str, Any],
    *,
    id_field: str,
    sha_field: str,
    id_prefix: str,
) -> Any:
    values.pop(id_field, None)
    values.pop(sha_field, None)
    draft = model.model_construct(
        **values,
        **{id_field: id_prefix + "0" * 32, sha_field: "0" * 64},
    )
    payload = draft.model_dump(mode="json", exclude={id_field, sha_field})
    digest = canonical_json_sha256(payload)
    return model(**values, **{id_field: id_prefix + digest[:32], sha_field: digest})


def _validate_receipt_identity(
    receipt: _StrictFrozenModel,
    *,
    id_field: str,
    sha_field: str,
    id_prefix: str,
) -> None:
    payload = receipt.model_dump(mode="json", exclude={id_field, sha_field})
    expected = canonical_json_sha256(payload)
    if getattr(receipt, sha_field) != expected:
        raise ValueError("content-free receipt SHA drifted")
    if getattr(receipt, id_field) != id_prefix + expected[:32]:
        raise ValueError("content-free receipt identity drifted")


def _require_machine_codes(value: tuple[str, ...], *, subject: str) -> None:
    if value != tuple(sorted(set(value))) or any(
        _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
    ):
        raise ValueError(f"{subject} reason codes must be sorted unique machine codes")


def _require_plan_identity(*, plan_id: str, plan_sha256: str) -> None:
    _require_sha256(plan_sha256, subject="object batch plan")
    if plan_id != f"parent-provision-object-plan-{plan_sha256[:32]}":
        raise ValueError("object batch plan identity drifted")


def _require_plan_and_batch_binding(
    *,
    plan_id: str,
    plan_sha256: str,
    batch_id: str,
    target_scope_sha256: str,
) -> None:
    _require_plan_identity(plan_id=plan_id, plan_sha256=plan_sha256)
    _require_sha256(target_scope_sha256, subject="object batch target scope")
    if batch_id != f"parent-provision-object-batch-{target_scope_sha256[:32]}":
        raise ValueError("object batch target identity drifted")


def _require_sha256(value: str, *, subject: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{subject} SHA-256 is invalid")


__all__ = [
    "PARENT_PROVISION_RECOGNITION_AGGREGATE_EXECUTION_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECOGNITION_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECOGNITION_BATCH_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECOGNITION_SELECTION_POLICY",
    "ParentProvisionRecognitionAggregateExecutionReceiptV2",
    "ParentProvisionRecognitionAggregateStatus",
    "ParentProvisionRecognitionAttemptOutcome",
    "ParentProvisionRecognitionAttemptReceiptV1",
    "ParentProvisionRecognitionBatchReceiptV1",
    "ParentProvisionRecognitionBatchStatus",
    "ParentProvisionRecognitionCleanupStatus",
    "build_parent_provision_recognition_aggregate_execution_receipt",
    "build_parent_provision_recognition_attempt_receipt",
    "build_parent_provision_recognition_batch_receipt",
    "canonical_json_sha256",
    "stable_parent_provision_recognition_attempt_id",
]
