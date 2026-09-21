"""Content-free execution proof for cross-object parent reconciliation.

The model may return semantic uncertainty only through a Host-validated
reconciliation candidate.  Transport failure, rejected output, cancellation,
cleanup drift, and indeterminate runtime state remain execution outcomes and
cannot create semantic-unresolved scope.
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

from reqpilotx_server.application.agent_workflow.parent_provision_batch_receipts import (
    ParentProvisionRecognitionAttemptOutcome,
    ParentProvisionRecognitionCleanupStatus,
)
from reqpilotx_server.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
)


PARENT_PROVISION_RECONCILIATION_ATTEMPT_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-attempt-receipt/v1"
)
PARENT_PROVISION_RECONCILIATION_GROUP_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-group-execution-receipt/v1"
)
PARENT_PROVISION_RECONCILIATION_AGGREGATE_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-object-reconciliation-aggregate-execution-receipt/v1"
)
PARENT_PROVISION_RECONCILIATION_SELECTION_POLICY = "FIRST_HOST_VALIDATED_RETRY/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,119}$")
_GROUP_ID_PATTERN = re.compile(
    r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
)
_BATCH_ID_PATTERN = re.compile(r"^parent-provision-object-batch-[0-9a-f]{32}$")
_OBJECT_ID_PATTERN = re.compile(
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionReconciliationGroupStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_SEMANTIC_UNRESOLVED = "ACCEPTED_WITH_SEMANTIC_UNRESOLVED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ParentProvisionReconciliationAggregateStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL_SEMANTIC = "PARTIAL_SEMANTIC"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    INDETERMINATE = "INDETERMINATE"


class ParentProvisionReconciliationAttemptReceiptV1(_StrictFrozenModel):
    schema_version: Literal[
        "parent-provision-object-reconciliation-attempt-receipt/v1"
    ] = PARENT_PROVISION_RECONCILIATION_ATTEMPT_RECEIPT_SCHEMA_VERSION
    receipt_id: str = Field(
        pattern=r"^parent-provision-reconciliation-attempt-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    attempt_id: str = Field(
        pattern=r"^parent-provision-reconciliation-attempt-[0-9a-f]{32}$"
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_batch_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
    )
    group_order: StrictInt = Field(ge=0, le=8191)
    group_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_batch_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    replacement_logical_object_ids: tuple[str, ...] = Field(
        min_length=1, max_length=8192
    )
    allowed_global_segment_ordinals: tuple[StrictInt, ...] = Field(
        min_length=1, max_length=65536
    )
    replacement_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_global_ordinals_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_ordinal: StrictInt = Field(ge=1, le=64)
    task_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_attempt_id: str | None = Field(
        default=None,
        pattern=r"^parent-provision-reconciliation-attempt-[0-9a-f]{32}$",
    )
    previous_attempt_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    previous_failure_feedback_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    stage_session_attestation_id: str = Field(
        pattern=r"^canonical-session-attestation-[0-9a-f]{32}$"
    )
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_output_execution_mode: Literal["OBJECT_RECONCILIATION"] = (
        "OBJECT_RECONCILIATION"
    )
    runner_output_schema_id: Literal[
        "parent-provision-object-reconciliation-candidate/v1"
    ] = "parent-provision-object-reconciliation-candidate/v1"
    runner_output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_output_schema_variants_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_id: str = Field(min_length=1, max_length=160)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    outcome: ParentProvisionRecognitionAttemptOutcome
    transport_result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    response_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tool_receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime_thread_id_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    runtime_turn_id_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reconciliation_compilation_result_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    compiled_decision_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    group_disposition_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(ge=0, le=65536)
    predecessor_partial: bool
    cleanup_status: ParentProvisionRecognitionCleanupStatus
    workspace_unchanged: bool
    reason_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("replacement_batch_ids")
    @classmethod
    def validate_batch_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _BATCH_ID_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reconciliation replacement batch IDs are invalid")
        return value

    @field_validator("replacement_logical_object_ids")
    @classmethod
    def validate_object_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _OBJECT_ID_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reconciliation replacement object IDs are invalid")
        return value

    @field_validator("allowed_global_segment_ordinals")
    @classmethod
    def validate_ordinals(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(item < 0 for item in value):
            raise ValueError("reconciliation allowed ordinals are invalid")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value)
        return value

    @model_validator(mode="after")
    def validate_attempt(self) -> ParentProvisionReconciliationAttemptReceiptV1:
        _require_group_inventory(
            replacement_batch_ids=self.replacement_batch_ids,
            replacement_logical_object_ids=self.replacement_logical_object_ids,
            allowed_global_segment_ordinals=self.allowed_global_segment_ordinals,
            replacement_inventory_sha256=self.replacement_inventory_sha256,
            allowed_global_ordinals_sha256=self.allowed_global_ordinals_sha256,
        )
        expected_attempt_id = stable_parent_provision_reconciliation_attempt_id(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            reconciliation_plan_id=self.reconciliation_plan_id,
            reconciliation_plan_sha256=self.reconciliation_plan_sha256,
            group_id=self.group_id,
            group_order=self.group_order,
            group_scope_sha256=self.group_scope_sha256,
            attempt_ordinal=self.attempt_ordinal,
            task_binding_sha256=self.task_binding_sha256,
        )
        if self.attempt_id != expected_attempt_id:
            raise ValueError("reconciliation attempt identity drifted")
        previous_fields = (
            self.previous_attempt_id,
            self.previous_attempt_receipt_sha256,
            self.previous_failure_feedback_sha256,
        )
        if self.attempt_ordinal == 1:
            if any(previous_fields):
                raise ValueError("first reconciliation attempt cannot claim feedback")
        elif not all(previous_fields):
            raise ValueError("retried reconciliation attempt requires full feedback")

        completed_transport = (
            self.transport_result_sha256,
            self.response_sha256,
            self.tool_receipt_sha256,
            self.runtime_thread_id_sha256,
            self.runtime_turn_id_sha256,
        )
        compilation_fields = (
            self.reconciliation_compilation_result_sha256,
            self.compiled_decision_sha256,
            self.group_disposition_sha256,
        )
        if self.outcome is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE:
            if not all(
                (*completed_transport, self.candidate_sha256, *compilation_fields)
            ):
                raise ValueError("validated reconciliation attempt proof is incomplete")
            if self.reason_codes:
                raise ValueError(
                    "validated reconciliation attempt cannot carry failure"
                )
            if (
                self.cleanup_status
                is not ParentProvisionRecognitionCleanupStatus.SUCCEEDED
                or not self.workspace_unchanged
            ):
                raise ValueError("validated reconciliation requires clean workspace")
        else:
            if (
                any(compilation_fields)
                or self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                or self.predecessor_partial
            ):
                raise ValueError(
                    "execution failure cannot claim reconciliation or semantic unresolved"
                )
            if not self.reason_codes:
                raise ValueError("failed reconciliation attempt requires a reason")
            if self.outcome is ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED:
                if not all(completed_transport):
                    raise ValueError(
                        "rejected reconciliation output requires transport proof"
                    )
                if (
                    self.cleanup_status
                    is not ParentProvisionRecognitionCleanupStatus.SUCCEEDED
                    or not self.workspace_unchanged
                ):
                    raise ValueError("rejected reconciliation output requires cleanup")
            elif any(completed_transport) or self.candidate_sha256 is not None:
                raise ValueError("incomplete transport cannot claim model output")
        if (
            self.cleanup_status is ParentProvisionRecognitionCleanupStatus.FAILED
            and self.outcome is not ParentProvisionRecognitionAttemptOutcome.BLOCKED
        ):
            raise ValueError("failed cleanup must block reconciliation")
        if (
            self.cleanup_status is ParentProvisionRecognitionCleanupStatus.UNKNOWN
            and self.outcome
            is not ParentProvisionRecognitionAttemptOutcome.INDETERMINATE
        ):
            raise ValueError("unknown cleanup must remain indeterminate")
        if not self.workspace_unchanged and (
            self.outcome is not ParentProvisionRecognitionAttemptOutcome.BLOCKED
            or self.cleanup_status is not ParentProvisionRecognitionCleanupStatus.FAILED
        ):
            raise ValueError("workspace drift must block reconciliation")
        _validate_receipt_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-reconciliation-attempt-receipt-",
        )
        return self


class ParentProvisionReconciliationGroupReceiptV1(_StrictFrozenModel):
    schema_version: Literal[
        "parent-provision-object-reconciliation-group-execution-receipt/v1"
    ] = PARENT_PROVISION_RECONCILIATION_GROUP_RECEIPT_SCHEMA_VERSION
    receipt_id: str = Field(
        pattern=r"^parent-provision-reconciliation-group-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_batch_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    group_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-group-[0-9a-f]{32}$"
    )
    group_order: StrictInt = Field(ge=0, le=8191)
    group_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    replacement_batch_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    replacement_logical_object_ids: tuple[str, ...] = Field(
        min_length=1, max_length=8192
    )
    allowed_global_segment_ordinals: tuple[StrictInt, ...] = Field(
        min_length=1, max_length=65536
    )
    replacement_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    allowed_global_ordinals_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection_policy: Literal["FIRST_HOST_VALIDATED_RETRY/v1"] = (
        PARENT_PROVISION_RECONCILIATION_SELECTION_POLICY
    )
    attempts: tuple[ParentProvisionReconciliationAttemptReceiptV1, ...] = Field(
        min_length=1, max_length=64
    )
    accepted_attempt_id: str | None = Field(
        default=None,
        pattern=r"^parent-provision-reconciliation-attempt-[0-9a-f]{32}$",
    )
    accepted_attempt_receipt_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    status: ParentProvisionReconciliationGroupStatus
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(ge=0, le=65536)
    predecessor_partial: bool
    reconciliation_compilation_result_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    compiled_decision_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    group_disposition_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    reason_codes: tuple[str, ...] = Field(default=(), max_length=64)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value)
        return value

    @model_validator(mode="after")
    def validate_group(self) -> ParentProvisionReconciliationGroupReceiptV1:
        if tuple(item.attempt_ordinal for item in self.attempts) != tuple(
            range(1, len(self.attempts) + 1)
        ):
            raise ValueError("reconciliation attempts must be ordered and contiguous")
        for index, attempt in enumerate(self.attempts):
            if (
                attempt.run_id != self.run_id
                or attempt.source_sha256 != self.source_sha256
                or attempt.predecessor_artifact_id != self.predecessor_artifact_id
                or attempt.predecessor_artifact_sha256
                != self.predecessor_artifact_sha256
                or attempt.kit_bundle_sha256 != self.kit_bundle_sha256
                or attempt.object_batch_plan_id != self.object_batch_plan_id
                or attempt.object_batch_plan_sha256 != self.object_batch_plan_sha256
                or attempt.initial_object_execution_receipt_sha256
                != self.initial_object_execution_receipt_sha256
                or attempt.initial_object_execution_trace_sha256
                != self.initial_object_execution_trace_sha256
                or attempt.initial_object_batch_receipts_sha256
                != self.initial_object_batch_receipts_sha256
                or attempt.reconciliation_plan_id != self.reconciliation_plan_id
                or attempt.reconciliation_plan_sha256 != self.reconciliation_plan_sha256
                or attempt.group_id != self.group_id
                or attempt.group_order != self.group_order
                or attempt.group_scope_sha256 != self.group_scope_sha256
                or attempt.replacement_batch_ids != self.replacement_batch_ids
                or attempt.replacement_logical_object_ids
                != self.replacement_logical_object_ids
                or attempt.allowed_global_segment_ordinals
                != self.allowed_global_segment_ordinals
                or attempt.replacement_inventory_sha256
                != self.replacement_inventory_sha256
                or attempt.allowed_global_ordinals_sha256
                != self.allowed_global_ordinals_sha256
            ):
                raise ValueError("reconciliation attempt lineage drifted from group")
            if index:
                previous = self.attempts[index - 1]
                if (
                    attempt.previous_attempt_id != previous.attempt_id
                    or attempt.previous_attempt_receipt_sha256
                    != previous.receipt_sha256
                    or attempt.previous_failure_feedback_sha256
                    != parent_provision_reconciliation_failure_feedback_sha256(previous)
                ):
                    raise ValueError("reconciliation retry feedback chain drifted")
        if any(
            item.outcome
            in {
                ParentProvisionRecognitionAttemptOutcome.CANCELLED,
                ParentProvisionRecognitionAttemptOutcome.BLOCKED,
                ParentProvisionRecognitionAttemptOutcome.INDETERMINATE,
            }
            for item in self.attempts[:-1]
        ):
            raise ValueError("terminal reconciliation state cannot retry")
        validated = tuple(
            item
            for item in self.attempts
            if item.outcome
            is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE
        )
        final_hashes = (
            self.reconciliation_compilation_result_sha256,
            self.compiled_decision_sha256,
            self.group_disposition_sha256,
        )
        accepted = self.status in {
            ParentProvisionReconciliationGroupStatus.ACCEPTED,
            ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        }
        if accepted:
            if len(validated) != 1 or self.attempts[-1] != validated[0]:
                raise ValueError("group must stop at its first Host-validated attempt")
            selected = validated[0]
            if (
                self.accepted_attempt_id != selected.attempt_id
                or self.accepted_attempt_receipt_sha256 != selected.receipt_sha256
                or self.semantic_unresolved_scope_count
                != selected.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                != selected.semantic_unresolved_entry_count
                or self.predecessor_partial != selected.predecessor_partial
                or final_hashes
                != (
                    selected.reconciliation_compilation_result_sha256,
                    selected.compiled_decision_sha256,
                    selected.group_disposition_sha256,
                )
                or not all(final_hashes)
                or self.reason_codes
            ):
                raise ValueError("accepted reconciliation group proof drifted")
            expected = (
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED
                if (
                    self.semantic_unresolved_scope_count
                    or self.semantic_unresolved_entry_count
                    or self.predecessor_partial
                )
                else ParentProvisionReconciliationGroupStatus.ACCEPTED
            )
            if self.status is not expected:
                raise ValueError("group status does not match semantic disposition")
        else:
            if (
                validated
                or self.accepted_attempt_id
                or self.accepted_attempt_receipt_sha256
            ):
                raise ValueError("failed reconciliation group cannot claim acceptance")
            if (
                any(final_hashes)
                or self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                or self.predecessor_partial
            ):
                raise ValueError(
                    "execution failure cannot claim semantic reconciliation"
                )
            if not self.reason_codes:
                raise ValueError("failed reconciliation group requires a reason")
            if self.status is not _terminal_group_status(self.attempts[-1].outcome):
                raise ValueError("reconciliation group terminal status drifted")
        _validate_receipt_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-reconciliation-group-receipt-",
        )
        return self


class ParentProvisionReconciliationAggregateReceiptV1(_StrictFrozenModel):
    schema_version: Literal[
        "parent-provision-object-reconciliation-aggregate-execution-receipt/v1"
    ] = PARENT_PROVISION_RECONCILIATION_AGGREGATE_RECEIPT_SCHEMA_VERSION
    receipt_id: str = Field(
        pattern=r"^parent-provision-reconciliation-aggregate-receipt-[0-9a-f]{32}$"
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
    stage_session_attestation_id: str = Field(
        pattern=r"^canonical-session-attestation-[0-9a-f]{32}$"
    )
    stage_session_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage_context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_output_execution_mode: Literal["OBJECT_RECONCILIATION"] = (
        "OBJECT_RECONCILIATION"
    )
    runner_output_schema_id: Literal[
        "parent-provision-object-reconciliation-candidate/v1"
    ] = "parent-provision-object-reconciliation-candidate/v1"
    runner_output_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runner_output_schema_variants_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_batch_plan_id: str = Field(
        pattern=r"^parent-provision-object-plan-[0-9a-f]{32}$"
    )
    object_batch_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    initial_object_batch_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reconciliation_plan_id: str = Field(
        pattern=r"^parent-provision-object-reconciliation-plan-[0-9a-f]{32}$"
    )
    reconciliation_plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    planned_group_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    groups_in_completion_order: tuple[
        ParentProvisionReconciliationGroupReceiptV1, ...
    ] = Field(min_length=1, max_length=8192)
    group_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ordered_group_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_group_receipts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    aggregate_execution_trace_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ParentProvisionReconciliationAggregateStatus
    semantic_unresolved_scope_count: StrictInt = Field(ge=0, le=8192)
    semantic_unresolved_entry_count: StrictInt = Field(ge=0, le=65536)
    predecessor_partial: bool
    reconciliation_aggregate_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    global_candidate_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    parent_provision_decision_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    evidence_refs_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reason_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("planned_group_ids")
    @classmethod
    def validate_planned_groups(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _GROUP_ID_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("planned reconciliation group IDs are invalid")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        _require_machine_codes(value)
        return value

    @model_validator(mode="after")
    def validate_aggregate(self) -> ParentProvisionReconciliationAggregateReceiptV1:
        completion_ids = tuple(
            item.group_id for item in self.groups_in_completion_order
        )
        if len(completion_ids) != len(set(completion_ids)) or not set(
            completion_ids
        ).issubset(self.planned_group_ids):
            raise ValueError("reconciliation completion inventory is invalid")
        for group in self.groups_in_completion_order:
            if (
                group.run_id != self.run_id
                or group.source_sha256 != self.source_sha256
                or group.predecessor_artifact_id != self.predecessor_artifact_id
                or group.predecessor_artifact_sha256 != self.predecessor_artifact_sha256
                or group.kit_bundle_sha256 != self.kit_bundle_sha256
                or group.object_batch_plan_id != self.object_batch_plan_id
                or group.object_batch_plan_sha256 != self.object_batch_plan_sha256
                or group.initial_object_execution_receipt_sha256
                != self.initial_object_execution_receipt_sha256
                or group.initial_object_execution_trace_sha256
                != self.initial_object_execution_trace_sha256
                or group.initial_object_batch_receipts_sha256
                != self.initial_object_batch_receipts_sha256
                or group.reconciliation_plan_id != self.reconciliation_plan_id
                or group.reconciliation_plan_sha256 != self.reconciliation_plan_sha256
            ):
                raise ValueError("reconciliation group aggregate lineage drifted")
        completion_hashes = tuple(
            item.receipt_sha256 for item in self.groups_in_completion_order
        )
        source_ordered = tuple(
            item.receipt_sha256
            for item in sorted(
                self.groups_in_completion_order, key=lambda item: item.group_order
            )
        )
        attempts = tuple(
            attempt.receipt_sha256
            for group in self.groups_in_completion_order
            for attempt in group.attempts
        )
        accepted = tuple(
            item.receipt_sha256
            for item in sorted(
                self.groups_in_completion_order, key=lambda item: item.group_order
            )
            if item.status
            in {
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
        )
        if (
            self.group_receipts_sha256 != canonical_json_sha256(completion_hashes)
            or self.source_ordered_group_receipts_sha256
            != canonical_json_sha256(source_ordered)
            or self.attempt_inventory_sha256 != canonical_json_sha256(attempts)
            or self.accepted_group_receipts_sha256 != canonical_json_sha256(accepted)
        ):
            raise ValueError("reconciliation receipt inventory root drifted")
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
                "stage_session_attestation_sha256": self.stage_session_attestation_sha256,
                "stage_context_sha256": self.stage_context_sha256,
                "skill_sha256": self.skill_sha256,
                "runner_output_schema_sha256": self.runner_output_schema_sha256,
                "runner_output_schema_variants_sha256": (
                    self.runner_output_schema_variants_sha256
                ),
                "object_batch_plan_sha256": self.object_batch_plan_sha256,
                "initial_object_execution_receipt_sha256": (
                    self.initial_object_execution_receipt_sha256
                ),
                "initial_object_execution_trace_sha256": (
                    self.initial_object_execution_trace_sha256
                ),
                "initial_object_batch_receipts_sha256": (
                    self.initial_object_batch_receipts_sha256
                ),
                "reconciliation_plan_sha256": self.reconciliation_plan_sha256,
                "planned_group_ids": self.planned_group_ids,
                "group_receipts_sha256": self.group_receipts_sha256,
                "source_ordered_group_receipts_sha256": (
                    self.source_ordered_group_receipts_sha256
                ),
                "attempt_inventory_sha256": self.attempt_inventory_sha256,
                "accepted_group_receipts_sha256": (self.accepted_group_receipts_sha256),
            }
        )
        if self.aggregate_execution_trace_sha256 != expected_trace:
            raise ValueError("reconciliation aggregate execution trace drifted")

        nonaccepted = tuple(
            item
            for item in self.groups_in_completion_order
            if item.status
            not in {
                ParentProvisionReconciliationGroupStatus.ACCEPTED,
                ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
            }
        )
        final_hashes = (
            self.reconciliation_aggregate_sha256,
            self.global_candidate_sha256,
            self.parent_provision_decision_sha256,
            self.evidence_refs_sha256,
        )
        success = self.status in {
            ParentProvisionReconciliationAggregateStatus.COMPLETE,
            ParentProvisionReconciliationAggregateStatus.PARTIAL_SEMANTIC,
        }
        if success:
            if (
                nonaccepted
                or set(completion_ids) != set(self.planned_group_ids)
                or len(completion_ids) != len(self.planned_group_ids)
                or not all(final_hashes)
                or self.reason_codes
            ):
                raise ValueError(
                    "successful reconciliation aggregate proof is incomplete"
                )
            expected_unresolved = sum(
                item.semantic_unresolved_scope_count
                for item in self.groups_in_completion_order
            )
            expected_unresolved_entries = sum(
                item.semantic_unresolved_entry_count
                for item in self.groups_in_completion_order
            )
            expected_predecessor_partial = any(
                item.predecessor_partial for item in self.groups_in_completion_order
            )
            expected_status = (
                ParentProvisionReconciliationAggregateStatus.PARTIAL_SEMANTIC
                if (
                    expected_unresolved
                    or expected_unresolved_entries
                    or expected_predecessor_partial
                )
                else ParentProvisionReconciliationAggregateStatus.COMPLETE
            )
            if (
                self.semantic_unresolved_scope_count != expected_unresolved
                or self.semantic_unresolved_entry_count != expected_unresolved_entries
                or self.predecessor_partial != expected_predecessor_partial
                or self.status is not expected_status
            ):
                raise ValueError("reconciliation aggregate semantic status drifted")
        else:
            if (
                not nonaccepted
                or any(final_hashes)
                or self.semantic_unresolved_scope_count
                or self.semantic_unresolved_entry_count
                or self.predecessor_partial
            ):
                raise ValueError(
                    "failed reconciliation aggregate cannot claim final semantics"
                )
            if not self.reason_codes:
                raise ValueError("failed reconciliation aggregate requires a reason")
            expected_status = _terminal_aggregate_status(
                tuple(item.status for item in nonaccepted)
            )
            if self.status is not expected_status:
                raise ValueError("reconciliation aggregate terminal status drifted")
        _validate_receipt_identity(
            self,
            id_field="receipt_id",
            sha_field="receipt_sha256",
            id_prefix="parent-provision-reconciliation-aggregate-receipt-",
        )
        return self

    @property
    def reconciliation_execution_receipts_sha256(self) -> str:
        """Root consumed by the professional finalized execution receipt."""

        return self.receipt_sha256


def stable_parent_provision_reconciliation_attempt_id(
    *,
    run_id: str,
    source_sha256: str,
    reconciliation_plan_id: str,
    reconciliation_plan_sha256: str,
    group_id: str,
    group_order: int,
    group_scope_sha256: str,
    attempt_ordinal: int,
    task_binding_sha256: str,
) -> str:
    payload = {
        "run_id": run_id,
        "source_sha256": source_sha256,
        "reconciliation_plan_id": reconciliation_plan_id,
        "reconciliation_plan_sha256": reconciliation_plan_sha256,
        "group_id": group_id,
        "group_order": group_order,
        "group_scope_sha256": group_scope_sha256,
        "attempt_ordinal": attempt_ordinal,
        "task_binding_sha256": task_binding_sha256,
    }
    digest = canonical_json_sha256(payload)
    return f"parent-provision-reconciliation-attempt-{digest[:32]}"


def parent_provision_reconciliation_failure_feedback(
    previous: ParentProvisionReconciliationAttemptReceiptV1,
) -> dict[str, object]:
    if previous.outcome is ParentProvisionRecognitionAttemptOutcome.VALIDATED_CANDIDATE:
        raise ValueError(
            "validated reconciliation attempt cannot become retry feedback"
        )
    return {
        "attempt_id": previous.attempt_id,
        "attempt_receipt_sha256": previous.receipt_sha256,
        "task_binding_sha256": previous.task_binding_sha256,
        "outcome": previous.outcome.value,
        "reason_codes": previous.reason_codes,
        "candidate_sha256": previous.candidate_sha256,
        "transport_result_sha256": previous.transport_result_sha256,
        "cleanup_status": previous.cleanup_status.value,
        "workspace_unchanged": previous.workspace_unchanged,
    }


def parent_provision_reconciliation_failure_feedback_sha256(
    previous: ParentProvisionReconciliationAttemptReceiptV1,
) -> str:
    return canonical_json_sha256(
        parent_provision_reconciliation_failure_feedback(previous)
    )


def build_parent_provision_reconciliation_attempt_receipt(
    **values: Any,
) -> ParentProvisionReconciliationAttemptReceiptV1:
    values = dict(values)
    values.setdefault("content_free", True)
    values.setdefault("reason_codes", ())
    values["attempt_id"] = stable_parent_provision_reconciliation_attempt_id(
        run_id=values["run_id"],
        source_sha256=values["source_sha256"],
        reconciliation_plan_id=values["reconciliation_plan_id"],
        reconciliation_plan_sha256=values["reconciliation_plan_sha256"],
        group_id=values["group_id"],
        group_order=values["group_order"],
        group_scope_sha256=values["group_scope_sha256"],
        attempt_ordinal=values["attempt_ordinal"],
        task_binding_sha256=values["task_binding_sha256"],
    )
    return _build_receipt(
        ParentProvisionReconciliationAttemptReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-reconciliation-attempt-receipt-",
    )


def build_parent_provision_reconciliation_group_receipt(
    **values: Any,
) -> ParentProvisionReconciliationGroupReceiptV1:
    values = dict(values)
    values.setdefault("content_free", True)
    values.setdefault(
        "selection_policy", PARENT_PROVISION_RECONCILIATION_SELECTION_POLICY
    )
    values.setdefault("reason_codes", ())
    return _build_receipt(
        ParentProvisionReconciliationGroupReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-reconciliation-group-receipt-",
    )


def build_parent_provision_reconciliation_aggregate_receipt(
    **values: Any,
) -> ParentProvisionReconciliationAggregateReceiptV1:
    values = dict(values)
    values.setdefault("content_free", True)
    values.setdefault("reason_codes", ())
    groups = tuple(values["groups_in_completion_order"])
    values["groups_in_completion_order"] = groups
    completion_hashes = tuple(item.receipt_sha256 for item in groups)
    source_ordered = tuple(
        item.receipt_sha256
        for item in sorted(groups, key=lambda item: item.group_order)
    )
    attempts = tuple(
        attempt.receipt_sha256 for group in groups for attempt in group.attempts
    )
    accepted = tuple(
        item.receipt_sha256
        for item in sorted(groups, key=lambda item: item.group_order)
        if item.status
        in {
            ParentProvisionReconciliationGroupStatus.ACCEPTED,
            ParentProvisionReconciliationGroupStatus.ACCEPTED_WITH_SEMANTIC_UNRESOLVED,
        }
    )
    values["group_receipts_sha256"] = canonical_json_sha256(completion_hashes)
    values["source_ordered_group_receipts_sha256"] = canonical_json_sha256(
        source_ordered
    )
    values["attempt_inventory_sha256"] = canonical_json_sha256(attempts)
    values["accepted_group_receipts_sha256"] = canonical_json_sha256(accepted)
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
            "stage_session_attestation_sha256": values[
                "stage_session_attestation_sha256"
            ],
            "stage_context_sha256": values["stage_context_sha256"],
            "skill_sha256": values["skill_sha256"],
            "runner_output_schema_sha256": values["runner_output_schema_sha256"],
            "runner_output_schema_variants_sha256": values[
                "runner_output_schema_variants_sha256"
            ],
            "object_batch_plan_sha256": values["object_batch_plan_sha256"],
            "initial_object_execution_receipt_sha256": values[
                "initial_object_execution_receipt_sha256"
            ],
            "initial_object_execution_trace_sha256": values[
                "initial_object_execution_trace_sha256"
            ],
            "initial_object_batch_receipts_sha256": values[
                "initial_object_batch_receipts_sha256"
            ],
            "reconciliation_plan_sha256": values["reconciliation_plan_sha256"],
            "planned_group_ids": values["planned_group_ids"],
            "group_receipts_sha256": values["group_receipts_sha256"],
            "source_ordered_group_receipts_sha256": values[
                "source_ordered_group_receipts_sha256"
            ],
            "attempt_inventory_sha256": values["attempt_inventory_sha256"],
            "accepted_group_receipts_sha256": values["accepted_group_receipts_sha256"],
        }
    )
    return _build_receipt(
        ParentProvisionReconciliationAggregateReceiptV1,
        values,
        id_field="receipt_id",
        sha_field="receipt_sha256",
        id_prefix="parent-provision-reconciliation-aggregate-receipt-",
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


def _require_group_inventory(
    *,
    replacement_batch_ids: tuple[str, ...],
    replacement_logical_object_ids: tuple[str, ...],
    allowed_global_segment_ordinals: tuple[int, ...],
    replacement_inventory_sha256: str,
    allowed_global_ordinals_sha256: str,
) -> None:
    if len(replacement_batch_ids) != len(replacement_logical_object_ids):
        raise ValueError("reconciliation replacement inventory is incomplete")
    expected_replacement = canonical_json_sha256(
        tuple(zip(replacement_batch_ids, replacement_logical_object_ids, strict=True))
    )
    if replacement_inventory_sha256 != expected_replacement:
        raise ValueError("reconciliation replacement inventory root drifted")
    if allowed_global_ordinals_sha256 != canonical_json_sha256(
        allowed_global_segment_ordinals
    ):
        raise ValueError("reconciliation allowed ordinal root drifted")


def _terminal_group_status(
    outcome: ParentProvisionRecognitionAttemptOutcome,
) -> ParentProvisionReconciliationGroupStatus:
    mapping = {
        ParentProvisionRecognitionAttemptOutcome.TRANSPORT_FAILED: (
            ParentProvisionReconciliationGroupStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.OUTPUT_REJECTED: (
            ParentProvisionReconciliationGroupStatus.FAILED
        ),
        ParentProvisionRecognitionAttemptOutcome.CANCELLED: (
            ParentProvisionReconciliationGroupStatus.CANCELLED
        ),
        ParentProvisionRecognitionAttemptOutcome.BLOCKED: (
            ParentProvisionReconciliationGroupStatus.BLOCKED
        ),
        ParentProvisionRecognitionAttemptOutcome.INDETERMINATE: (
            ParentProvisionReconciliationGroupStatus.INDETERMINATE
        ),
    }
    try:
        return mapping[outcome]
    except KeyError as exc:
        raise ValueError("validated attempt cannot terminate a failed group") from exc


def _terminal_aggregate_status(
    statuses: tuple[ParentProvisionReconciliationGroupStatus, ...],
) -> ParentProvisionReconciliationAggregateStatus:
    if ParentProvisionReconciliationGroupStatus.INDETERMINATE in statuses:
        return ParentProvisionReconciliationAggregateStatus.INDETERMINATE
    if ParentProvisionReconciliationGroupStatus.BLOCKED in statuses:
        return ParentProvisionReconciliationAggregateStatus.BLOCKED
    if ParentProvisionReconciliationGroupStatus.CANCELLED in statuses:
        return ParentProvisionReconciliationAggregateStatus.CANCELLED
    return ParentProvisionReconciliationAggregateStatus.FAILED


def _require_machine_codes(value: tuple[str, ...]) -> None:
    if value != tuple(sorted(set(value))) or any(
        _MACHINE_CODE_PATTERN.fullmatch(item) is None for item in value
    ):
        raise ValueError("reconciliation reasons must be sorted machine codes")


def _build_receipt(
    model: type[_StrictFrozenModel],
    values: dict[str, Any],
    *,
    id_field: str,
    sha_field: str,
    id_prefix: str,
) -> Any:
    values = dict(values)
    values[id_field] = id_prefix + "0" * 32
    values[sha_field] = "0" * 64
    draft = model.model_construct(**values)
    payload = draft.model_dump(mode="json", exclude={id_field, sha_field})
    digest = canonical_json_sha256(payload)
    values[id_field] = id_prefix + digest[:32]
    values[sha_field] = digest
    return model.model_validate(values)


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
        raise ValueError("reconciliation receipt SHA drifted")
    if getattr(receipt, id_field) != id_prefix + expected[:32]:
        raise ValueError("reconciliation receipt identity drifted")


__all__ = [
    "PARENT_PROVISION_RECONCILIATION_AGGREGATE_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECONCILIATION_ATTEMPT_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECONCILIATION_GROUP_RECEIPT_SCHEMA_VERSION",
    "PARENT_PROVISION_RECONCILIATION_SELECTION_POLICY",
    "ParentProvisionReconciliationAggregateReceiptV1",
    "ParentProvisionReconciliationAggregateStatus",
    "ParentProvisionReconciliationAttemptReceiptV1",
    "ParentProvisionReconciliationGroupReceiptV1",
    "ParentProvisionReconciliationGroupStatus",
    "build_parent_provision_reconciliation_aggregate_receipt",
    "build_parent_provision_reconciliation_attempt_receipt",
    "build_parent_provision_reconciliation_group_receipt",
    "canonical_json_sha256",
    "parent_provision_reconciliation_failure_feedback",
    "parent_provision_reconciliation_failure_feedback_sha256",
    "stable_parent_provision_reconciliation_attempt_id",
]
