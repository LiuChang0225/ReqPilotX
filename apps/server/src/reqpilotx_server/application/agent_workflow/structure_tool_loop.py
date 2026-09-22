from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)


STRUCTURE_HANDOFF_ATTEMPT_REQUEST_SCHEMA_VERSION = (
    "native-structure-handoff-attempt-request/v1"
)
STRUCTURE_HANDOFF_ATTEMPT_RECEIPT_SCHEMA_VERSION = (
    "native-structure-handoff-attempt-receipt/v1"
)
STRUCTURE_HOST_TOOL_REQUEST_SCHEMA_VERSION = "native-structure-host-tool-request/v1"
STRUCTURE_HOST_TOOL_RECEIPT_SCHEMA_VERSION = "native-structure-host-tool-receipt/v1"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureToolIntent(StrEnum):
    READ_NATIVE_TEXT = "READ_NATIVE_TEXT"
    RENDER_SCOPE = "RENDER_SCOPE"
    INSPECT_LOGICAL_TABLE = "INSPECT_LOGICAL_TABLE"
    RESTORE_TABLE_CANDIDATE = "RESTORE_TABLE_CANDIDATE"
    VALIDATE_CONTENT_OWNERSHIP = "VALIDATE_CONTENT_OWNERSHIP"
    VALIDATE_MATERIALIZATION = "VALIDATE_MATERIALIZATION"
    VALIDATE_STRUCTURE_GRANULARITY = "VALIDATE_STRUCTURE_GRANULARITY"


class StructureToolOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNAVAILABLE = "UNAVAILABLE"


class StructureToolResultRole(StrEnum):
    SOURCE_EVIDENCE = "SOURCE_EVIDENCE"
    ADVISORY_CANDIDATE = "ADVISORY_CANDIDATE"
    MATERIALIZED_OUTPUT = "MATERIALIZED_OUTPUT"
    VALIDATION_REPORT = "VALIDATION_REPORT"


class StructureRevisionBasisRole(StrEnum):
    ADVISORY_CANDIDATE = "ADVISORY_CANDIDATE"
    CURRENT_MATERIALIZATION = "CURRENT_MATERIALIZATION"
    PREVIOUS_MATERIALIZATION = "PREVIOUS_MATERIALIZATION"


class StructureHandoffAttemptStatus(StrEnum):
    VERIFIED = "VERIFIED"
    VERIFICATION_REQUIRED = "VERIFICATION_REQUIRED"
    REVISION_REQUIRED = "REVISION_REQUIRED"


class StructureProjectionOperationKind(StrEnum):
    COLLAPSE_NAVIGATION_CHILDREN = "COLLAPSE_NAVIGATION_CHILDREN"
    EXCLUDE_TABLE_OWNED_DIRECT_CONTENT = "EXCLUDE_TABLE_OWNED_DIRECT_CONTENT"
    COLLAPSE_REPEATED_SOURCE_LINES = "COLLAPSE_REPEATED_SOURCE_LINES"
    USE_VISUAL_TABLE_REGION = "USE_VISUAL_TABLE_REGION"
    SET_CANONICAL_TABLE_COLUMNS = "SET_CANONICAL_TABLE_COLUMNS"
    PLACE_FLOATING_TABLE_IMAGE = "PLACE_FLOATING_TABLE_IMAGE"
    PRESERVE_SOURCE_LABEL = "PRESERVE_SOURCE_LABEL"


class StructureProjectionOperationV1(_StrictFrozenModel):
    operation: StructureProjectionOperationKind
    target_scope_id: str = Field(min_length=1, max_length=200)
    related_refs: tuple[str, ...] = Field(default=(), max_length=128)
    logical_table_scope_id: str | None = Field(default=None, max_length=200)
    canonical_column_count: StrictInt | None = Field(default=None, ge=1, le=64)

    @field_validator("related_refs")
    @classmethod
    def validate_related_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("projection operation refs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_operation(self) -> StructureProjectionOperationV1:
        if (
            self.operation
            is StructureProjectionOperationKind.COLLAPSE_NAVIGATION_CHILDREN
        ):
            if not self.related_refs or self.logical_table_scope_id is not None:
                raise ValueError("navigation collapse requires child refs only")
        elif (
            self.operation
            is StructureProjectionOperationKind.EXCLUDE_TABLE_OWNED_DIRECT_CONTENT
        ):
            if not self.related_refs:
                raise ValueError("table-owned exclusion requires table or child refs")
        elif self.operation in {
            StructureProjectionOperationKind.USE_VISUAL_TABLE_REGION,
            StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS,
            StructureProjectionOperationKind.PLACE_FLOATING_TABLE_IMAGE,
        }:
            if self.logical_table_scope_id is None:
                raise ValueError("table presentation operation requires a table scope")
        if (
            self.operation
            is StructureProjectionOperationKind.SET_CANONICAL_TABLE_COLUMNS
        ):
            if self.canonical_column_count is None:
                raise ValueError("canonical-column operation requires a column count")
        elif self.canonical_column_count is not None:
            raise ValueError("only canonical-column operation carries a column count")
        if (
            self.operation
            is StructureProjectionOperationKind.PLACE_FLOATING_TABLE_IMAGE
            and len(self.related_refs) != 1
        ):
            raise ValueError("floating-image operation requires one placement ref")
        return self


class StructureToolCallReceiptV1(_StrictFrozenModel):
    call_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    intent: StructureToolIntent
    reason_signals: tuple[str, ...] = Field(default=(), max_length=3)
    outcome: StructureToolOutcome
    result_role: StructureToolResultRole | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    failure_code: str = Field(
        default="",
        pattern=r"^(?:|[a-z][a-z0-9_]{0,119})$",
    )

    @field_validator("reason_signals")
    @classmethod
    def validate_reason_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item or not item.replace("_", "a").isalnum() for item in value
        ):
            raise ValueError("tool reason signals must be unique sorted machine codes")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> StructureToolCallReceiptV1:
        succeeded = self.outcome is StructureToolOutcome.SUCCEEDED
        if succeeded != bool(self.result_role and self.result_sha256):
            raise ValueError("successful tool call requires one typed result receipt")
        if succeeded and self.failure_code:
            raise ValueError("successful tool call cannot carry a failure")
        if not succeeded and not self.failure_code:
            raise ValueError("unsuccessful tool call requires a failure code")
        return self


class NativeStructureHostToolRequestV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-host-tool-request/v1"] = (
        STRUCTURE_HOST_TOOL_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    scope_id: str = Field(min_length=1, max_length=200)
    intent: StructureToolIntent
    reason_signals: tuple[str, ...] = Field(default=(), max_length=3)
    logical_table_scope_id: str | None = Field(default=None, max_length=200)

    @field_validator("reason_signals")
    @classmethod
    def validate_reason_signals(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item or not item.replace("_", "a").isalnum() for item in value
        ):
            raise ValueError("tool reason signals must be unique sorted machine codes")
        return value


class NativeStructureHostToolReceiptV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-host-tool-receipt/v1"] = (
        STRUCTURE_HOST_TOOL_RECEIPT_SCHEMA_VERSION
    )
    receipt_id: str = Field(
        pattern=r"^native-structure-host-tool-receipt-[0-9a-f]{32}$"
    )
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str
    handoff_id: str
    scope_id: str
    logical_table_scope_id: str | None = None
    tool_call: StructureToolCallReceiptV1
    created_at_utc: datetime


class StructureAgentCandidateRevisionV1(_StrictFrozenModel):
    revision_id: str = Field(pattern=r"^native-structure-agent-revision-[0-9a-f]{32}$")
    target_scope_id: str = Field(min_length=1, max_length=200)
    basis_role: StructureRevisionBasisRole
    basis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revised_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: tuple[StructureProjectionOperationV1, ...] = Field(
        min_length=1,
        max_length=32,
    )
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=4096)
    tool_call_ids: tuple[str, ...] = Field(default=(), max_length=32)

    @field_validator("evidence_refs", "tool_call_ids")
    @classmethod
    def validate_unique_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("revision values must be non-empty, unique, and sorted")
        return value

    @model_validator(mode="after")
    def validate_revision(self) -> StructureAgentCandidateRevisionV1:
        if self.basis_sha256 == self.revised_candidate_sha256:
            raise ValueError("Agent revision must change the candidate identity")
        expected_id = stable_structure_agent_revision_id(
            target_scope_id=self.target_scope_id,
            basis_role=self.basis_role,
            basis_sha256=self.basis_sha256,
            revised_candidate_sha256=self.revised_candidate_sha256,
            operations=self.operations,
            evidence_refs=self.evidence_refs,
            tool_call_ids=self.tool_call_ids,
        )
        if self.revision_id != expected_id:
            raise ValueError("Agent revision ID does not bind its replacement")
        return self


class StructureWorkItemDispositionV1(_StrictFrozenModel):
    input_kind: Literal["FEEDBACK", "PROPOSAL_DECISION"]
    input_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    outcome: Literal["RESOLVED", "KEPT_UNRESOLVED", "REJECTED"]
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,119}$")


class StructureConfirmedActionObservationV1(_StrictFrozenModel):
    action_id: str = Field(min_length=1, max_length=200)
    materialization_receipt_id: str = Field(default="", max_length=200)
    verification_receipt_id: str = Field(default="", max_length=200)
    rendered_output_reopened: bool = False


class StructureRenderedCellObservationV1(_StrictFrozenModel):
    cell_id: str = Field(min_length=1, max_length=200)
    bbox: tuple[float, float, float, float]
    text: str = Field(default="", max_length=20000)


class StructureContinuationSegmentObservationV1(_StrictFrozenModel):
    segment_id: str = Field(min_length=1, max_length=200)
    canonical_columns: tuple[str, ...]


class StructureImageDispositionObservationV1(_StrictFrozenModel):
    image_id: str = Field(min_length=1, max_length=200)
    disposition: Literal["PLACED_IN_CELL", "FLOATING_IN_TABLE", "UNRESOLVED"]


class StructureNavigationChildObservationV1(_StrictFrozenModel):
    child_id: str = Field(min_length=1, max_length=200)
    source_kind: str = Field(min_length=1, max_length=120)
    independently_navigable: bool


class StructureMaterializationObservationV1(_StrictFrozenModel):
    schema_version: Literal["structure-materialization-observation/v1"] = (
        "structure-materialization-observation/v1"
    )
    confirmed_actions: tuple[StructureConfirmedActionObservationV1, ...] = ()
    parent_direct_source_refs: tuple[str, ...] = ()
    child_owned_source_refs: tuple[str, ...] = ()
    node_title: str = Field(default="", max_length=20000)
    visible_title_lines: tuple[str, ...] = ()
    visual_scope_bbox: tuple[float, float, float, float] | None = None
    rendered_cells: tuple[StructureRenderedCellObservationV1, ...] = ()
    following_heading_texts: tuple[str, ...] = ()
    continuation_segments: tuple[StructureContinuationSegmentObservationV1, ...] = ()
    expected_image_ids: tuple[str, ...] = ()
    image_dispositions: tuple[StructureImageDispositionObservationV1, ...] = ()
    navigation_children: tuple[StructureNavigationChildObservationV1, ...] = ()


class NativeStructureHandoffAttemptRequestV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-handoff-attempt-request/v1"] = (
        STRUCTURE_HANDOFF_ATTEMPT_REQUEST_SCHEMA_VERSION
    )
    command_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    expected_source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_artifact_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    handoff_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
    attempt_ordinal: StrictInt = Field(ge=1, le=64)
    previous_attempt_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    affected_scope_ids: tuple[str, ...] = Field(min_length=1, max_length=64)
    tool_calls: tuple[StructureToolCallReceiptV1, ...] = Field(
        default=(), max_length=64
    )
    candidate_revisions: tuple[StructureAgentCandidateRevisionV1, ...] = Field(
        default=(),
        max_length=64,
    )
    materialized_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materialization_observation: StructureMaterializationObservationV1
    work_item_dispositions: tuple[StructureWorkItemDispositionV1, ...] = Field(
        default=(),
        max_length=128,
    )
    current_action: str = Field(min_length=1, max_length=500)
    public_summary: str = Field(min_length=1, max_length=1000)

    @field_validator("affected_scope_ids")
    @classmethod
    def validate_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("affected scopes must be non-empty, unique, and sorted")
        return value

    @field_validator("current_action", "public_summary")
    @classmethod
    def normalize_public_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or "\x00" in normalized:
            raise ValueError("public attempt text must be non-empty plain text")
        return normalized

    @model_validator(mode="after")
    def validate_attempt_input(self) -> NativeStructureHandoffAttemptRequestV1:
        if (self.attempt_ordinal == 1) != (
            self.previous_attempt_receipt_sha256 is None
        ):
            raise ValueError("only the first attempt omits previous receipt identity")
        tool_ids = tuple(item.call_id for item in self.tool_calls)
        if len(tool_ids) != len(set(tool_ids)):
            raise ValueError("tool call IDs must be unique")
        revision_ids = tuple(item.revision_id for item in self.candidate_revisions)
        if len(revision_ids) != len(set(revision_ids)):
            raise ValueError("candidate revision IDs must be unique")
        dispositions = tuple(
            (item.input_kind, item.input_id) for item in self.work_item_dispositions
        )
        if len(dispositions) != len(set(dispositions)):
            raise ValueError("work-item dispositions must be unique")
        successful_candidates = {
            str(item.result_sha256)
            for item in self.tool_calls
            if item.outcome is StructureToolOutcome.SUCCEEDED
            and item.result_role is StructureToolResultRole.ADVISORY_CANDIDATE
        }
        for revision in self.candidate_revisions:
            if revision.target_scope_id not in set(self.affected_scope_ids):
                raise ValueError("Agent revision targets an unaffected scope")
            if any(call_id not in set(tool_ids) for call_id in revision.tool_call_ids):
                raise ValueError("Agent revision cites an unknown tool call")
            if (
                revision.basis_role is StructureRevisionBasisRole.ADVISORY_CANDIDATE
                and revision.basis_sha256 not in successful_candidates
            ):
                raise ValueError(
                    "Agent can revise only a successful advisory tool candidate"
                )
        return self


class NativeStructureHandoffAttemptReceiptV1(_StrictFrozenModel):
    schema_version: Literal["native-structure-handoff-attempt-receipt/v1"] = (
        STRUCTURE_HANDOFF_ATTEMPT_RECEIPT_SCHEMA_VERSION
    )
    attempt_id: str = Field(pattern=r"^native-structure-handoff-attempt-[0-9a-f]{32}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    command_id: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str
    handoff_id: str
    kit_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_ordinal: StrictInt = Field(ge=1, le=64)
    previous_attempt_receipt_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    status: StructureHandoffAttemptStatus
    affected_scope_ids: tuple[str, ...]
    tool_calls: tuple[StructureToolCallReceiptV1, ...]
    candidate_revisions: tuple[StructureAgentCandidateRevisionV1, ...]
    materialized_candidate_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rendered_output_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validation_report: dict[str, Any]
    selected_failure_patterns: dict[str, Any]
    accepted_work_item_dispositions: tuple[StructureWorkItemDispositionV1, ...]
    current_action: str
    public_summary: str
    created_at_utc: datetime


def build_native_structure_handoff_attempt_receipt(
    request: NativeStructureHandoffAttemptRequestV1,
    *,
    validation_report: dict[str, Any],
    selected_failure_patterns: dict[str, Any],
    kit_version: str,
    kit_bundle_sha256: str,
    previous_attempt: NativeStructureHandoffAttemptReceiptV1 | None = None,
) -> NativeStructureHandoffAttemptReceiptV1:
    if request.attempt_ordinal == 1:
        if previous_attempt is not None:
            raise ValueError("first attempt cannot follow another attempt")
    elif (
        previous_attempt is None
        or request.attempt_ordinal != previous_attempt.attempt_ordinal + 1
        or request.previous_attempt_receipt_sha256 != previous_attempt.receipt_sha256
    ):
        raise ValueError("handoff attempt does not bind the previous receipt")
    if previous_attempt is not None and (
        previous_attempt.source_sha256 != request.expected_source_sha256
        or previous_attempt.artifact_id != request.expected_artifact_id
        or previous_attempt.handoff_id != request.handoff_id
    ):
        raise ValueError("handoff attempt lineage changed")
    for revision in request.candidate_revisions:
        if (
            revision.basis_role is StructureRevisionBasisRole.PREVIOUS_MATERIALIZATION
            and (
                previous_attempt is None
                or revision.basis_sha256
                != previous_attempt.materialized_candidate_sha256
            )
        ):
            raise ValueError("revision does not bind the previous materialization")
    if (
        previous_attempt is not None
        and previous_attempt.status is StructureHandoffAttemptStatus.REVISION_REQUIRED
    ):
        if (
            not request.candidate_revisions
            or request.materialized_candidate_sha256
            == previous_attempt.materialized_candidate_sha256
        ):
            raise ValueError(
                "failed materialization requires a changed replacement candidate"
            )
    validation_status = str(validation_report.get("status") or "")
    if validation_status not in {"PASS", "FAIL"}:
        raise ValueError("materialization validation report is not terminal")
    finding_ids = {
        str(item.get("pattern_id") or "")
        for item in validation_report.get("findings", [])
        if isinstance(item, dict)
    }
    materialized = bool(request.materialization_observation.confirmed_actions) and all(
        bool(item.materialization_receipt_id)
        for item in request.materialization_observation.confirmed_actions
    )
    status = (
        StructureHandoffAttemptStatus.VERIFIED
        if validation_status == "PASS"
        else StructureHandoffAttemptStatus.VERIFICATION_REQUIRED
        if finding_ids == {"confirmed_proposal_not_verified"} and materialized
        else StructureHandoffAttemptStatus.REVISION_REQUIRED
    )
    accepted_dispositions = (
        request.work_item_dispositions
        if status is StructureHandoffAttemptStatus.VERIFIED
        else ()
    )
    payload = {
        "schema_version": STRUCTURE_HANDOFF_ATTEMPT_RECEIPT_SCHEMA_VERSION,
        "command_id": request.command_id,
        "source_sha256": request.expected_source_sha256,
        "artifact_id": request.expected_artifact_id,
        "handoff_id": request.handoff_id,
        "kit_version": kit_version,
        "kit_bundle_sha256": kit_bundle_sha256,
        "attempt_ordinal": request.attempt_ordinal,
        "previous_attempt_receipt_sha256": (request.previous_attempt_receipt_sha256),
        "status": status.value,
        "affected_scope_ids": list(request.affected_scope_ids),
        "tool_calls": [item.model_dump(mode="json") for item in request.tool_calls],
        "candidate_revisions": [
            item.model_dump(mode="json") for item in request.candidate_revisions
        ],
        "materialized_candidate_sha256": request.materialized_candidate_sha256,
        "rendered_output_sha256": request.rendered_output_sha256,
        "validation_report": validation_report,
        "selected_failure_patterns": selected_failure_patterns,
        "accepted_work_item_dispositions": [
            item.model_dump(mode="json") for item in accepted_dispositions
        ],
        "current_action": request.current_action,
        "public_summary": request.public_summary,
    }
    request_sha256 = _canonical_sha256(request.model_dump(mode="json"))
    attempt_digest = _canonical_sha256(payload)
    attempt_id = f"native-structure-handoff-attempt-{attempt_digest[:32]}"
    receipt_payload = {
        "attempt_id": attempt_id,
        "request_sha256": request_sha256,
        **payload,
    }
    return NativeStructureHandoffAttemptReceiptV1.model_validate(
        {
            **receipt_payload,
            "receipt_sha256": _canonical_sha256(receipt_payload),
            "created_at_utc": datetime.now(UTC),
        }
    )


def build_native_structure_host_tool_receipt(
    request: NativeStructureHostToolRequestV1,
    *,
    outcome: StructureToolOutcome,
    result_role: StructureToolResultRole | None = None,
    result_sha256: str | None = None,
    failure_code: str = "",
) -> NativeStructureHostToolReceiptV1:
    request_payload = request.model_dump(mode="json")
    request_sha256 = _canonical_sha256(request_payload)
    call_identity = {
        "request_sha256": request_sha256,
        "outcome": outcome.value,
        "result_role": result_role.value if result_role is not None else None,
        "result_sha256": result_sha256,
        "failure_code": failure_code,
    }
    call_id = f"native-structure-host-tool-call-{_canonical_sha256(call_identity)[:32]}"
    tool_call = StructureToolCallReceiptV1(
        call_id=call_id,
        intent=request.intent,
        reason_signals=request.reason_signals,
        outcome=outcome,
        result_role=result_role,
        result_sha256=result_sha256,
        failure_code=failure_code,
    )
    receipt_payload = {
        "schema_version": STRUCTURE_HOST_TOOL_RECEIPT_SCHEMA_VERSION,
        "request_sha256": request_sha256,
        "command_id": request.command_id,
        "source_sha256": request.expected_source_sha256,
        "artifact_id": request.expected_artifact_id,
        "handoff_id": request.handoff_id,
        "scope_id": request.scope_id,
        "logical_table_scope_id": request.logical_table_scope_id,
        "tool_call": tool_call.model_dump(mode="json"),
    }
    receipt_id = (
        f"native-structure-host-tool-receipt-{_canonical_sha256(receipt_payload)[:32]}"
    )
    bound_payload = {"receipt_id": receipt_id, **receipt_payload}
    return NativeStructureHostToolReceiptV1.model_validate(
        {
            **bound_payload,
            "receipt_sha256": _canonical_sha256(bound_payload),
            "created_at_utc": datetime.now(UTC),
        }
    )


def stable_structure_agent_revision_id(
    *,
    target_scope_id: str,
    basis_role: StructureRevisionBasisRole,
    basis_sha256: str,
    revised_candidate_sha256: str,
    operations: tuple[StructureProjectionOperationV1, ...],
    evidence_refs: tuple[str, ...],
    tool_call_ids: tuple[str, ...],
) -> str:
    digest = _canonical_sha256(
        {
            "target_scope_id": target_scope_id,
            "basis_role": basis_role.value,
            "basis_sha256": basis_sha256,
            "revised_candidate_sha256": revised_candidate_sha256,
            "operations": [item.model_dump(mode="json") for item in operations],
            "evidence_refs": list(evidence_refs),
            "tool_call_ids": list(tool_call_ids),
        }
    )
    return f"native-structure-agent-revision-{digest[:32]}"


def active_structure_projection_attempt(
    attempts: Iterable[NativeStructureHandoffAttemptReceiptV1],
) -> NativeStructureHandoffAttemptReceiptV1 | None:
    adopted = _ordered_adopted_structure_projection_attempts(attempts)
    latest_by_handoff: dict[
        str, tuple[int, NativeStructureHandoffAttemptReceiptV1]
    ] = {}
    for index, attempt in enumerate(adopted):
        latest_by_handoff[attempt.handoff_id] = (index, attempt)
    unresolved = tuple(
        indexed_attempt[1]
        for indexed_attempt in sorted(
            latest_by_handoff.values(), key=lambda item: item[0]
        )
        if (
            indexed_attempt[1].status
            is StructureHandoffAttemptStatus.VERIFICATION_REQUIRED
        )
    )
    if unresolved:
        return unresolved[-1]
    return adopted[-1] if adopted else None


def canonical_structure_projection_attempts_sha256(
    attempts: Iterable[NativeStructureHandoffAttemptReceiptV1],
) -> str:
    adopted = _ordered_adopted_structure_projection_attempts(attempts)
    return _canonical_sha256(
        {
            "schema_version": "native-structure-projection-attempt-set/v1",
            "attempts": [
                {
                    "attempt_id": attempt.attempt_id,
                    "receipt_sha256": attempt.receipt_sha256,
                    "status": attempt.status.value,
                }
                for attempt in adopted
            ],
        }
    )


def _ordered_adopted_structure_projection_attempts(
    attempts: Iterable[NativeStructureHandoffAttemptReceiptV1],
) -> tuple[NativeStructureHandoffAttemptReceiptV1, ...]:
    def order_key(
        attempt: NativeStructureHandoffAttemptReceiptV1,
    ) -> tuple[datetime, str, int, str]:
        created_at = attempt.created_at_utc
        if created_at.tzinfo is None:
            raise ValueError("structure attempt timestamp must be timezone-bound")
        return (
            created_at.astimezone(UTC),
            attempt.handoff_id,
            attempt.attempt_ordinal,
            attempt.attempt_id,
        )

    return tuple(
        attempt
        for attempt in sorted(tuple(attempts), key=order_key)
        if attempt.status
        in {
            StructureHandoffAttemptStatus.VERIFIED,
            StructureHandoffAttemptStatus.VERIFICATION_REQUIRED,
        }
    )


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "NativeStructureHostToolReceiptV1",
    "NativeStructureHostToolRequestV1",
    "NativeStructureHandoffAttemptReceiptV1",
    "NativeStructureHandoffAttemptRequestV1",
    "StructureAgentCandidateRevisionV1",
    "StructureHandoffAttemptStatus",
    "StructureMaterializationObservationV1",
    "StructureProjectionOperationKind",
    "StructureProjectionOperationV1",
    "StructureRevisionBasisRole",
    "StructureToolCallReceiptV1",
    "StructureToolIntent",
    "StructureToolOutcome",
    "StructureToolResultRole",
    "StructureWorkItemDispositionV1",
    "build_native_structure_handoff_attempt_receipt",
    "build_native_structure_host_tool_receipt",
    "active_structure_projection_attempt",
    "canonical_structure_projection_attempts_sha256",
    "stable_structure_agent_revision_id",
]
