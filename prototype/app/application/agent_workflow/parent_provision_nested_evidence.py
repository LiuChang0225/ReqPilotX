"""Content-free closure for nested evidence assigned to final parent provisions.

The region-aware stage input keeps nested tables and figures outside the exact
text-cover segment set.  They still need one final parent owner before a parent
baseline can be published.  This module only accepts a Host-provable assignment:
all non-whitespace content of every declared owner segment must be carried by one
and only one final parent entry.  Ambiguous or missing ownership stays explicit
and cannot be upgraded to a successful professional finalization.
"""

from __future__ import annotations

from enum import StrEnum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.agent_workflow.d3_parent_provision_compiler import (
    D3HostCompiledParentProvisionDecisionV1,
)
from app.application.agent_workflow.d3_parent_provisions import (
    D3ExactSourceSegmentV1,
    ParentProvisionEntryV3,
    canonical_json_sha256,
)
from app.application.agent_workflow.d3_region_stage_input import (
    D3EvidenceLedgerEntryV1,
    D3EvidenceLedgerKindV1,
)
from app.application.agent_workflow.parent_provision_object_batches import (
    PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN,
)


PARENT_PROVISION_NESTED_EVIDENCE_ELIGIBILITY_SCHEMA_VERSION = (
    "parent-provision-nested-evidence-eligibility/v1"
)
PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_SCHEMA_VERSION = (
    "parent-provision-nested-evidence-assignment/v1"
)
PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_CLOSURE_SCHEMA_VERSION = (
    "parent-provision-nested-evidence-assignment-closure/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ParentProvisionNestedEvidenceError(ValueError):
    """Fail-closed nested-evidence assignment boundary."""


class ParentProvisionNestedEvidenceAssignmentStatus(StrEnum):
    COMPLETE = "COMPLETE"
    INCOMPLETE = "INCOMPLETE"


class ParentProvisionNestedEvidenceAssignmentMethod(StrEnum):
    HOST_UNIQUE_OWNER_SEGMENT_EXACT_COVER = "HOST_UNIQUE_OWNER_SEGMENT_EXACT_COVER"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionNestedEvidenceEligibilityV1(_StrictFrozenModel):
    """Content-free Host fact that one nested evidence item needs a parent."""

    schema_version: Literal["parent-provision-nested-evidence-eligibility/v1"] = (
        PARENT_PROVISION_NESTED_EVIDENCE_ELIGIBILITY_SCHEMA_VERSION
    )
    evidence_id: str = Field(
        pattern=r"^d2-table-region-excluded-evidence-[0-9a-f]{32}$"
    )
    evidence_kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    evidence_locator_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    ultimate_owner_logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    owner_segment_ids: tuple[str, ...] = Field(min_length=1, max_length=4096)
    source_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ledger_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    eligibility_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_eligibility(self) -> ParentProvisionNestedEvidenceEligibilityV1:
        if len(self.owner_segment_ids) != len(set(self.owner_segment_ids)) or any(
            not item.strip() for item in self.owner_segment_ids
        ):
            raise ValueError("nested evidence owner segments must be unique")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"eligibility_sha256"})
        )
        if self.eligibility_sha256 != expected:
            raise ValueError("nested evidence eligibility SHA drifted")
        return self


class ParentProvisionNestedEvidenceAssignmentV1(_StrictFrozenModel):
    """One replayable, content-free evidence-to-parent assignment."""

    schema_version: Literal["parent-provision-nested-evidence-assignment/v1"] = (
        PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_SCHEMA_VERSION
    )
    evidence_id: str = Field(
        pattern=r"^d2-table-region-excluded-evidence-[0-9a-f]{32}$"
    )
    eligibility_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ultimate_owner_logical_object_id: str = Field(
        pattern=PARENT_PROVISION_LOGICAL_OBJECT_ID_PATTERN
    )
    selected_parent_provision_id: str = Field(
        pattern=r"^parent-provision-[0-9a-f]{32}$"
    )
    selected_parent_entry_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    assignment_method: Literal[
        ParentProvisionNestedEvidenceAssignmentMethod.HOST_UNIQUE_OWNER_SEGMENT_EXACT_COVER
    ] = ParentProvisionNestedEvidenceAssignmentMethod.HOST_UNIQUE_OWNER_SEGMENT_EXACT_COVER
    assignment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_assignment(self) -> ParentProvisionNestedEvidenceAssignmentV1:
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"assignment_sha256"})
        )
        if self.assignment_sha256 != expected:
            raise ValueError("nested evidence assignment SHA drifted")
        return self


class ParentProvisionNestedEvidenceAssignmentClosureV1(_StrictFrozenModel):
    """Final decision-bound assignment closure; contains no customer text."""

    schema_version: Literal[
        "parent-provision-nested-evidence-assignment-closure/v1"
    ] = PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_CLOSURE_SCHEMA_VERSION
    closure_id: str = Field(
        pattern=r"^parent-provision-nested-evidence-closure-[0-9a-f]{32}$"
    )
    closure_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    content_free: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    predecessor_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    stage_input_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    stage_input_manifest_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    parent_provision_decision_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    eligibilities: tuple[ParentProvisionNestedEvidenceEligibilityV1, ...] = ()
    eligibility_ledger_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    assignments: tuple[ParentProvisionNestedEvidenceAssignmentV1, ...] = ()
    assignment_ledger_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    unassigned_evidence_ids: tuple[str, ...] = ()
    status: ParentProvisionNestedEvidenceAssignmentStatus

    @model_validator(mode="after")
    def validate_closure(self) -> ParentProvisionNestedEvidenceAssignmentClosureV1:
        eligibility_ids = tuple(item.evidence_id for item in self.eligibilities)
        assignment_ids = tuple(item.evidence_id for item in self.assignments)
        if len(eligibility_ids) != len(set(eligibility_ids)):
            raise ValueError("nested evidence eligibility IDs must be unique")
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("nested evidence assignment IDs must be unique")
        expected_assigned_ids = tuple(
            item
            for item in eligibility_ids
            if item not in set(self.unassigned_evidence_ids)
        )
        if (
            self.unassigned_evidence_ids
            != tuple(
                item for item in eligibility_ids if item not in set(assignment_ids)
            )
            or assignment_ids != expected_assigned_ids
            or any(
                assignment.eligibility_sha256 != eligibility.eligibility_sha256
                or assignment.ultimate_owner_logical_object_id
                != eligibility.ultimate_owner_logical_object_id
                for assignment, eligibility in zip(
                    self.assignments,
                    (
                        item
                        for item in self.eligibilities
                        if item.evidence_id in set(assignment_ids)
                    ),
                    strict=True,
                )
            )
        ):
            raise ValueError("nested evidence assignment coverage drifted")
        if self.eligibility_ledger_sha256 != canonical_json_sha256(
            tuple(item.eligibility_sha256 for item in self.eligibilities)
        ):
            raise ValueError("nested evidence eligibility ledger SHA drifted")
        if self.assignment_ledger_sha256 != canonical_json_sha256(
            tuple(item.assignment_sha256 for item in self.assignments)
        ):
            raise ValueError("nested evidence assignment ledger SHA drifted")
        expected_status = (
            ParentProvisionNestedEvidenceAssignmentStatus.INCOMPLETE
            if self.unassigned_evidence_ids
            else ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE
        )
        if self.status is not expected_status:
            raise ValueError(
                "nested evidence assignment status hides an incomplete item"
            )
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"closure_id", "closure_sha256"})
        )
        if self.closure_sha256 != expected:
            raise ValueError("nested evidence assignment closure SHA drifted")
        if self.closure_id != (
            f"parent-provision-nested-evidence-closure-{expected[:32]}"
        ):
            raise ValueError("nested evidence assignment closure identity drifted")
        return self


def build_nested_evidence_eligibilities(
    evidence_ledger: tuple[D3EvidenceLedgerEntryV1, ...],
) -> tuple[ParentProvisionNestedEvidenceEligibilityV1, ...]:
    """Project every eligible region-ledger item into a content-free fact."""

    try:
        ledger = tuple(
            D3EvidenceLedgerEntryV1.model_validate(item.model_dump(mode="json"))
            for item in evidence_ledger
        )
    except (TypeError, ValueError) as exc:
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_LEDGER_INVALID"
        ) from exc
    eligible = tuple(
        item for item in ledger if item.selected_parent_assignment_eligible
    )
    result: list[ParentProvisionNestedEvidenceEligibilityV1] = []
    for item in eligible:
        if (
            item.evidence_kind is not D3EvidenceLedgerKindV1.NESTED_REGION
            or item.region_id is None
            or item.ultimate_owner_logical_object_id is None
            or not item.owner_segment_ids
        ):
            raise ParentProvisionNestedEvidenceError(
                "PARENT_PROVISION_NESTED_EVIDENCE_ELIGIBILITY_INVALID"
            )
        values = {
            "evidence_id": item.evidence_id,
            "evidence_kind": item.evidence_kind.value,
            "evidence_locator_id": item.region_id,
            "ultimate_owner_logical_object_id": (item.ultimate_owner_logical_object_id),
            "owner_segment_ids": item.owner_segment_ids,
            "source_evidence_sha256": item.source_evidence_sha256,
            "source_ledger_sha256": item.ledger_sha256,
        }
        draft = ParentProvisionNestedEvidenceEligibilityV1.model_construct(
            **values,
            eligibility_sha256="0" * 64,
        )
        result.append(
            ParentProvisionNestedEvidenceEligibilityV1(
                **values,
                eligibility_sha256=canonical_json_sha256(
                    draft.model_dump(mode="json", exclude={"eligibility_sha256"})
                ),
            )
        )
    return tuple(result)


def build_parent_provision_nested_evidence_assignment_closure(
    *,
    stage_input_bundle_sha256: str,
    stage_input_manifest_sha256: str,
    decision: D3HostCompiledParentProvisionDecisionV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    eligibilities: tuple[ParentProvisionNestedEvidenceEligibilityV1, ...],
) -> ParentProvisionNestedEvidenceAssignmentClosureV1:
    """Assign only evidence with exactly one full owner-segment parent carrier."""

    try:
        compiled = D3HostCompiledParentProvisionDecisionV1.model_validate(
            decision.model_dump(mode="json")
        )
        segments = tuple(
            D3ExactSourceSegmentV1.model_validate(item.model_dump(mode="json"))
            for item in source_segments
        )
        eligible = tuple(
            ParentProvisionNestedEvidenceEligibilityV1.model_validate(
                item.model_dump(mode="json")
            )
            for item in eligibilities
        )
    except (TypeError, ValueError) as exc:
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_CONTRACT_INVALID"
        ) from exc
    for value, subject in (
        (stage_input_bundle_sha256, "STAGE_INPUT_BUNDLE"),
        (stage_input_manifest_sha256, "STAGE_INPUT_MANIFEST"),
    ):
        if _SHA256_PATTERN.fullmatch(value) is None:
            raise ParentProvisionNestedEvidenceError(
                f"PARENT_PROVISION_NESTED_EVIDENCE_{subject}_SHA_INVALID"
            )
    if len({item.evidence_id for item in eligible}) != len(eligible):
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_ELIGIBILITY_DUPLICATED"
        )
    segment_by_id = {item.segment_id: item for item in segments}
    if len(segment_by_id) != len(segments):
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_SEGMENT_DUPLICATED"
        )

    assignments: list[ParentProvisionNestedEvidenceAssignmentV1] = []
    unassigned: list[str] = []
    for item in eligible:
        owner_segments = tuple(
            segment_by_id.get(value) for value in item.owner_segment_ids
        )
        if any(value is None for value in owner_segments) or any(
            value.logical_object_id != item.ultimate_owner_logical_object_id
            for value in owner_segments
            if value is not None
        ):
            raise ParentProvisionNestedEvidenceError(
                "PARENT_PROVISION_NESTED_EVIDENCE_OWNER_SEGMENT_DRIFT"
            )
        concrete_segments = tuple(
            value for value in owner_segments if value is not None
        )
        carriers = tuple(
            entry
            for entry in compiled.entries
            if item.ultimate_owner_logical_object_id in entry.logical_object_ids
            and _fully_carries_owner_segments(entry, concrete_segments)
        )
        if len(carriers) != 1:
            unassigned.append(item.evidence_id)
            continue
        carrier = carriers[0]
        values = {
            "evidence_id": item.evidence_id,
            "eligibility_sha256": item.eligibility_sha256,
            "ultimate_owner_logical_object_id": (item.ultimate_owner_logical_object_id),
            "selected_parent_provision_id": carrier.parent_provision_id,
            "selected_parent_entry_sha256": canonical_json_sha256(
                carrier.model_dump(mode="json")
            ),
            "assignment_method": (
                ParentProvisionNestedEvidenceAssignmentMethod.HOST_UNIQUE_OWNER_SEGMENT_EXACT_COVER
            ),
        }
        draft = ParentProvisionNestedEvidenceAssignmentV1.model_construct(
            **values,
            assignment_sha256="0" * 64,
        )
        assignments.append(
            ParentProvisionNestedEvidenceAssignmentV1(
                **values,
                assignment_sha256=canonical_json_sha256(
                    draft.model_dump(mode="json", exclude={"assignment_sha256"})
                ),
            )
        )

    closure_values = {
        "content_free": True,
        "run_id": compiled.run_id,
        "source_sha256": compiled.source_sha256,
        "predecessor_artifact_id": compiled.predecessor_d2_artifact_id,
        "predecessor_artifact_sha256": compiled.predecessor_d2_artifact_sha256,
        "stage_input_bundle_sha256": stage_input_bundle_sha256,
        "stage_input_manifest_sha256": stage_input_manifest_sha256,
        "parent_provision_decision_sha256": compiled.decision_sha256,
        "eligibilities": eligible,
        "eligibility_ledger_sha256": canonical_json_sha256(
            tuple(item.eligibility_sha256 for item in eligible)
        ),
        "assignments": tuple(assignments),
        "assignment_ledger_sha256": canonical_json_sha256(
            tuple(item.assignment_sha256 for item in assignments)
        ),
        "unassigned_evidence_ids": tuple(unassigned),
        "status": (
            ParentProvisionNestedEvidenceAssignmentStatus.INCOMPLETE
            if unassigned
            else ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE
        ),
    }
    draft = ParentProvisionNestedEvidenceAssignmentClosureV1.model_construct(
        closure_id="parent-provision-nested-evidence-closure-" + "0" * 32,
        closure_sha256="0" * 64,
        **closure_values,
    )
    digest = canonical_json_sha256(
        draft.model_dump(mode="json", exclude={"closure_id", "closure_sha256"})
    )
    return ParentProvisionNestedEvidenceAssignmentClosureV1(
        closure_id=f"parent-provision-nested-evidence-closure-{digest[:32]}",
        closure_sha256=digest,
        **closure_values,
    )


def validate_parent_provision_nested_evidence_assignment_closure(
    *,
    expected_stage_input_bundle_sha256: str,
    expected_stage_input_manifest_sha256: str,
    decision: D3HostCompiledParentProvisionDecisionV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    expected_eligibilities: tuple[ParentProvisionNestedEvidenceEligibilityV1, ...],
    closure: ParentProvisionNestedEvidenceAssignmentClosureV1,
) -> ParentProvisionNestedEvidenceAssignmentClosureV1:
    """Replay the Host-only assignment and require an exact complete closure."""

    try:
        observed = ParentProvisionNestedEvidenceAssignmentClosureV1.model_validate(
            closure.model_dump(mode="json")
        )
    except (TypeError, ValueError) as exc:
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_CLOSURE_INVALID"
        ) from exc
    replayed = build_parent_provision_nested_evidence_assignment_closure(
        stage_input_bundle_sha256=expected_stage_input_bundle_sha256,
        stage_input_manifest_sha256=expected_stage_input_manifest_sha256,
        decision=decision,
        source_segments=source_segments,
        eligibilities=expected_eligibilities,
    )
    if observed != replayed:
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_REPLAY_DRIFT"
        )
    if observed.status is not ParentProvisionNestedEvidenceAssignmentStatus.COMPLETE:
        raise ParentProvisionNestedEvidenceError(
            "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_INCOMPLETE"
        )
    return observed


def _fully_carries_owner_segments(
    entry: ParentProvisionEntryV3,
    owner_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> bool:
    ranges_by_segment: dict[str, list[tuple[int, int]]] = {}
    for source_range in entry.source_ranges:
        ranges_by_segment.setdefault(source_range.segment_id, []).append(
            (source_range.start_offset, source_range.end_offset)
        )
    has_non_whitespace_obligation = False
    for segment in owner_segments:
        ranges = ranges_by_segment.get(segment.segment_id, ())
        for offset, character in enumerate(segment.source_text):
            if character.isspace():
                continue
            has_non_whitespace_obligation = True
            if sum(start <= offset < end for start, end in ranges) != 1:
                return False
    return has_non_whitespace_obligation


__all__ = [
    "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_CLOSURE_SCHEMA_VERSION",
    "PARENT_PROVISION_NESTED_EVIDENCE_ASSIGNMENT_SCHEMA_VERSION",
    "PARENT_PROVISION_NESTED_EVIDENCE_ELIGIBILITY_SCHEMA_VERSION",
    "ParentProvisionNestedEvidenceAssignmentClosureV1",
    "ParentProvisionNestedEvidenceAssignmentMethod",
    "ParentProvisionNestedEvidenceAssignmentStatus",
    "ParentProvisionNestedEvidenceAssignmentV1",
    "ParentProvisionNestedEvidenceEligibilityV1",
    "ParentProvisionNestedEvidenceError",
    "build_nested_evidence_eligibilities",
    "build_parent_provision_nested_evidence_assignment_closure",
    "validate_parent_provision_nested_evidence_assignment_closure",
]
