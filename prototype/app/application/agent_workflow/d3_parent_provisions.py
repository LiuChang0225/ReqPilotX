from __future__ import annotations

import hashlib
import json
import math
import re
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureProposalV1,
    D2LogicalStructureValidationReceiptV1,
)
from app.application.agent_workflow.d3_parent_provision_attestation import (
    D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION,
    D3ParentProvisionExecutionReceiptV1,
    D3ParentProvisionPolicyAttestationV1,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1


PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION = "parent-provision-proposal/v3"
PARENT_PROVISION_ENTRY_EVIDENCE_REF_LIMIT = 256
PARENT_PROVISION_VALIDATION_RECEIPT_SCHEMA_VERSION = (
    "parent-provision-validation-receipt/v3"
)
D3_VALIDATED_D2_IDENTITY_SCHEMA_VERSION = "d3-validated-d2-identity/v1"
D3_SOURCE_SEGMENT_SCHEMA_VERSION = "d3-exact-source-segment/v1"
D3_POLICY_ATTESTATION_IDENTITY_SCHEMA_VERSION = (
    "d3-professional-policy-attestation-identity/v1"
)
PARENT_PROVISION_VALIDATOR_ID = "reqpilotx-parent-provision-validator"
PARENT_PROVISION_VALIDATOR_VERSION = "1.0.0"


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ParentProvisionStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class Requirementness(StrEnum):
    REQUIREMENT = "REQUIREMENT"
    NON_REQUIREMENT = "NON_REQUIREMENT"
    UNRESOLVED = "UNRESOLVED"


class RequirementKind(StrEnum):
    FUNCTIONAL = "FUNCTIONAL"
    QUALITY = "QUALITY"
    CONSTRAINT = "CONSTRAINT"
    OTHER_REQUIREMENT = "OTHER_REQUIREMENT"
    UNRESOLVED = "UNRESOLVED"


class RequirementTechnicality(StrEnum):
    TECHNICAL = "TECHNICAL"
    NON_TECHNICAL = "NON_TECHNICAL"
    MIXED = "MIXED"
    UNRESOLVED = "UNRESOLVED"


class ProvisionContentRole(StrEnum):
    PROVISION = "PROVISION"
    HEADING = "HEADING"
    METADATA = "METADATA"
    CONTEXT = "CONTEXT"
    RATIONALE = "RATIONALE"
    ASSUMPTION = "ASSUMPTION"
    EXAMPLE = "EXAMPLE"
    DEFINITION = "DEFINITION"
    REFERENCE = "REFERENCE"
    NOTE = "NOTE"
    TABLE_RECORD = "TABLE_RECORD"
    OTHER = "OTHER"


class D3ValidatedD2ArtifactIdentityV1(_StrictFrozenModel):
    """Host-built narrow view of one already validated D2 artifact.

    This is deliberately not another D2 proposal.  The D2 validator/adapter must
    create this view from its immutable proposal and validation receipt; D3 is
    not allowed to reinterpret D2 ownership.
    """

    schema_version: Literal["d3-validated-d2-identity/v1"] = (
        D3_VALIDATED_D2_IDENTITY_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=200)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    status: ParentProvisionStatus
    logical_object_ids: tuple[str, ...] = Field(min_length=1, max_length=8192)
    source_segment_ids: tuple[str, ...] = Field(min_length=1, max_length=65536)
    source_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("logical_object_ids", "source_segment_ids")
    @classmethod
    def validate_unique_ordered_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("validated D2 IDs must be unique and non-empty")
        return value


class D3ExactSourceSegmentV1(_StrictFrozenModel):
    """Exact Host-resolved text for one D2-owned source segment/handle."""

    schema_version: Literal["d3-exact-source-segment/v1"] = (
        D3_SOURCE_SEGMENT_SCHEMA_VERSION
    )
    segment_id: str = Field(min_length=1, max_length=200)
    logical_object_id: str = Field(min_length=1, max_length=200)
    source_handle: str = Field(min_length=1, max_length=240)
    handle_kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    owner_source_node_id: str = Field(min_length=1, max_length=200)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1)
    handle_start_offset: StrictInt | None = Field(default=None, ge=0)
    handle_end_offset: StrictInt | None = Field(default=None, ge=0)
    source_text: str = Field(max_length=1_000_000)
    source_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_segment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("source-segment evidence refs must be unique and sorted")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("source-segment pages must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_exact_text(self) -> D3ExactSourceSegmentV1:
        if hashlib.sha256(self.source_text.encode("utf-8")).hexdigest() != (
            self.source_text_sha256
        ):
            raise ValueError("D3 source segment text hash mismatch")
        if (self.handle_start_offset is None) != (self.handle_end_offset is None):
            raise ValueError("source-handle offsets must be supplied as one pair")
        if self.handle_start_offset is not None:
            assert self.handle_end_offset is not None
            if self.handle_end_offset <= self.handle_start_offset:
                raise ValueError("source-handle range must be non-empty")
            if self.handle_end_offset - self.handle_start_offset != len(
                self.source_text
            ):
                raise ValueError("source text length must match its D2 handle range")
        return self


class ParentProvisionSourceRangeV3(_StrictFrozenModel):
    segment_id: str = Field(min_length=1, max_length=200)
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(gt=0)
    verbatim_text: str = Field(min_length=1, max_length=1_000_000)
    verbatim_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_local_range(self) -> ParentProvisionSourceRangeV3:
        if self.end_offset <= self.start_offset:
            raise ValueError("parent source range must be non-empty")
        if self.end_offset - self.start_offset != len(self.verbatim_text):
            raise ValueError("parent source range length must match verbatim text")
        if hashlib.sha256(self.verbatim_text.encode("utf-8")).hexdigest() != (
            self.verbatim_text_sha256
        ):
            raise ValueError("parent source range verbatim hash mismatch")
        return self


class ParentProvisionEntryV3(_StrictFrozenModel):
    parent_provision_id: str = Field(pattern=r"^parent-provision-[0-9a-f]{32}$")
    source_order: StrictInt = Field(ge=0)
    logical_object_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    explicit_label: str | None = Field(default=None, max_length=160)
    ancestor_labels: tuple[str, ...] = Field(default=(), max_length=64)
    source_ranges: tuple[ParentProvisionSourceRangeV3, ...] = Field(
        min_length=1,
        max_length=1024,
    )
    requirementness: Requirementness
    requirement_kind: RequirementKind | None = None
    technicality: RequirementTechnicality | None = None
    content_role: ProvisionContentRole
    confidence: StrictFloat = Field(ge=0.0, le=1.0)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    evidence_refs: tuple[str, ...] = Field(
        min_length=1,
        max_length=PARENT_PROVISION_ENTRY_EVIDENCE_REF_LIMIT,
    )

    @field_validator("logical_object_ids", "ancestor_labels")
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("parent lineage values must be unique and non-empty")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", item) is None for item in value
        ):
            raise ValueError("parent reasons must be sorted bounded machine codes")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_entry_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("parent evidence refs must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_classification_axes(self) -> ParentProvisionEntryV3:
        if not math.isfinite(self.confidence):
            raise ValueError("parent confidence must be finite")
        if (
            self.requirementness is Requirementness.REQUIREMENT
            and self.requirement_kind is None
        ):
            raise ValueError("requirements must declare a requirement kind")
        if (
            self.requirementness is Requirementness.NON_REQUIREMENT
            and self.requirement_kind is not None
        ):
            raise ValueError("non-requirements cannot claim a requirement kind")
        if (
            self.requirementness is Requirementness.UNRESOLVED
            and self.requirement_kind not in {None, RequirementKind.UNRESOLVED}
        ):
            raise ValueError("unresolved requirementness cannot claim a resolved kind")
        if (
            self.requirementness is not Requirementness.REQUIREMENT
            and self.technicality not in {None, RequirementTechnicality.UNRESOLVED}
        ):
            raise ValueError(
                "non-requirement or unresolved entries cannot claim resolved "
                "technicality"
            )
        return self


class D3ProfessionalPolicyAttestationIdentityV1(_StrictFrozenModel):
    """Content-free Host-pinned proof of the professional D3 execution context."""

    schema_version: Literal["d3-professional-policy-attestation-identity/v1"] = (
        D3_POLICY_ATTESTATION_IDENTITY_SCHEMA_VERSION
    )
    attestation_id: str = Field(pattern=r"^d3-policy-attestation-[0-9a-f]{32}$")
    attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_attestation_id: str = Field(
        pattern=r"^d3-policy-attestation-[0-9a-f]{32}$"
    )
    parent_provision_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parent_provision_policy_attested: Literal[True] = True
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_id: Literal["reqpilotx-requirement-baseline-curator"] = (
        "reqpilotx-requirement-baseline-curator"
    )
    policy_closure_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    attested_decision_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_identity_hash(self) -> D3ProfessionalPolicyAttestationIdentityV1:
        expected_sha256 = canonical_json_sha256(
            self.model_dump(
                mode="json",
                exclude={"attestation_id", "attestation_sha256"},
            )
        )
        if self.attestation_sha256 != expected_sha256:
            raise ValueError("D3 professional policy identity SHA is stale")
        if self.attestation_id != f"d3-policy-attestation-{expected_sha256[:32]}":
            raise ValueError("D3 professional policy identity ID is stale")
        return self


class ParentProvisionProposalV3(_StrictFrozenModel):
    """Source-faithful D3 baseline; it intentionally contains no atomization."""

    schema_version: Literal["parent-provision-proposal/v3"] = (
        PARENT_PROVISION_PROPOSAL_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    predecessor_d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_attestation: D3ProfessionalPolicyAttestationIdentityV1
    status: ParentProvisionStatus
    declared_logical_object_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    unresolved_logical_object_ids: tuple[str, ...] = Field(
        default=(),
        max_length=8192,
    )
    entries: tuple[ParentProvisionEntryV3, ...] = Field(
        default=(),
        max_length=65536,
    )
    diagnostic_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator(
        "declared_logical_object_ids",
        "unresolved_logical_object_ids",
    )
    @classmethod
    def validate_scope_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("D3 logical-object scopes must be unique and non-empty")
        return value

    @field_validator("diagnostic_codes")
    @classmethod
    def validate_diagnostics(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None for item in value
        ):
            raise ValueError("D3 diagnostics must be sorted bounded machine codes")
        return value

    @model_validator(mode="after")
    def validate_local_proposal(self) -> ParentProvisionProposalV3:
        _validate_parent_decision_shape(
            status=self.status,
            declared_logical_object_ids=self.declared_logical_object_ids,
            unresolved_logical_object_ids=self.unresolved_logical_object_ids,
            entries=self.entries,
            diagnostic_codes=self.diagnostic_codes,
        )
        decision_sha256 = parent_provision_decision_sha256(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            kit_identity=self.kit_identity,
            predecessor_d2_artifact_id=self.predecessor_d2_artifact_id,
            predecessor_d2_artifact_sha256=(self.predecessor_d2_artifact_sha256),
            status=self.status,
            declared_logical_object_ids=self.declared_logical_object_ids,
            unresolved_logical_object_ids=self.unresolved_logical_object_ids,
            entries=self.entries,
            diagnostic_codes=self.diagnostic_codes,
        )
        if self.policy_attestation.attested_decision_sha256 != decision_sha256:
            raise ValueError(
                "D3 policy attestation does not bind the proposal decision"
            )
        return self


class ParentProvisionValidationReceiptV3(_StrictFrozenModel):
    schema_version: Literal["parent-provision-validation-receipt/v3"] = (
        PARENT_PROVISION_VALIDATION_RECEIPT_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    predecessor_d2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_d2_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_attestation_id: str = Field(pattern=r"^d3-policy-attestation-[0-9a-f]{32}$")
    policy_attestation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ParentProvisionStatus
    declared_scope_count: StrictInt = Field(ge=0)
    unresolved_scope_count: StrictInt = Field(ge=0)
    unresolved_entry_count: StrictInt = Field(ge=0)
    parent_provision_count: StrictInt = Field(ge=0)
    source_segment_count: StrictInt = Field(ge=0)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: Literal["reqpilotx-parent-provision-validator"] = (
        PARENT_PROVISION_VALIDATOR_ID
    )
    validator_version: Literal["1.0.0"] = PARENT_PROVISION_VALIDATOR_VERSION

    @model_validator(mode="after")
    def validate_receipt_hash(self) -> ParentProvisionValidationReceiptV3:
        expected_artifact_id = stable_parent_provision_artifact_id(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            proposal_sha256=self.artifact_sha256,
        )
        if self.artifact_id != expected_artifact_id:
            raise ValueError("D3 artifact ID does not bind its validated proposal")
        if self.status is ParentProvisionStatus.COMPLETE and (
            self.unresolved_scope_count or self.unresolved_entry_count
        ):
            raise ValueError("COMPLETE D3 receipt cannot claim unresolved capture")
        expected = canonical_json_sha256(
            self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("D3 validation receipt SHA must bind its payload")
        return self


def validate_parent_provision_proposal(
    *,
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
    parent_execution_receipt: D3ParentProvisionExecutionReceiptV1,
    parent_policy_attestation: D3ParentProvisionPolicyAttestationV1,
    proposal: ParentProvisionProposalV3,
) -> ParentProvisionValidationReceiptV3:
    """Fail closed on source invention, stale lineage, or incomplete scope capture."""

    decision_sha256 = parent_provision_decision_sha256(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        kit_identity=proposal.kit_identity,
        predecessor_d2_artifact_id=proposal.predecessor_d2_artifact_id,
        predecessor_d2_artifact_sha256=proposal.predecessor_d2_artifact_sha256,
        status=proposal.status,
        declared_logical_object_ids=proposal.declared_logical_object_ids,
        unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
        entries=proposal.entries,
        diagnostic_codes=proposal.diagnostic_codes,
    )
    candidate_sha256 = parent_provision_decision_candidate_sha256(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        kit_identity=proposal.kit_identity,
        predecessor_d2_artifact_id=proposal.predecessor_d2_artifact_id,
        predecessor_d2_artifact_sha256=proposal.predecessor_d2_artifact_sha256,
        status=proposal.status,
        declared_logical_object_ids=proposal.declared_logical_object_ids,
        unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
        entries=proposal.entries,
        diagnostic_codes=proposal.diagnostic_codes,
    )
    evidence_refs = parent_provision_evidence_refs(
        declared_logical_object_ids=proposal.declared_logical_object_ids,
        unresolved_logical_object_ids=proposal.unresolved_logical_object_ids,
        entries=proposal.entries,
    )
    if (
        parent_execution_receipt.decision_candidate_sha256 != candidate_sha256
        or parent_policy_attestation.decision_candidate_sha256 != candidate_sha256
    ):
        raise ValueError("D3 direct attestation does not bind the decision candidate")
    if (
        parent_execution_receipt.evidence_refs != evidence_refs
        or parent_policy_attestation.evidence_refs != evidence_refs
    ):
        raise ValueError("D3 direct attestation evidence differs from the proposal")
    expected_policy_attestation = build_professional_policy_attestation_identity(
        parent_execution_receipt=parent_execution_receipt,
        parent_policy_attestation=parent_policy_attestation,
        attested_decision_sha256=decision_sha256,
    )
    _validate_lineage(d2_identity, expected_policy_attestation, proposal)
    _validate_source_scope(d2_identity, source_segments)

    segment_order = {
        value: index for index, value in enumerate(d2_identity.source_segment_ids)
    }
    segments_by_id = {segment.segment_id: segment for segment in source_segments}
    segments_by_object: dict[str, tuple[D3ExactSourceSegmentV1, ...]] = {
        object_id: tuple(
            segment
            for segment in source_segments
            if segment.logical_object_id == object_id
        )
        for object_id in d2_identity.logical_object_ids
    }

    expected_declared = tuple(
        value
        for value in d2_identity.logical_object_ids
        if value in set(proposal.declared_logical_object_ids)
    )
    expected_unresolved = tuple(
        value
        for value in d2_identity.logical_object_ids
        if value in set(proposal.unresolved_logical_object_ids)
    )
    if proposal.declared_logical_object_ids != expected_declared or (
        proposal.unresolved_logical_object_ids != expected_unresolved
    ):
        raise ValueError("D3 scopes must preserve validated D2 logical-object order")
    if set(expected_declared) | set(expected_unresolved) != set(
        d2_identity.logical_object_ids
    ):
        raise ValueError("D3 scopes must disposition every validated D2 logical object")

    unresolved_classification = any(
        entry.requirementness is Requirementness.UNRESOLVED
        for entry in proposal.entries
    )
    expected_status = (
        ParentProvisionStatus.COMPLETE
        if d2_identity.status is ParentProvisionStatus.COMPLETE
        and not expected_unresolved
        and not unresolved_classification
        else ParentProvisionStatus.PARTIAL
    )
    if proposal.status is not expected_status:
        raise ValueError("D3 COMPLETE/PARTIAL status does not match capture scope")

    coverage = {
        segment.segment_id: bytearray(len(segment.source_text))
        for segment in source_segments
    }
    entry_anchor_order: list[tuple[int, int, int]] = []
    allowed_evidence_refs = set(d2_identity.logical_object_ids)
    for segment in source_segments:
        allowed_evidence_refs.update(
            {segment.segment_id, segment.source_handle, *segment.evidence_refs}
        )

    for entry in proposal.entries:
        if any(value not in expected_declared for value in entry.logical_object_ids):
            raise ValueError("D3 parent references an undeclared logical object")
        range_objects: set[str] = set()
        entry_grounding_refs: set[str] = set(entry.logical_object_ids)
        local_order: list[tuple[int, int, int]] = []
        for source_range in entry.source_ranges:
            segment = segments_by_id.get(source_range.segment_id)
            if segment is None:
                raise ValueError("D3 parent range references an unknown source segment")
            if segment.logical_object_id not in expected_declared:
                raise ValueError(
                    "D3 parent range references an unresolved source scope"
                )
            if source_range.end_offset > len(segment.source_text):
                raise ValueError("D3 parent range exceeds its exact source segment")
            source_slice = segment.source_text[
                source_range.start_offset : source_range.end_offset
            ]
            if source_range.verbatim_text != source_slice:
                raise ValueError("D3 parent range invented or rewrote source text")
            observed = coverage[source_range.segment_id]
            for offset in range(source_range.start_offset, source_range.end_offset):
                if observed[offset]:
                    raise ValueError("D3 parent source ranges overlap")
                observed[offset] = 1
            key = (
                segment_order[source_range.segment_id],
                source_range.start_offset,
                source_range.end_offset,
            )
            local_order.append(key)
            range_objects.add(segment.logical_object_id)
            entry_grounding_refs.update({segment.segment_id, segment.source_handle})
        if local_order != sorted(local_order):
            raise ValueError("D3 parent ranges must preserve D2 source order")
        entry_anchor_order.append(local_order[0])
        exact_object_ids = tuple(
            object_id
            for object_id in d2_identity.logical_object_ids
            if object_id in range_objects
        )
        if entry.logical_object_ids != exact_object_ids:
            raise ValueError("D3 parent logical objects must exactly own its ranges")
        if any(value not in allowed_evidence_refs for value in entry.evidence_refs):
            raise ValueError("D3 parent evidence is not source/D2 bound")
        if not entry_grounding_refs.intersection(entry.evidence_refs):
            raise ValueError("D3 parent evidence does not ground its own source scope")
        expected_parent_id = stable_parent_provision_id(
            source_sha256=proposal.source_sha256,
            d2_artifact_sha256=proposal.predecessor_d2_artifact_sha256,
            logical_object_ids=entry.logical_object_ids,
            source_ranges=entry.source_ranges,
        )
        if entry.parent_provision_id != expected_parent_id:
            raise ValueError("D3 parent ID does not bind its exact source ranges")

    if entry_anchor_order != sorted(entry_anchor_order):
        raise ValueError("D3 parents must preserve global D2 source order")

    for object_id in expected_declared:
        for segment in segments_by_object[object_id]:
            observed = coverage[segment.segment_id]
            if any(
                not character.isspace() and not observed[offset]
                for offset, character in enumerate(segment.source_text)
            ):
                raise ValueError(
                    "D3 proposal does not completely disposition declared source scope"
                )
    for object_id in expected_unresolved:
        for segment in segments_by_object[object_id]:
            if any(coverage[segment.segment_id]):
                raise ValueError("unresolved D3 scope cannot contain parent ranges")

    proposal_sha256 = canonical_json_sha256(proposal.model_dump(mode="json"))
    evidence_sha256 = canonical_json_sha256(
        {
            "d2_identity": d2_identity.model_dump(mode="json"),
            "source_scope_sha256": d2_identity.source_scope_sha256,
            "policy_attestation": expected_policy_attestation.model_dump(mode="json"),
            "coverage": [
                {
                    "segment_id": segment.segment_id,
                    "source_text_sha256": segment.source_text_sha256,
                    "covered_ranges": _covered_ranges(coverage[segment.segment_id]),
                }
                for segment in source_segments
            ],
        }
    )
    artifact_id = stable_parent_provision_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal_sha256,
    )
    receipt_payload = {
        "schema_version": PARENT_PROVISION_VALIDATION_RECEIPT_SCHEMA_VERSION,
        "artifact_id": artifact_id,
        "artifact_sha256": proposal_sha256,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "kit_bundle_sha256": proposal.kit_identity.bundle_sha256,
        "predecessor_d2_artifact_id": proposal.predecessor_d2_artifact_id,
        "predecessor_d2_artifact_sha256": (proposal.predecessor_d2_artifact_sha256),
        "policy_attestation_id": proposal.policy_attestation.attestation_id,
        "policy_attestation_sha256": proposal.policy_attestation.attestation_sha256,
        "status": proposal.status,
        "declared_scope_count": len(expected_declared),
        "unresolved_scope_count": len(expected_unresolved),
        "unresolved_entry_count": sum(
            entry.requirementness is Requirementness.UNRESOLVED
            for entry in proposal.entries
        ),
        "parent_provision_count": len(proposal.entries),
        "source_segment_count": sum(
            len(segments_by_object[object_id]) for object_id in expected_declared
        ),
        "evidence_sha256": evidence_sha256,
        "validator_id": PARENT_PROVISION_VALIDATOR_ID,
        "validator_version": PARENT_PROVISION_VALIDATOR_VERSION,
    }
    return ParentProvisionValidationReceiptV3(
        **receipt_payload,
        receipt_sha256=canonical_json_sha256(
            {
                **receipt_payload,
                "status": proposal.status.value,
            }
        ),
    )


def compile_validated_d2_identity(
    *,
    d2_proposal: D2LogicalStructureProposalV1,
    d2_receipt: D2LogicalStructureValidationReceiptV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> D3ValidatedD2ArtifactIdentityV1:
    """Compile D3's narrow predecessor view from the actual validated D2 pair."""

    if (
        d2_receipt.run_id != d2_proposal.run_id
        or d2_receipt.source_sha256 != d2_proposal.source_sha256
        or d2_receipt.source_ir_id != d2_proposal.source_ir_id
        or d2_receipt.source_ir_canonical_sha256
        != d2_proposal.source_ir_canonical_sha256
        or d2_receipt.kit_identity != d2_proposal.kit_identity
        or d2_receipt.proposal_sha256 != d2_proposal.proposal_sha256
        or d2_receipt.artifact_sha256 != d2_proposal.proposal_sha256
        or d2_receipt.status.value != d2_proposal.status.value
    ):
        raise ValueError("D3 cannot consume a stale D2 proposal/receipt pair")
    logical_object_ids = tuple(
        item.logical_object_id for item in d2_proposal.logical_objects
    )
    d2_segments = tuple(
        (logical_object.logical_object_id, segment)
        for logical_object in d2_proposal.logical_objects
        for segment in logical_object.source_segments
    )
    source_segment_ids = tuple(segment.segment_id for _, segment in d2_segments)
    if (
        d2_receipt.logical_object_ids != logical_object_ids
        or d2_receipt.source_segment_ids != source_segment_ids
    ):
        raise ValueError("D3 predecessor identity differs from validated D2 scope")
    if tuple(item.segment_id for item in source_segments) != source_segment_ids:
        raise ValueError("D3 exact source segments differ from validated D2 order")
    for resolved, (logical_object_id, segment) in zip(
        source_segments,
        d2_segments,
        strict=True,
    ):
        if (
            resolved.logical_object_id != logical_object_id
            or resolved.source_handle != segment.source_handle
            or resolved.handle_kind != segment.handle_kind.value
            or resolved.owner_source_node_id != segment.owner_source_node_id
            or resolved.source_order != segment.source_order
            or resolved.page_numbers != segment.page_numbers
            or resolved.handle_start_offset != segment.start_offset
            or resolved.handle_end_offset != segment.end_offset
            or resolved.source_content_sha256 != segment.source_content_sha256
            or resolved.d2_segment_sha256 != segment.segment_sha256
        ):
            raise ValueError("D3 exact source segment drifted from its D2 handle")
    return D3ValidatedD2ArtifactIdentityV1(
        artifact_id=d2_receipt.artifact_id,
        artifact_sha256=d2_receipt.artifact_sha256,
        validator_receipt_sha256=d2_receipt.receipt_sha256,
        run_id=d2_receipt.run_id,
        source_sha256=d2_receipt.source_sha256,
        source_ir_id=d2_receipt.source_ir_id,
        source_ir_canonical_sha256=d2_receipt.source_ir_canonical_sha256,
        kit_identity=d2_receipt.kit_identity,
        status=ParentProvisionStatus(d2_receipt.status.value),
        logical_object_ids=logical_object_ids,
        source_segment_ids=source_segment_ids,
        source_scope_sha256=d3_source_scope_sha256(source_segments),
    )


def build_professional_policy_attestation_identity(
    *,
    parent_execution_receipt: D3ParentProvisionExecutionReceiptV1,
    parent_policy_attestation: D3ParentProvisionPolicyAttestationV1,
    attested_decision_sha256: str,
) -> D3ProfessionalPolicyAttestationIdentityV1:
    if (
        not parent_policy_attestation.policy_attested
        or parent_policy_attestation.diagnostic_codes
    ):
        raise ValueError("D3 professional parent-provision policy is not attested")
    execution_receipt_sha256 = canonical_json_sha256(
        parent_execution_receipt.model_dump(mode="json")
    )
    if parent_policy_attestation.execution_receipt_sha256 != execution_receipt_sha256:
        raise ValueError("D3 parent attestation execution receipt drifted")
    policy_closure_sha256 = canonical_json_sha256(
        parent_execution_receipt.loaded_policy_closure.model_dump(mode="json")
    )
    if (
        parent_policy_attestation.kit_bundle_sha256
        != parent_execution_receipt.kit_identity.bundle_sha256
        or parent_policy_attestation.agent_id != parent_execution_receipt.agent_id
        or parent_policy_attestation.run_id != parent_execution_receipt.run_id
        or parent_policy_attestation.source_sha256
        != parent_execution_receipt.source_sha256
        or parent_policy_attestation.d2_artifact_id
        != parent_execution_receipt.d2_artifact_id
        or parent_policy_attestation.d2_artifact_sha256
        != parent_execution_receipt.d2_artifact_sha256
        or parent_policy_attestation.policy_closure_sha256 != policy_closure_sha256
        or parent_policy_attestation.stage_session_attestation_sha256
        != parent_execution_receipt.stage_session_attestation_sha256
        or parent_policy_attestation.execution_trace_sha256
        != parent_execution_receipt.execution_trace_sha256
        or parent_policy_attestation.decision_candidate_sha256
        != parent_execution_receipt.decision_candidate_sha256
        or parent_policy_attestation.evidence_refs
        != parent_execution_receipt.evidence_refs
    ):
        raise ValueError("D3 parent attestation lineage drifted from execution")
    if (
        parent_execution_receipt.parent_provision_decision_sha256
        != attested_decision_sha256
        or parent_policy_attestation.parent_provision_decision_sha256
        != attested_decision_sha256
    ):
        raise ValueError("D3 professional execution did not attest this decision")
    parent_attestation_sha256 = canonical_json_sha256(
        parent_policy_attestation.model_dump(mode="json")
    )
    identity_payload = {
        "schema_version": D3_POLICY_ATTESTATION_IDENTITY_SCHEMA_VERSION,
        "parent_provision_attestation_id": (parent_policy_attestation.attestation_id),
        "parent_provision_attestation_sha256": parent_attestation_sha256,
        "parent_provision_policy_attested": True,
        "source_sha256": parent_policy_attestation.source_sha256,
        "d2_artifact_id": parent_policy_attestation.d2_artifact_id,
        "d2_artifact_sha256": parent_policy_attestation.d2_artifact_sha256,
        "kit_bundle_sha256": parent_policy_attestation.kit_bundle_sha256,
        "agent_id": parent_policy_attestation.agent_id,
        "policy_closure_sha256": parent_policy_attestation.policy_closure_sha256,
        "execution_receipt_sha256": execution_receipt_sha256,
        "attested_decision_sha256": attested_decision_sha256,
    }
    identity_sha256 = canonical_json_sha256(identity_payload)
    return D3ProfessionalPolicyAttestationIdentityV1(
        **identity_payload,
        attestation_id=f"d3-policy-attestation-{identity_sha256[:32]}",
        attestation_sha256=identity_sha256,
    )


def parent_provision_decision_sha256(
    *,
    run_id: str,
    source_sha256: str,
    kit_identity: AgentKitIdentityV1,
    predecessor_d2_artifact_id: str,
    predecessor_d2_artifact_sha256: str,
    status: ParentProvisionStatus,
    declared_logical_object_ids: tuple[str, ...],
    unresolved_logical_object_ids: tuple[str, ...],
    entries: tuple[ParentProvisionEntryV3, ...],
    diagnostic_codes: tuple[str, ...],
) -> str:
    return canonical_json_sha256(
        {
            "run_id": run_id,
            "source_sha256": source_sha256,
            "kit_identity": kit_identity.model_dump(mode="json"),
            "predecessor_d2_artifact_id": predecessor_d2_artifact_id,
            "predecessor_d2_artifact_sha256": predecessor_d2_artifact_sha256,
            "status": status.value,
            "declared_logical_object_ids": declared_logical_object_ids,
            "unresolved_logical_object_ids": unresolved_logical_object_ids,
            "entries": [entry.model_dump(mode="json") for entry in entries],
            "diagnostic_codes": diagnostic_codes,
        }
    )


def _validate_parent_decision_shape(
    *,
    status: ParentProvisionStatus,
    declared_logical_object_ids: tuple[str, ...],
    unresolved_logical_object_ids: tuple[str, ...],
    entries: tuple[ParentProvisionEntryV3, ...],
    diagnostic_codes: tuple[str, ...],
) -> None:
    for values in (declared_logical_object_ids, unresolved_logical_object_ids):
        if len(values) != len(set(values)) or any(not item.strip() for item in values):
            raise ValueError("D3 logical-object scopes must be unique and non-empty")
    if diagnostic_codes != tuple(sorted(set(diagnostic_codes))) or any(
        re.fullmatch(r"[A-Z][A-Z0-9_]{0,119}", item) is None
        for item in diagnostic_codes
    ):
        raise ValueError("D3 diagnostics must be sorted bounded machine codes")
    if set(declared_logical_object_ids) & set(unresolved_logical_object_ids):
        raise ValueError("declared and unresolved D3 scopes must be disjoint")
    if tuple(entry.source_order for entry in entries) != tuple(range(len(entries))):
        raise ValueError("D3 parent entries must use contiguous source order")
    parent_ids = tuple(entry.parent_provision_id for entry in entries)
    if len(parent_ids) != len(set(parent_ids)):
        raise ValueError("D3 parent provision IDs must be unique")
    if status is ParentProvisionStatus.COMPLETE and (
        unresolved_logical_object_ids or diagnostic_codes
    ):
        raise ValueError("COMPLETE D3 cannot hide unresolved capture scopes")
    if status is ParentProvisionStatus.PARTIAL and not (
        unresolved_logical_object_ids or diagnostic_codes
    ):
        raise ValueError("PARTIAL D3 requires explicit capture diagnostics")


def parent_provision_decision_candidate_sha256(
    *,
    run_id: str,
    source_sha256: str,
    kit_identity: AgentKitIdentityV1,
    predecessor_d2_artifact_id: str,
    predecessor_d2_artifact_sha256: str,
    status: ParentProvisionStatus,
    declared_logical_object_ids: tuple[str, ...],
    unresolved_logical_object_ids: tuple[str, ...],
    entries: tuple[ParentProvisionEntryV3, ...],
    diagnostic_codes: tuple[str, ...],
) -> str:
    return canonical_json_sha256(
        {
            "schema_version": (D3_PARENT_PROVISION_DECISION_CANDIDATE_SCHEMA_VERSION),
            "run_id": run_id,
            "source_sha256": source_sha256,
            "kit_identity": kit_identity.model_dump(mode="json"),
            "predecessor_d2_artifact_id": predecessor_d2_artifact_id,
            "predecessor_d2_artifact_sha256": predecessor_d2_artifact_sha256,
            "status": status.value,
            "declared_logical_object_ids": declared_logical_object_ids,
            "unresolved_logical_object_ids": unresolved_logical_object_ids,
            "entries": [entry.model_dump(mode="json") for entry in entries],
            "diagnostic_codes": diagnostic_codes,
        }
    )


def parent_provision_evidence_refs(
    *,
    declared_logical_object_ids: tuple[str, ...],
    unresolved_logical_object_ids: tuple[str, ...],
    entries: tuple[ParentProvisionEntryV3, ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                *declared_logical_object_ids,
                *unresolved_logical_object_ids,
                *(value for entry in entries for value in entry.logical_object_ids),
                *(value for entry in entries for value in entry.evidence_refs),
                *(
                    source_range.segment_id
                    for entry in entries
                    for source_range in entry.source_ranges
                ),
            }
        )
    )


def stable_parent_provision_id(
    *,
    source_sha256: str,
    d2_artifact_sha256: str,
    logical_object_ids: tuple[str, ...],
    source_ranges: tuple[ParentProvisionSourceRangeV3, ...],
) -> str:
    digest = canonical_json_sha256(
        {
            "source_sha256": source_sha256,
            "d2_artifact_sha256": d2_artifact_sha256,
            "logical_object_ids": logical_object_ids,
            "source_ranges": [item.model_dump(mode="json") for item in source_ranges],
        }
    )
    return f"parent-provision-{digest[:32]}"


def stable_parent_provision_artifact_id(
    *,
    run_id: str,
    source_sha256: str,
    proposal_sha256: str,
) -> str:
    digest = canonical_json_sha256(
        {
            "run_id": run_id,
            "source_sha256": source_sha256,
            "stage": "D3_PARENT_PROVISION_RECOGNITION",
            "proposal_sha256": proposal_sha256,
        }
    )
    return f"agent-artifact-{digest[:32]}"


def d3_source_scope_sha256(
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> str:
    return canonical_json_sha256(
        [
            segment.model_dump(mode="json", exclude={"source_text"})
            for segment in source_segments
        ]
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


def _validate_lineage(
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    expected_policy_attestation: D3ProfessionalPolicyAttestationIdentityV1,
    proposal: ParentProvisionProposalV3,
) -> None:
    if (
        proposal.run_id != d2_identity.run_id
        or proposal.source_sha256 != d2_identity.source_sha256
        or proposal.predecessor_d2_artifact_id != d2_identity.artifact_id
        or proposal.predecessor_d2_artifact_sha256 != d2_identity.artifact_sha256
    ):
        raise ValueError("D3 proposal source/D2 lineage is stale")
    if proposal.kit_identity != d2_identity.kit_identity:
        raise ValueError("D3 proposal Kit identity drifted from validated D2")
    if proposal.policy_attestation != expected_policy_attestation:
        raise ValueError("D3 proposal did not use the Host-pinned policy attestation")
    if (
        expected_policy_attestation.source_sha256 != d2_identity.source_sha256
        or expected_policy_attestation.d2_artifact_id != d2_identity.artifact_id
        or expected_policy_attestation.d2_artifact_sha256 != d2_identity.artifact_sha256
        or expected_policy_attestation.kit_bundle_sha256
        != d2_identity.kit_identity.bundle_sha256
    ):
        raise ValueError("D3 professional policy attestation lineage is stale")


def _validate_source_scope(
    d2_identity: D3ValidatedD2ArtifactIdentityV1,
    source_segments: tuple[D3ExactSourceSegmentV1, ...],
) -> None:
    if tuple(segment.segment_id for segment in source_segments) != (
        d2_identity.source_segment_ids
    ):
        raise ValueError("D3 source segments do not match validated D2 source order")
    if any(
        segment.logical_object_id not in d2_identity.logical_object_ids
        for segment in source_segments
    ):
        raise ValueError("D3 source segment has no validated D2 logical owner")
    if d3_source_scope_sha256(source_segments) != d2_identity.source_scope_sha256:
        raise ValueError("D3 source segment scope drifted from validated D2")


def _covered_ranges(observed: bytearray) -> list[list[int]]:
    ranges: list[list[int]] = []
    start: int | None = None
    for offset, value in enumerate((*observed, 0)):
        if value and start is None:
            start = offset
        elif not value and start is not None:
            ranges.append([start, offset])
            start = None
    return ranges


__all__ = [
    "D3ExactSourceSegmentV1",
    "D3ProfessionalPolicyAttestationIdentityV1",
    "D3ValidatedD2ArtifactIdentityV1",
    "PARENT_PROVISION_ENTRY_EVIDENCE_REF_LIMIT",
    "ParentProvisionEntryV3",
    "ParentProvisionProposalV3",
    "ParentProvisionSourceRangeV3",
    "ParentProvisionStatus",
    "ParentProvisionValidationReceiptV3",
    "ProvisionContentRole",
    "RequirementKind",
    "RequirementTechnicality",
    "Requirementness",
    "build_professional_policy_attestation_identity",
    "canonical_json_sha256",
    "compile_validated_d2_identity",
    "d3_source_scope_sha256",
    "parent_provision_decision_sha256",
    "parent_provision_decision_candidate_sha256",
    "parent_provision_evidence_refs",
    "stable_parent_provision_id",
    "stable_parent_provision_artifact_id",
    "validate_parent_provision_proposal",
]
