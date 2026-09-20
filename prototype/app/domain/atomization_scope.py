from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ATOMIZATION_SCOPE_RECEIPT_SCHEMA_VERSION = "atomization-scope-receipt/v1"
ATOMIZATION_SCOPE_ATTEMPT_SCHEMA_VERSION = "atomization-scope-attempt/v1"
ATOMIZATION_SCOPE_ALGORITHM_VERSION = "explicit-source-bound-scope/v1"
ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS = 60 * 60

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RECEIPT_ID_RE = re.compile(r"^atomization-scope-receipt-[0-9a-f]{32}$")
_ATTEMPT_ID_RE = re.compile(r"^atomization-scope-attempt-[0-9a-f]{32}$")
_COMMAND_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


class AtomizationScopeOperation(str, Enum):
    APPLY = "APPLY"
    UNDO = "UNDO"


class AtomizationTargetScope(str, Enum):
    CURRENT = "CURRENT"
    SELECTED = "SELECTED"
    BRANCH = "BRANCH"
    ALL = "ALL"


class AtomizationUnselectedPolicy(str, Enum):
    SOURCE_BOUND_PASSTHROUGH = "SOURCE_BOUND_PASSTHROUGH"


class AtomizationScopeSkipReason(str, Enum):
    UNCLASSIFIED = "UNCLASSIFIED"
    NON_TECHNICAL = "NON_TECHNICAL"
    INFORMATION = "INFORMATION"
    NOT_INCLUDED = "NOT_INCLUDED"
    NOT_REQUIREMENT = "NOT_REQUIREMENT"
    NOT_DIRECT = "NOT_DIRECT"


class AtomizationScopeAttemptStatus(str, Enum):
    RUNNING = "RUNNING"
    INTERRUPTED = "INTERRUPTED"
    SUCCEEDED = "SUCCEEDED"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AtomizationScopeSkippedTargetV1(_FrozenModel):
    review_node_id: str = Field(min_length=1, max_length=260)
    reason: AtomizationScopeSkipReason


class AtomizationScopeAttemptV1(_FrozenModel):
    """Append-only execution evidence for one explicit scope attempt."""

    schema_version: Literal["atomization-scope-attempt/v1"] = (
        ATOMIZATION_SCOPE_ATTEMPT_SCHEMA_VERSION
    )
    attempt_id: str
    payload_sha256: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(pattern=r"^llm-review-[0-9a-f]{32}$")
    review_revision: int = Field(ge=0)
    lineage_sha256: str
    attempt_sequence: int = Field(ge=1)
    command_id: str
    command_sha256: str
    expected_scope_revision: int = Field(ge=0)
    operation: AtomizationScopeOperation
    scope: AtomizationTargetScope
    status: AtomizationScopeAttemptStatus
    requested_review_node_ids: tuple[str, ...]
    executed_review_node_ids: tuple[str, ...]
    skipped_targets: tuple[AtomizationScopeSkippedTargetV1, ...]
    cumulative_review_node_ids: tuple[str, ...]
    completed_review_node_ids: tuple[str, ...]
    failed_review_node_ids: tuple[str, ...]
    checkpoint_created_review_node_ids: tuple[str, ...]
    checkpoint_reused_review_node_ids: tuple[str, ...]
    checkpoint_invalidated_review_node_ids: tuple[str, ...]
    provider_call_count: int = Field(ge=0)
    retryable: bool
    checkpoint_resumable: bool
    error_code: str = Field(default="", max_length=160)
    atomization_run_id: str = Field(default="", max_length=80)
    atomization_run_revision: int = Field(default=0, ge=0)
    receipt_id: str = Field(default="", max_length=80)
    started_at_utc: datetime
    updated_at_utc: datetime

    @field_validator("payload_sha256", "lineage_sha256", "command_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("scope attempt hashes must be lowercase SHA-256")
        return value

    @field_validator("attempt_id")
    @classmethod
    def validate_attempt_id(cls, value: str) -> str:
        if not _ATTEMPT_ID_RE.fullmatch(value):
            raise ValueError("scope attempt ID is invalid")
        return value

    @field_validator("command_id")
    @classmethod
    def validate_command_id(cls, value: str) -> str:
        if not _COMMAND_ID_RE.fullmatch(value):
            raise ValueError("scope command ID is invalid")
        return value

    @field_validator("atomization_run_id")
    @classmethod
    def validate_atomization_run_id(cls, value: str) -> str:
        if value and not re.fullmatch(r"^atomization-run-[0-9a-f]{32}$", value):
            raise ValueError("scope attempt atomization run ID is invalid")
        return value

    @field_validator("receipt_id")
    @classmethod
    def validate_receipt_id(cls, value: str) -> str:
        if value and not _RECEIPT_ID_RE.fullmatch(value):
            raise ValueError("scope attempt receipt ID is invalid")
        return value

    @model_validator(mode="after")
    def validate_attempt(self) -> AtomizationScopeAttemptV1:
        if self.started_at_utc.tzinfo is None or self.updated_at_utc.tzinfo is None:
            raise ValueError("scope attempt timestamps must be timezone-aware")
        if self.updated_at_utc < self.started_at_utc:
            raise ValueError("scope attempt update cannot predate its start")
        collections = (
            self.requested_review_node_ids,
            self.executed_review_node_ids,
            self.cumulative_review_node_ids,
            self.completed_review_node_ids,
            self.failed_review_node_ids,
            self.checkpoint_created_review_node_ids,
            self.checkpoint_reused_review_node_ids,
            self.checkpoint_invalidated_review_node_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("scope attempt node ID collections must be unique")
        skipped_ids = tuple(item.review_node_id for item in self.skipped_targets)
        if len(skipped_ids) != len(set(skipped_ids)):
            raise ValueError("scope attempt skipped targets must be unique")
        requested = set(self.requested_review_node_ids)
        executed = set(self.executed_review_node_ids)
        skipped = set(skipped_ids)
        if executed & skipped or executed | skipped != requested:
            raise ValueError("executed and skipped targets must partition the request")
        completed = set(self.completed_review_node_ids)
        failed = set(self.failed_review_node_ids)
        if completed & failed:
            raise ValueError("completed and failed targets must not overlap")
        if not failed.issubset(executed):
            raise ValueError("failed targets must be current executed targets")
        if not set(self.checkpoint_created_review_node_ids).issubset(completed):
            raise ValueError("created checkpoints must be completed targets")
        if not set(self.checkpoint_reused_review_node_ids).issubset(
            set(self.cumulative_review_node_ids)
        ):
            raise ValueError("reused checkpoints must be cumulative targets")
        if self.operation is AtomizationScopeOperation.UNDO and any(
            (
                self.requested_review_node_ids,
                self.executed_review_node_ids,
                self.skipped_targets,
            )
        ):
            raise ValueError("UNDO attempts cannot carry request targets")
        if self.status is AtomizationScopeAttemptStatus.RUNNING:
            if any(
                (
                    self.completed_review_node_ids,
                    self.failed_review_node_ids,
                    self.checkpoint_created_review_node_ids,
                    self.checkpoint_reused_review_node_ids,
                    self.checkpoint_invalidated_review_node_ids,
                    self.provider_call_count,
                    self.retryable,
                    self.checkpoint_resumable,
                    self.error_code,
                    self.atomization_run_id,
                    self.atomization_run_revision,
                    self.receipt_id,
                )
            ):
                raise ValueError(
                    "RUNNING scope attempts cannot contain terminal output"
                )
        elif self.status is AtomizationScopeAttemptStatus.INTERRUPTED:
            if not self.error_code:
                raise ValueError("INTERRUPTED scope attempts require an error code")
            if (
                self.atomization_run_id
                or self.atomization_run_revision
                or self.receipt_id
            ):
                raise ValueError(
                    "INTERRUPTED scope attempts cannot bind committed output"
                )
            if self.checkpoint_resumable != bool(self.completed_review_node_ids):
                raise ValueError(
                    "checkpoint_resumable must reflect completed checkpoints"
                )
        else:
            if self.error_code or self.retryable or self.checkpoint_resumable:
                raise ValueError("SUCCEEDED scope attempts cannot be retryable")
            if not self.atomization_run_id or not self.receipt_id:
                raise ValueError("SUCCEEDED scope attempts must bind committed output")
            if self.atomization_run_revision != self.expected_scope_revision + 1:
                raise ValueError("SUCCEEDED scope attempt revision is invalid")
        expected_id = stable_atomization_scope_attempt_id(
            lineage_sha256=self.lineage_sha256,
            attempt_sequence=self.attempt_sequence,
            command_sha256=self.command_sha256,
        )
        if self.attempt_id != expected_id:
            raise ValueError("scope attempt ID does not match immutable binding")
        if self.payload_sha256 != canonical_atomization_scope_attempt_sha256(self):
            raise ValueError("scope attempt payload hash does not match")
        return self


class AtomizationScopeReceiptV1(_FrozenModel):
    schema_version: Literal["atomization-scope-receipt/v1"] = (
        ATOMIZATION_SCOPE_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str
    payload_sha256: str
    project_id: str = Field(min_length=1, max_length=160)
    document_id: str = Field(min_length=1, max_length=160)
    session_id: str = Field(pattern=r"^llm-review-[0-9a-f]{32}$")
    review_revision: int = Field(ge=0)
    lineage_sha256: str
    entry_classification_snapshot_id: str = Field(min_length=1, max_length=200)
    entry_classification_revision: int = Field(ge=0)
    entry_classification_sha256: str
    entry_classification_content_sha256: str
    scope_revision: int = Field(ge=1)
    active_revision: int = Field(ge=0)
    previous_active_revision: int = Field(ge=0)
    supersedes_receipt_id: str = Field(default="", max_length=80)
    command_id: str
    command_sha256: str
    operation: AtomizationScopeOperation
    scope: AtomizationTargetScope
    target_scope_sha256: str
    unselected_policy: AtomizationUnselectedPolicy = (
        AtomizationUnselectedPolicy.SOURCE_BOUND_PASSTHROUGH
    )
    requested_review_node_ids: tuple[str, ...]
    executed_review_node_ids: tuple[str, ...]
    new_review_node_ids: tuple[str, ...]
    reused_review_node_ids: tuple[str, ...]
    skipped_targets: tuple[AtomizationScopeSkippedTargetV1, ...]
    succeeded_review_node_ids: tuple[str, ...]
    failed_review_node_ids: tuple[str, ...]
    pass_through_review_node_ids: tuple[str, ...]
    cumulative_review_node_ids: tuple[str, ...]
    provider_call_count: int = Field(ge=0)
    checkpoint_created_count: int = Field(ge=0)
    checkpoint_reuse_count: int = Field(ge=0)
    resumable: bool
    atomization_run_id: str = Field(pattern=r"^atomization-run-[0-9a-f]{32}$")
    atomization_run_revision: int = Field(ge=1)
    created_at_utc: datetime

    @field_validator(
        "payload_sha256",
        "lineage_sha256",
        "entry_classification_sha256",
        "entry_classification_content_sha256",
        "command_sha256",
        "target_scope_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("scope receipt hashes must be lowercase SHA-256")
        return value

    @field_validator("receipt_id")
    @classmethod
    def validate_receipt_id(cls, value: str) -> str:
        if not _RECEIPT_ID_RE.fullmatch(value):
            raise ValueError("scope receipt ID is invalid")
        return value

    @field_validator("supersedes_receipt_id")
    @classmethod
    def validate_supersedes_receipt_id(cls, value: str) -> str:
        if value and not _RECEIPT_ID_RE.fullmatch(value):
            raise ValueError("superseded scope receipt ID is invalid")
        return value

    @field_validator("command_id")
    @classmethod
    def validate_command_id(cls, value: str) -> str:
        if not _COMMAND_ID_RE.fullmatch(value):
            raise ValueError("scope command ID is invalid")
        return value

    @model_validator(mode="after")
    def validate_receipt(self) -> AtomizationScopeReceiptV1:
        if self.created_at_utc.tzinfo is None:
            raise ValueError("scope receipt timestamp must be timezone-aware")
        collections = (
            self.requested_review_node_ids,
            self.executed_review_node_ids,
            self.new_review_node_ids,
            self.reused_review_node_ids,
            self.succeeded_review_node_ids,
            self.failed_review_node_ids,
            self.pass_through_review_node_ids,
            self.cumulative_review_node_ids,
        )
        if any(len(values) != len(set(values)) for values in collections):
            raise ValueError("scope receipt node ID collections must be unique")
        skipped_ids = tuple(item.review_node_id for item in self.skipped_targets)
        if len(skipped_ids) != len(set(skipped_ids)):
            raise ValueError("scope receipt skipped targets must be unique")
        executed = set(self.executed_review_node_ids)
        requested = set(self.requested_review_node_ids)
        skipped = set(skipped_ids)
        if executed & skipped or executed | skipped != requested:
            raise ValueError("executed and skipped targets must partition the request")
        if set(self.new_review_node_ids) | set(self.reused_review_node_ids) != executed:
            raise ValueError("new and reused targets must partition executed targets")
        if set(self.new_review_node_ids) & set(self.reused_review_node_ids):
            raise ValueError("new and reused targets must not overlap")
        if not set(self.succeeded_review_node_ids).issubset(executed):
            raise ValueError("succeeded targets must be executed targets")
        if not set(self.failed_review_node_ids).issubset(executed):
            raise ValueError("failed targets must be executed targets")
        if (
            set(self.succeeded_review_node_ids) & set(self.failed_review_node_ids)
            or set(self.succeeded_review_node_ids) | set(self.failed_review_node_ids)
            != executed
        ):
            raise ValueError("succeeded and failed targets must partition execution")
        if set(self.pass_through_review_node_ids) & set(
            self.cumulative_review_node_ids
        ):
            raise ValueError("pass-through and cumulative targets must not overlap")
        if self.operation is AtomizationScopeOperation.UNDO and any(
            (
                self.requested_review_node_ids,
                self.executed_review_node_ids,
                self.new_review_node_ids,
                self.reused_review_node_ids,
                self.skipped_targets,
            )
        ):
            raise ValueError("UNDO receipts cannot carry request targets")
        if self.active_revision > self.scope_revision:
            raise ValueError("active scope revision cannot be in the future")
        if self.previous_active_revision > self.scope_revision:
            raise ValueError("previous active revision cannot be in the future")
        if self.atomization_run_revision != self.scope_revision:
            raise ValueError("scope and atomization run revisions must match")
        if self.payload_sha256 != canonical_atomization_scope_receipt_sha256(self):
            raise ValueError("scope receipt payload hash does not match")
        expected_id = stable_atomization_scope_receipt_id(
            lineage_sha256=self.lineage_sha256,
            scope_revision=self.scope_revision,
            command_sha256=self.command_sha256,
            atomization_run_id=self.atomization_run_id,
        )
        if self.receipt_id != expected_id:
            raise ValueError("scope receipt ID does not match immutable binding")
        return self


def stable_atomization_target_scope_sha256(
    *,
    project_id: str,
    document_id: str,
    session_id: str,
    command_id: str,
    operation: AtomizationScopeOperation | str,
    scope: AtomizationTargetScope | str,
    expected_review_revision: int,
    expected_scope_revision: int,
    expected_entry_classification_snapshot_id: str,
    expected_entry_classification_revision: int,
    expected_entry_classification_sha256: str,
    expected_working_tree_id: str,
    expected_working_tree_sha256: str,
    expected_effective_structure_id: str,
    anchor_tree_node_id: str | None,
    target_review_node_ids: tuple[str, ...],
    unselected_policy: AtomizationUnselectedPolicy | str,
) -> str:
    return _sha256_json(
        {
            "schema_version": "atomization-target-scope/v2",
            "project_id": project_id,
            "document_id": document_id,
            "session_id": session_id,
            "command_id": command_id,
            "operation": AtomizationScopeOperation(operation).value,
            "scope": AtomizationTargetScope(scope).value,
            "expected_review_revision": expected_review_revision,
            "expected_scope_revision": expected_scope_revision,
            "expected_entry_classification_snapshot_id": (
                expected_entry_classification_snapshot_id
            ),
            "expected_entry_classification_revision": (
                expected_entry_classification_revision
            ),
            "expected_entry_classification_sha256": (
                expected_entry_classification_sha256
            ),
            "expected_working_tree_id": expected_working_tree_id,
            "expected_working_tree_sha256": expected_working_tree_sha256,
            "expected_effective_structure_id": expected_effective_structure_id,
            "anchor_tree_node_id": anchor_tree_node_id,
            "target_review_node_ids": list(target_review_node_ids),
            "unselected_policy": AtomizationUnselectedPolicy(unselected_policy).value,
        }
    )


def stable_atomization_scope_receipt_id(
    *,
    lineage_sha256: str,
    scope_revision: int,
    command_sha256: str,
    atomization_run_id: str,
) -> str:
    digest = _sha256_json(
        {
            "lineage_sha256": lineage_sha256,
            "scope_revision": scope_revision,
            "command_sha256": command_sha256,
            "atomization_run_id": atomization_run_id,
        }
    )
    return f"atomization-scope-receipt-{digest[:32]}"


def stable_atomization_scope_attempt_id(
    *,
    lineage_sha256: str,
    attempt_sequence: int,
    command_sha256: str,
) -> str:
    digest = _sha256_json(
        {
            "lineage_sha256": lineage_sha256,
            "attempt_sequence": attempt_sequence,
            "command_sha256": command_sha256,
        }
    )
    return f"atomization-scope-attempt-{digest[:32]}"


def stable_effective_entry_classification_sha256(snapshot: object) -> str:
    entries = getattr(snapshot, "entries", None)
    if not isinstance(entries, tuple):
        raise TypeError("classification snapshot entries are invalid")
    return _sha256_json(
        {
            "schema_version": "effective-entry-classification-content/v1",
            "entries": [
                {
                    "workflow_node_id": item.workflow_node_id,
                    "effective_type": (
                        item.effective_type.value
                        if item.effective_type is not None
                        else None
                    ),
                }
                for item in entries
            ],
        }
    )


def canonical_atomization_scope_receipt_sha256(
    receipt: AtomizationScopeReceiptV1,
) -> str:
    return _sha256_json(receipt.model_dump(mode="json", exclude={"payload_sha256"}))


def canonical_atomization_scope_attempt_sha256(
    attempt: AtomizationScopeAttemptV1,
) -> str:
    return _sha256_json(attempt.model_dump(mode="json", exclude={"payload_sha256"}))


def _sha256_json(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "ATOMIZATION_SCOPE_ALGORITHM_VERSION",
    "ATOMIZATION_SCOPE_ATTEMPT_SCHEMA_VERSION",
    "ATOMIZATION_SCOPE_RECEIPT_SCHEMA_VERSION",
    "ATOMIZATION_SCOPE_RUNNING_ATTEMPT_STALE_AFTER_SECONDS",
    "AtomizationScopeAttemptStatus",
    "AtomizationScopeAttemptV1",
    "AtomizationScopeOperation",
    "AtomizationScopeReceiptV1",
    "AtomizationScopeSkipReason",
    "AtomizationScopeSkippedTargetV1",
    "AtomizationTargetScope",
    "AtomizationUnselectedPolicy",
    "canonical_atomization_scope_attempt_sha256",
    "canonical_atomization_scope_receipt_sha256",
    "stable_atomization_scope_attempt_id",
    "stable_atomization_scope_receipt_id",
    "stable_atomization_target_scope_sha256",
    "stable_effective_entry_classification_sha256",
]
