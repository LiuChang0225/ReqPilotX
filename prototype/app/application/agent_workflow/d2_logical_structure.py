from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.extraction_runtime import (
    AgentExtractionStage,
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
    AgentStageArtifactV1,
)
from app.application.agent_workflow.document_understanding import (
    DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION,
    DocumentUnderstandingProposalV2,
    DocumentUnderstandingStatus,
    DocumentUnderstandingValidationReceiptV2,
    canonical_json_sha256,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeV1,
    source_ir_canonical_sha256,
    validate_agent_logical_table_scope,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)
from app.domain.extraction.structure_revision import canonical_model_sha256


D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION = "logical-structure-proposal/v1"
D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_SCHEMA_VERSION = (
    "logical-structure-validation-receipt/v1"
)
D2_LOGICAL_STRUCTURE_VALIDATOR_ID = "reqpilotx-logical-structure-validator"
D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION = "1.1.0"
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2LogicalStructureStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class D2LogicalObjectKind(StrEnum):
    DOCUMENT_UNIT = "DOCUMENT_UNIT"
    SECTION = "SECTION"
    PROVISION_SCOPE = "PROVISION_SCOPE"
    TABLE = "TABLE"
    FIGURE = "FIGURE"
    ATTACHMENT = "ATTACHMENT"
    CONTEXT = "CONTEXT"
    OTHER = "OTHER"


class D2SourceHandleKind(StrEnum):
    SOURCE_NODE = "SOURCE_NODE"
    TABLE_CELL = "TABLE_CELL"


class D2PhysicalTableDispositionKind(StrEnum):
    LOGICAL_SCOPE_PART = "LOGICAL_SCOPE_PART"
    NESTED_VALUE_TABLE = "NESTED_VALUE_TABLE"
    NON_BUSINESS_FALSE_TABLE = "NON_BUSINESS_FALSE_TABLE"
    UNRESOLVED = "UNRESOLVED"


class D2SourceEvidenceDispositionKind(StrEnum):
    LOGICAL_OBJECT_PART = "LOGICAL_OBJECT_PART"
    NESTED_EVIDENCE = "NESTED_EVIDENCE"
    NON_BUSINESS_SOURCE = "NON_BUSINESS_SOURCE"
    PAGE_FURNITURE = "PAGE_FURNITURE"
    UNRESOLVED = "UNRESOLVED"


class D2ValidatedD1ArtifactIdentityV1(_StrictFrozenModel):
    """Host-issued identity of the D1 artifact accepted as D2 input."""

    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    stage: Literal[AgentExtractionStage.DOCUMENT_UNDERSTANDING] = (
        AgentExtractionStage.DOCUMENT_UNDERSTANDING
    )
    status: Literal[
        AgentStageArtifactStatus.VALIDATED,
        AgentStageArtifactStatus.PARTIAL,
    ]
    proposal_schema_id: Literal["document-understanding-proposal/v2"] = (
        "document-understanding-proposal/v2"
    )
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_count: StrictInt = Field(ge=1)
    kit_bundle_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class D2SourceSegmentV1(_StrictFrozenModel):
    """Content-free, exact handle/range evidence owned by one D2 object."""

    segment_id: str = Field(pattern=r"^d2-source-segment-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    handle_kind: D2SourceHandleKind
    source_handle: str = Field(min_length=1, max_length=160)
    owner_source_node_id: str = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)
    source_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    segment_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_handle", "owner_source_node_id")
    @classmethod
    def reject_blank_handles(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 source handles must not be blank")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("D2 source segment pages must be positive and sorted")
        return value

    @model_validator(mode="after")
    def validate_segment(self) -> D2SourceSegmentV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("D2 source segment offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("D2 source segment range must be non-empty")
        expected_id = stable_d2_source_segment_id(
            source_sha256=self.source_sha256,
            handle_kind=self.handle_kind,
            source_handle=self.source_handle,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
        )
        if self.segment_id != expected_id:
            raise ValueError("D2 source segment ID does not bind its source range")
        expected_sha = stable_d2_source_segment_sha256(
            segment_id=self.segment_id,
            source_sha256=self.source_sha256,
            handle_kind=self.handle_kind,
            source_handle=self.source_handle,
            owner_source_node_id=self.owner_source_node_id,
            source_order=self.source_order,
            page_numbers=self.page_numbers,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            source_content_sha256=self.source_content_sha256,
        )
        if self.segment_sha256 != expected_sha:
            raise ValueError("D2 source segment SHA does not bind its evidence")
        return self


class D2LogicalObjectV1(_StrictFrozenModel):
    """Minimal D2 object boundary; source text remains in SourceIR."""

    logical_object_id: str = Field(
        pattern=r"^(?:d2-logical-object|agent-logical-table)-[0-9a-f]{32}$"
    )
    object_kind: D2LogicalObjectKind
    parent_logical_object_id: str | None = Field(
        default=None,
        pattern=r"^(?:d2-logical-object|agent-logical-table)-[0-9a-f]{32}$",
    )
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = Field(min_length=1)
    source_segments: tuple[D2SourceSegmentV1, ...] = ()
    logical_table_scope_ids: tuple[str, ...] = Field(
        default=(),
        max_length=1,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("D2 logical-object pages must be positive and sorted")
        return value

    @field_validator("logical_table_scope_ids")
    @classmethod
    def validate_canonical_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 logical-object values must be non-empty and sorted")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("D2 reason codes must be sorted lowercase snake_case")
        return value

    @model_validator(mode="after")
    def validate_object(self) -> D2LogicalObjectV1:
        if self.parent_logical_object_id == self.logical_object_id:
            raise ValueError("a D2 logical object cannot own itself")
        if (
            not self.source_segments
            and not self.logical_table_scope_ids
            and self.object_kind
            not in {
                D2LogicalObjectKind.DOCUMENT_UNIT,
                D2LogicalObjectKind.SECTION,
                D2LogicalObjectKind.ATTACHMENT,
                D2LogicalObjectKind.CONTEXT,
            }
        ):
            raise ValueError(
                "only D2 containers may derive evidence from child objects"
            )
        if bool(self.logical_table_scope_ids) != (
            self.object_kind is D2LogicalObjectKind.TABLE
        ):
            raise ValueError("only D2 TABLE objects bind one logical-table scope")
        if self.logical_table_scope_ids and self.logical_table_scope_ids != (
            self.logical_object_id,
        ):
            raise ValueError("a D2 TABLE object identity must equal its scope identity")
        segment_order = tuple(
            (
                item.source_order,
                item.source_handle,
                -1 if item.start_offset is None else item.start_offset,
                item.segment_id,
            )
            for item in self.source_segments
        )
        if segment_order != tuple(sorted(segment_order)):
            raise ValueError("D2 source segments must follow canonical source order")
        if len({item.segment_id for item in self.source_segments}) != len(
            self.source_segments
        ):
            raise ValueError("a D2 logical object cannot repeat a source segment")
        expected_sha = stable_d2_logical_object_sha256(
            logical_object_id=self.logical_object_id,
            object_kind=self.object_kind,
            parent_logical_object_id=self.parent_logical_object_id,
            source_order=self.source_order,
            page_numbers=self.page_numbers,
            source_segments=self.source_segments,
            logical_table_scope_ids=self.logical_table_scope_ids,
            reason_codes=self.reason_codes,
        )
        if self.object_sha256 != expected_sha:
            raise ValueError("D2 logical-object SHA does not bind its boundary")
        return self


class D2PhysicalTableDispositionV1(_StrictFrozenModel):
    table_id: str = Field(min_length=1, max_length=160)
    disposition: D2PhysicalTableDispositionKind
    logical_table_scope_id: str | None = Field(
        default=None,
        pattern=r"^agent-logical-table-[0-9a-f]{32}$",
    )
    nested_owner_table_id: str | None = Field(default=None, max_length=160)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    disposition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("table_id", "nested_owner_table_id")
    @classmethod
    def reject_blank_ids(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("D2 table IDs must not be blank")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_canonical_strings(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 table evidence must be non-empty and sorted")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("D2 reason codes must be sorted lowercase snake_case")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> D2PhysicalTableDispositionV1:
        needs_scope = (
            self.disposition is D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART
        )
        needs_owner = (
            self.disposition is D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE
        )
        if bool(self.logical_table_scope_id) != needs_scope:
            raise ValueError("LOGICAL_SCOPE_PART requires exactly one scope binding")
        if bool(self.nested_owner_table_id) != needs_owner:
            raise ValueError("NESTED_VALUE_TABLE requires exactly one owner table")
        if self.nested_owner_table_id == self.table_id:
            raise ValueError("a nested value table cannot own itself")
        expected_sha = stable_d2_table_disposition_sha256(
            table_id=self.table_id,
            disposition=self.disposition,
            logical_table_scope_id=self.logical_table_scope_id,
            nested_owner_table_id=self.nested_owner_table_id,
            reason_codes=self.reason_codes,
            evidence_refs=self.evidence_refs,
        )
        if self.disposition_sha256 != expected_sha:
            raise ValueError("D2 table disposition SHA does not bind its decision")
        return self


class D2SourceEvidenceDispositionV1(_StrictFrozenModel):
    """Exactly-once D2 disposition for one non-table SourceIR range or asset."""

    disposition_id: str = Field(pattern=r"^d2-source-disposition-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_node_id: str = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)
    disposition: D2SourceEvidenceDispositionKind
    logical_object_id: str | None = Field(
        default=None,
        pattern=r"^(?:d2-logical-object|agent-logical-table)-[0-9a-f]{32}$",
    )
    source_segment_id: str | None = Field(
        default=None,
        pattern=r"^d2-source-segment-[0-9a-f]{32}$",
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=64)
    disposition_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_node_id")
    @classmethod
    def reject_blank_node_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 source disposition node must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("D2 reason codes must be sorted lowercase snake_case")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_evidence_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 source evidence refs must be non-empty and sorted")
        return value

    @model_validator(mode="after")
    def validate_disposition(self) -> D2SourceEvidenceDispositionV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("D2 source disposition offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("D2 source disposition range must be non-empty")
        object_bound = self.disposition in {
            D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART,
            D2SourceEvidenceDispositionKind.NESTED_EVIDENCE,
        }
        if (
            bool(self.logical_object_id) != object_bound
            or bool(self.source_segment_id) != object_bound
        ):
            raise ValueError(
                "object-bound D2 source dispositions require object and segment"
            )
        expected_id = stable_d2_source_disposition_id(
            source_sha256=self.source_sha256,
            source_node_id=self.source_node_id,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
        )
        if self.disposition_id != expected_id:
            raise ValueError("D2 source disposition ID does not bind its range")
        expected_sha = stable_d2_source_disposition_sha256(
            disposition_id=self.disposition_id,
            source_sha256=self.source_sha256,
            source_node_id=self.source_node_id,
            source_order=self.source_order,
            start_offset=self.start_offset,
            end_offset=self.end_offset,
            disposition=self.disposition,
            logical_object_id=self.logical_object_id,
            source_segment_id=self.source_segment_id,
            reason_codes=self.reason_codes,
            evidence_refs=self.evidence_refs,
        )
        if self.disposition_sha256 != expected_sha:
            raise ValueError("D2 source disposition SHA does not bind its decision")
        return self


class D2LogicalStructureProposalV1(_StrictFrozenModel):
    schema_version: Literal["logical-structure-proposal/v1"] = (
        D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    predecessor_d1_artifact: D2ValidatedD1ArtifactIdentityV1
    d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1
    status: D2LogicalStructureStatus
    logical_objects: tuple[D2LogicalObjectV1, ...] = Field(min_length=1)
    logical_table_scopes: tuple[AgentLogicalTableScopeV1, ...] = ()
    source_evidence_dispositions: tuple[D2SourceEvidenceDispositionV1, ...] = ()
    physical_table_dispositions: tuple[D2PhysicalTableDispositionV1, ...] = ()
    unresolved_codes: tuple[str, ...] = ()
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_ir_id")
    @classmethod
    def reject_blank_source_ir_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 SourceIR ID must not be blank")
        return value

    @field_validator("unresolved_codes")
    @classmethod
    def validate_unresolved_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            not item.strip() for item in value
        ):
            raise ValueError("D2 unresolved codes must be non-empty and sorted")
        return value

    @model_validator(mode="after")
    def validate_proposal_shape(self) -> D2LogicalStructureProposalV1:
        if (
            self.predecessor_d1_artifact.run_id != self.run_id
            or self.predecessor_d1_artifact.source_sha256 != self.source_sha256
            or self.d1_table_map_artifact.source_sha256 != self.source_sha256
        ):
            raise ValueError("D2 proposal source/D1 lineage is inconsistent")
        if (
            self.predecessor_d1_artifact.kit_bundle_sha256
            != self.kit_identity.bundle_sha256
        ):
            raise ValueError("D2 proposal Kit lineage drifted from D1")

        object_order = tuple(
            (item.source_order, item.logical_object_id) for item in self.logical_objects
        )
        if object_order != tuple(sorted(object_order)):
            raise ValueError("D2 logical objects must follow canonical source order")
        object_ids = tuple(item.logical_object_id for item in self.logical_objects)
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("D2 logical-object IDs must be unique")
        segment_ids = tuple(
            segment.segment_id
            for logical_object in self.logical_objects
            for segment in logical_object.source_segments
        )
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("D2 source segments require one logical-object owner")
        if any(
            segment.source_sha256 != self.source_sha256
            for logical_object in self.logical_objects
            for segment in logical_object.source_segments
        ):
            raise ValueError("D2 source segment belongs to another source")

        scope_order = tuple(
            (scope.physical_parts[0].source_order, scope.logical_object_id)
            for scope in self.logical_table_scopes
        )
        if scope_order != tuple(sorted(scope_order)):
            raise ValueError("D2 logical-table scopes must follow source order")
        scope_ids = tuple(
            scope.logical_object_id for scope in self.logical_table_scopes
        )
        if len(scope_ids) != len(set(scope_ids)):
            raise ValueError("D2 logical-table scope IDs must be unique")
        disposition_ids = tuple(
            item.table_id for item in self.physical_table_dispositions
        )
        if len(disposition_ids) != len(set(disposition_ids)):
            raise ValueError("D2 physical tables require exactly one disposition")

        source_disposition_order = tuple(
            (
                item.source_order,
                item.source_node_id,
                -1 if item.start_offset is None else item.start_offset,
                item.disposition_id,
            )
            for item in self.source_evidence_dispositions
        )
        if source_disposition_order != tuple(sorted(source_disposition_order)):
            raise ValueError("D2 source dispositions must follow source order")
        if len(
            {item.disposition_id for item in self.source_evidence_dispositions}
        ) != len(self.source_evidence_dispositions):
            raise ValueError("D2 source disposition IDs must be unique")

        expected_sha = stable_d2_logical_structure_proposal_sha256(self)
        if self.proposal_sha256 != expected_sha:
            raise ValueError("D2 proposal SHA does not bind the complete proposal")
        return self


class D2LogicalStructureValidationReceiptV1(_StrictFrozenModel):
    schema_version: Literal["logical-structure-validation-receipt/v1"] = (
        D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kit_identity: AgentKitIdentityV1
    predecessor_d1_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_d1_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: D2LogicalStructureStatus
    logical_object_ids: tuple[str, ...]
    source_segment_ids: tuple[str, ...]
    logical_table_scope_ids: tuple[str, ...]
    source_evidence_disposition_ids: tuple[str, ...]
    unresolved_source_evidence_disposition_ids: tuple[str, ...]
    physical_table_ids: tuple[str, ...]
    unresolved_table_ids: tuple[str, ...]
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validator_id: Literal["reqpilotx-logical-structure-validator"] = (
        D2_LOGICAL_STRUCTURE_VALIDATOR_ID
    )
    validator_version: Literal["1.1.0"] = D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION
    receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_receipt(self) -> D2LogicalStructureValidationReceiptV1:
        if self.artifact_sha256 != self.proposal_sha256:
            raise ValueError("D2 artifact SHA must equal the validated proposal SHA")
        expected_artifact_id = stable_d2_artifact_id(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            proposal_sha256=self.proposal_sha256,
        )
        if self.artifact_id != expected_artifact_id:
            raise ValueError("D2 artifact ID does not bind the proposal")
        payload = self.model_dump(mode="json", exclude={"receipt_sha256"})
        if self.receipt_sha256 != _canonical_sha256(payload):
            raise ValueError("D2 validation receipt SHA does not bind the receipt")
        return self


def validate_d2_logical_structure_proposal(
    source_ir: SourceIRSnapshot,
    proposal: D2LogicalStructureProposalV1,
    *,
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureValidationReceiptV1:
    """Host-validate one runner-neutral D2 proposal against immutable facts."""

    if source_ir.status is SourceIRStatus.FAILED:
        raise ValueError("D2 cannot consume a failed SourceIR snapshot")
    current_source_ir_sha = source_ir_canonical_sha256(source_ir)
    if (
        proposal.source_ir_id != source_ir.source_ir_id
        or proposal.source_sha256 != source_ir.source_sha256
        or proposal.source_ir_canonical_sha256 != current_source_ir_sha
    ):
        raise ValueError("D2 proposal belongs to another SourceIR snapshot")
    if proposal.predecessor_d1_artifact != expected_d1_artifact:
        raise ValueError("D2 predecessor D1 artifact identity drifted")
    if proposal.d1_table_map_artifact != expected_d1_table_map_artifact:
        raise ValueError("D2 table-map artifact identity drifted")
    if proposal.kit_identity != expected_kit_identity:
        raise ValueError("D2 Kit identity drifted")
    if expected_d1_artifact.source_sha256 != source_ir.source_sha256:
        raise ValueError("expected D1 artifact belongs to another source")
    if expected_d1_table_map_artifact.source_sha256 != source_ir.source_sha256:
        raise ValueError("expected D1 table-map artifact belongs to another source")

    table_catalog = _physical_table_catalog(source_ir)
    table_ids_in_order = tuple(table_catalog)
    disposition_ids = tuple(
        item.table_id for item in proposal.physical_table_dispositions
    )
    if disposition_ids != table_ids_in_order:
        raise ValueError("D2 must disposition every physical TableIR in source order")

    memberships: Counter[str] = Counter()
    scope_by_id: dict[str, AgentLogicalTableScopeV1] = {}
    for scope in proposal.logical_table_scopes:
        validate_agent_logical_table_scope(
            source_ir,
            scope,
            expected_d1_table_map_artifact=expected_d1_table_map_artifact,
        )
        scope_by_id[scope.logical_object_id] = scope
        memberships.update(scope.physical_table_ids)
    duplicated_memberships = tuple(
        table_id for table_id, count in memberships.items() if count != 1
    )
    if duplicated_memberships:
        raise ValueError("a physical TableIR cannot belong to multiple logical scopes")

    dispositions = {
        item.table_id: item for item in proposal.physical_table_dispositions
    }
    for table_id, item in dispositions.items():
        table_node = table_catalog[table_id]
        table = table_node.table_payload
        if table is None:  # pragma: no cover - catalog construction proves this.
            raise ValueError("D2 physical table lost its TableIR payload")
        table_bound_refs = {
            f"document-table-map:{table_id}",
            table_node.source_node_id,
            *(cell.cell_id for cell in table.cells),
        }
        if not table_bound_refs.intersection(item.evidence_refs):
            raise ValueError("D2 table disposition lacks table-bound evidence")
        if item.disposition is D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART:
            scope = scope_by_id.get(item.logical_table_scope_id or "")
            if scope is None or table_id not in scope.physical_table_ids:
                raise ValueError("LOGICAL_SCOPE_PART does not match scope membership")
        elif table_id in memberships:
            raise ValueError("only LOGICAL_SCOPE_PART may enter a logical-table scope")
        if item.disposition is D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE:
            owner = item.nested_owner_table_id or ""
            if owner not in table_catalog:
                raise ValueError("nested value table owner does not exist")
            if owner == table_id:
                raise ValueError("a nested value table cannot own itself")
            owner_disposition = dispositions.get(owner)
            if (
                owner_disposition is None
                or owner_disposition.disposition
                is not D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART
                or owner not in memberships
            ):
                raise ValueError(
                    "nested value table owner must be a validated logical-scope part"
                )
    if set(memberships) != {
        table_id
        for table_id, item in dispositions.items()
        if item.disposition is D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART
    }:
        raise ValueError("D2 logical-table membership and dispositions differ")

    object_by_id = {item.logical_object_id: item for item in proposal.logical_objects}
    for item in proposal.logical_objects:
        if (
            item.parent_logical_object_id is not None
            and item.parent_logical_object_id not in object_by_id
        ):
            raise ValueError("D2 logical-object parent does not exist")
    _reject_logical_object_cycles(object_by_id)
    for item in proposal.logical_objects:
        if any(page > expected_d1_artifact.page_count for page in item.page_numbers):
            raise ValueError("D2 logical-object page exceeds validated D1 scope")
        _validate_logical_object_identity(
            item,
            source_ir.source_sha256,
            object_by_id=object_by_id,
            scope_by_id=scope_by_id,
        )
        _validate_logical_object_pages(
            item,
            scope_by_id,
            object_by_id=object_by_id,
        )
        for segment in item.source_segments:
            _validate_source_segment(
                source_ir,
                segment,
                page_count=expected_d1_artifact.page_count,
            )
    _reject_overlapping_source_segments(proposal.logical_objects)
    unresolved_source_disposition_ids = _validate_source_evidence_dispositions(
        source_ir=source_ir,
        logical_objects=proposal.logical_objects,
        dispositions=proposal.source_evidence_dispositions,
    )

    table_object_ids = {
        item.logical_object_id
        for item in proposal.logical_objects
        if item.object_kind is D2LogicalObjectKind.TABLE
    }
    if table_object_ids != set(scope_by_id):
        raise ValueError("every logical-table scope requires exactly one TABLE object")

    unresolved_table_ids = tuple(
        item.table_id
        for item in proposal.physical_table_dispositions
        if item.disposition is D2PhysicalTableDispositionKind.UNRESOLVED
    )
    partial_inputs = (
        source_ir.status is SourceIRStatus.PARTIAL
        or expected_d1_artifact.status is AgentStageArtifactStatus.PARTIAL
        or bool(unresolved_table_ids)
        or bool(unresolved_source_disposition_ids)
        or bool(proposal.unresolved_codes)
    )
    if (proposal.status is D2LogicalStructureStatus.PARTIAL) != partial_inputs:
        raise ValueError("D2 COMPLETE/PARTIAL status is inconsistent with evidence")

    evidence_sha256 = _canonical_sha256(
        {
            "source_ir_canonical_sha256": current_source_ir_sha,
            "d1_artifact_sha256": expected_d1_artifact.artifact_sha256,
            "d1_table_map_artifact_sha256": (
                expected_d1_table_map_artifact.artifact_sha256
            ),
            "source_segment_sha256s": [
                segment.segment_sha256
                for item in proposal.logical_objects
                for segment in item.source_segments
            ],
            "logical_table_scope_sha256s": [
                scope.scope_sha256 for scope in proposal.logical_table_scopes
            ],
            "source_evidence_disposition_sha256s": [
                item.disposition_sha256
                for item in proposal.source_evidence_dispositions
            ],
            "table_disposition_sha256s": [
                item.disposition_sha256 for item in proposal.physical_table_dispositions
            ],
        }
    )
    artifact_id = stable_d2_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    receipt_values = {
        "artifact_id": artifact_id,
        "artifact_sha256": proposal.proposal_sha256,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "predecessor_d1_artifact_id": expected_d1_artifact.artifact_id,
        "predecessor_d1_artifact_sha256": expected_d1_artifact.artifact_sha256,
        "d1_table_map_artifact_id": expected_d1_table_map_artifact.artifact_id,
        "d1_table_map_artifact_sha256": (
            expected_d1_table_map_artifact.artifact_sha256
        ),
        "status": proposal.status,
        "logical_object_ids": tuple(
            item.logical_object_id for item in proposal.logical_objects
        ),
        "source_segment_ids": tuple(
            segment.segment_id
            for item in proposal.logical_objects
            for segment in item.source_segments
        ),
        "logical_table_scope_ids": tuple(scope_by_id),
        "source_evidence_disposition_ids": tuple(
            item.disposition_id for item in proposal.source_evidence_dispositions
        ),
        "unresolved_source_evidence_disposition_ids": (
            unresolved_source_disposition_ids
        ),
        "physical_table_ids": table_ids_in_order,
        "unresolved_table_ids": unresolved_table_ids,
        "proposal_sha256": proposal.proposal_sha256,
        "evidence_sha256": evidence_sha256,
        "validator_id": D2_LOGICAL_STRUCTURE_VALIDATOR_ID,
        "validator_version": D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION,
    }
    receipt_sha256 = _canonical_sha256(
        _jsonable(receipt_values)
        | {"schema_version": (D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_SCHEMA_VERSION)}
    )
    return D2LogicalStructureValidationReceiptV1(
        **receipt_values,
        receipt_sha256=receipt_sha256,
    )


def compile_validated_d1_artifact_identity(
    *,
    stage_artifact: AgentStageArtifactV1,
    proposal: DocumentUnderstandingProposalV2,
    validation_receipt: DocumentUnderstandingValidationReceiptV2,
    kit_identity: AgentKitIdentityV1,
) -> D2ValidatedD1ArtifactIdentityV1:
    """Compile D2's predecessor identity from the actual validated D1 triple."""

    proposal_sha256 = canonical_json_sha256(proposal.model_dump(mode="json"))
    if (
        stage_artifact.stage is not AgentExtractionStage.DOCUMENT_UNDERSTANDING
        or stage_artifact.run_id != proposal.run_id
        or stage_artifact.source_sha256 != proposal.source_sha256
        or stage_artifact.proposal_schema_id
        != DOCUMENT_UNDERSTANDING_PROPOSAL_SCHEMA_VERSION
        or stage_artifact.payload_sha256 != proposal_sha256
        or stage_artifact.payload_sha256 != validation_receipt.proposal_sha256
        or stage_artifact.evidence_sha256 != validation_receipt.evidence_sha256
        or stage_artifact.validator_receipt_sha256 != validation_receipt.receipt_sha256
        or validation_receipt.run_id != proposal.run_id
        or validation_receipt.source_sha256 != proposal.source_sha256
        or validation_receipt.page_count != proposal.page_count
        or validation_receipt.status is not proposal.status
        or validation_receipt.kit_bundle_sha256 != kit_identity.bundle_sha256
    ):
        raise ValueError("D2 cannot consume a stale D1 artifact/proposal/receipt")
    expected_artifact_status = (
        AgentStageArtifactStatus.VALIDATED
        if proposal.status is DocumentUnderstandingStatus.COMPLETE
        else AgentStageArtifactStatus.PARTIAL
    )
    if stage_artifact.status is not expected_artifact_status:
        raise ValueError("D1 stage status does not match its understanding proposal")
    return D2ValidatedD1ArtifactIdentityV1(
        artifact_id=stage_artifact.artifact_id,
        artifact_sha256=stage_artifact.payload_sha256,
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        status=stage_artifact.status,
        proposal_sha256=proposal_sha256,
        evidence_sha256=validation_receipt.evidence_sha256,
        validator_receipt_sha256=validation_receipt.receipt_sha256,
        page_count=proposal.page_count,
        kit_bundle_sha256=kit_identity.bundle_sha256,
    )


def stable_d2_source_segment_id(
    *,
    source_sha256: str,
    handle_kind: D2SourceHandleKind,
    source_handle: str,
    start_offset: int | None,
    end_offset: int | None,
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "handle_kind": handle_kind.value,
            "source_handle": source_handle,
            "start_offset": start_offset,
            "end_offset": end_offset,
        }
    )
    return f"d2-source-segment-{digest[:32]}"


def stable_d2_source_segment_sha256(
    *,
    segment_id: str,
    source_sha256: str,
    handle_kind: D2SourceHandleKind,
    source_handle: str,
    owner_source_node_id: str,
    source_order: int,
    page_numbers: tuple[int, ...],
    start_offset: int | None,
    end_offset: int | None,
    source_content_sha256: str,
) -> str:
    return _canonical_sha256(
        {
            "segment_id": segment_id,
            "source_sha256": source_sha256,
            "handle_kind": handle_kind.value,
            "source_handle": source_handle,
            "owner_source_node_id": owner_source_node_id,
            "source_order": source_order,
            "page_numbers": list(page_numbers),
            "start_offset": start_offset,
            "end_offset": end_offset,
            "source_content_sha256": source_content_sha256,
        }
    )


def stable_d2_source_disposition_id(
    *,
    source_sha256: str,
    source_node_id: str,
    start_offset: int | None,
    end_offset: int | None,
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "source_node_id": source_node_id,
            "start_offset": start_offset,
            "end_offset": end_offset,
        }
    )
    return f"d2-source-disposition-{digest[:32]}"


def stable_d2_source_disposition_sha256(
    *,
    disposition_id: str,
    source_sha256: str,
    source_node_id: str,
    source_order: int,
    start_offset: int | None,
    end_offset: int | None,
    disposition: D2SourceEvidenceDispositionKind,
    logical_object_id: str | None,
    source_segment_id: str | None,
    reason_codes: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "disposition_id": disposition_id,
            "source_sha256": source_sha256,
            "source_node_id": source_node_id,
            "source_order": source_order,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "disposition": disposition.value,
            "logical_object_id": logical_object_id,
            "source_segment_id": source_segment_id,
            "reason_codes": list(reason_codes),
            "evidence_refs": list(evidence_refs),
        }
    )


def stable_d2_logical_object_id(
    *,
    source_sha256: str,
    object_kind: D2LogicalObjectKind,
    source_segment_ids: tuple[str, ...],
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "object_kind": object_kind.value,
            "source_segment_ids": list(source_segment_ids),
        }
    )
    return f"d2-logical-object-{digest[:32]}"


def stable_d2_derived_container_id(
    *,
    source_sha256: str,
    object_kind: D2LogicalObjectKind,
    descendant_source_segment_ids: tuple[str, ...],
    descendant_logical_table_scope_ids: tuple[str, ...],
    parent_logical_object_id: str | None,
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "object_kind": object_kind.value,
            "descendant_source_segment_ids": list(descendant_source_segment_ids),
            "descendant_logical_table_scope_ids": list(
                descendant_logical_table_scope_ids
            ),
            "parent_logical_object_id": parent_logical_object_id,
            "identity_basis": "DERIVED_CONTAINER_CHILD_EVIDENCE",
        }
    )
    return f"d2-logical-object-{digest[:32]}"


def stable_d2_logical_object_sha256(
    *,
    logical_object_id: str,
    object_kind: D2LogicalObjectKind,
    parent_logical_object_id: str | None,
    source_order: int,
    page_numbers: tuple[int, ...],
    source_segments: tuple[D2SourceSegmentV1, ...],
    logical_table_scope_ids: tuple[str, ...],
    reason_codes: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "logical_object_id": logical_object_id,
            "object_kind": object_kind.value,
            "parent_logical_object_id": parent_logical_object_id,
            "source_order": source_order,
            "page_numbers": list(page_numbers),
            "source_segments": [
                item.model_dump(mode="json") for item in source_segments
            ],
            "logical_table_scope_ids": list(logical_table_scope_ids),
            "reason_codes": list(reason_codes),
        }
    )


def stable_d2_table_disposition_sha256(
    *,
    table_id: str,
    disposition: D2PhysicalTableDispositionKind,
    logical_table_scope_id: str | None,
    nested_owner_table_id: str | None,
    reason_codes: tuple[str, ...],
    evidence_refs: tuple[str, ...],
) -> str:
    return _canonical_sha256(
        {
            "table_id": table_id,
            "disposition": disposition.value,
            "logical_table_scope_id": logical_table_scope_id,
            "nested_owner_table_id": nested_owner_table_id,
            "reason_codes": list(reason_codes),
            "evidence_refs": list(evidence_refs),
        }
    )


def stable_d2_logical_structure_proposal_sha256(
    proposal: D2LogicalStructureProposalV1,
) -> str:
    return _canonical_sha256(
        proposal.model_dump(mode="json", exclude={"proposal_sha256"})
    )


def stable_d2_artifact_id(
    *,
    run_id: str,
    source_sha256: str,
    proposal_sha256: str,
) -> str:
    digest = _canonical_sha256(
        {
            "run_id": run_id,
            "source_sha256": source_sha256,
            "proposal_sha256": proposal_sha256,
        }
    )
    return f"agent-artifact-{digest[:32]}"


def _validate_source_segment(
    source_ir: SourceIRSnapshot,
    segment: D2SourceSegmentV1,
    *,
    page_count: int,
) -> None:
    nodes = {node.source_node_id: node for node in source_ir.nodes}
    owner = nodes.get(segment.owner_source_node_id)
    if owner is None:
        raise ValueError("D2 source segment owner does not exist")
    if segment.source_order != owner.source_order:
        raise ValueError("D2 source segment source order drifted")
    if any(page > page_count for page in segment.page_numbers):
        raise ValueError("D2 source segment page exceeds validated D1 scope")

    if segment.handle_kind is D2SourceHandleKind.SOURCE_NODE:
        if segment.source_handle != owner.source_node_id:
            raise ValueError("D2 node segment handle does not match its owner")
        source_text = owner.raw_text
        content_sha256 = _source_node_content_sha256(owner)
        native_pages = _native_page_numbers(owner)
    else:
        if owner.kind is not SourceNodeKind.TABLE or owner.table_payload is None:
            raise ValueError("D2 table-cell segment owner is not a physical table")
        cells = {cell.cell_id: cell for cell in owner.table_payload.cells}
        cell = cells.get(segment.source_handle)
        if cell is None:
            raise ValueError("D2 table-cell segment handle does not exist")
        source_text = cell.raw_text
        content_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        native_pages = _native_cell_page_numbers(cell)

    if segment.source_content_sha256 != content_sha256:
        raise ValueError("D2 source segment content binding is stale or tampered")
    if native_pages and segment.page_numbers != native_pages:
        raise ValueError("D2 source segment page evidence drifted")
    if source_text:
        if segment.start_offset is None or segment.end_offset is None:
            raise ValueError("text-bearing D2 source segments require an exact range")
        if segment.end_offset > len(source_text):
            raise ValueError("D2 source segment range exceeds its source text")
    elif segment.start_offset is not None or segment.end_offset is not None:
        raise ValueError("textless D2 source evidence cannot carry a text range")


def _validate_logical_object_identity(
    item: D2LogicalObjectV1,
    source_sha256: str,
    *,
    object_by_id: dict[str, D2LogicalObjectV1],
    scope_by_id: dict[str, AgentLogicalTableScopeV1],
) -> None:
    if item.object_kind is D2LogicalObjectKind.TABLE:
        return
    identity_segments = (
        item.source_segments
        if item.source_segments
        else _descendant_object_segments(item.logical_object_id, object_by_id)
    )
    if item.source_segments:
        expected = stable_d2_logical_object_id(
            source_sha256=source_sha256,
            object_kind=item.object_kind,
            source_segment_ids=tuple(
                segment.segment_id for segment in identity_segments
            ),
        )
    else:
        expected = stable_d2_derived_container_id(
            source_sha256=source_sha256,
            object_kind=item.object_kind,
            descendant_source_segment_ids=tuple(
                segment.segment_id for segment in identity_segments
            ),
            descendant_logical_table_scope_ids=_descendant_object_scope_ids(
                item.logical_object_id,
                object_by_id,
                scope_by_id=scope_by_id,
            ),
            parent_logical_object_id=item.parent_logical_object_id,
        )
    if item.logical_object_id != expected:
        raise ValueError("D2 logical-object ID does not bind its source evidence")


def _validate_logical_object_pages(
    item: D2LogicalObjectV1,
    scope_by_id: dict[str, AgentLogicalTableScopeV1],
    *,
    object_by_id: dict[str, D2LogicalObjectV1],
) -> None:
    evidence_pages = {
        page for segment in item.source_segments for page in segment.page_numbers
    }
    for scope_id in item.logical_table_scope_ids:
        scope = scope_by_id.get(scope_id)
        if scope is None:
            raise ValueError("D2 TABLE object references an unknown logical scope")
        evidence_pages.update(
            page for part in scope.physical_parts for page in part.page_numbers
        )
    if not evidence_pages:
        evidence_pages.update(
            _descendant_object_pages(item.logical_object_id, object_by_id)
        )
    if set(item.page_numbers) != evidence_pages:
        raise ValueError("D2 logical-object pages must equal its evidence pages")


def _descendant_object_segments(
    object_id: str,
    object_by_id: dict[str, D2LogicalObjectV1],
) -> tuple[D2SourceSegmentV1, ...]:
    children = tuple(
        item
        for item in object_by_id.values()
        if item.parent_logical_object_id == object_id
    )
    segments = tuple(
        segment
        for child in children
        for segment in (
            *child.source_segments,
            *_descendant_object_segments(child.logical_object_id, object_by_id),
        )
    )
    return tuple(
        sorted(
            segments,
            key=lambda item: (
                item.source_order,
                item.source_handle,
                -1 if item.start_offset is None else item.start_offset,
                item.segment_id,
            ),
        )
    )


def _descendant_object_pages(
    object_id: str,
    object_by_id: dict[str, D2LogicalObjectV1],
) -> tuple[int, ...]:
    children = tuple(
        item
        for item in object_by_id.values()
        if item.parent_logical_object_id == object_id
    )
    return tuple(
        sorted(
            {
                page
                for child in children
                for page in (
                    *child.page_numbers,
                    *_descendant_object_pages(child.logical_object_id, object_by_id),
                )
            }
        )
    )


def _descendant_object_scope_ids(
    object_id: str,
    object_by_id: dict[str, D2LogicalObjectV1],
    *,
    scope_by_id: dict[str, AgentLogicalTableScopeV1],
) -> tuple[str, ...]:
    children = tuple(
        item
        for item in object_by_id.values()
        if item.parent_logical_object_id == object_id
    )
    descendant_scope_ids = {
        scope_id
        for child in children
        for scope_id in (
            *child.logical_table_scope_ids,
            *_descendant_object_scope_ids(
                child.logical_object_id,
                object_by_id,
                scope_by_id=scope_by_id,
            ),
        )
    }
    unknown_scope_ids = descendant_scope_ids - set(scope_by_id)
    if unknown_scope_ids:
        raise ValueError("D2 logical-object descendant references an unknown scope")
    return tuple(
        sorted(
            descendant_scope_ids,
            key=lambda scope_id: (
                scope_by_id[scope_id].physical_parts[0].source_order,
                scope_id,
            ),
        )
    )


def _reject_logical_object_cycles(
    object_by_id: dict[str, D2LogicalObjectV1],
) -> None:
    for object_id in object_by_id:
        seen: set[str] = set()
        current: str | None = object_id
        while current is not None:
            if current in seen:
                raise ValueError("D2 logical-object parentage contains a cycle")
            seen.add(current)
            current = object_by_id[current].parent_logical_object_id


def _reject_overlapping_source_segments(
    logical_objects: tuple[D2LogicalObjectV1, ...],
) -> None:
    ranges: dict[tuple[D2SourceHandleKind, str], list[tuple[int, int]]] = {}
    whole_handles: set[tuple[D2SourceHandleKind, str]] = set()
    for logical_object in logical_objects:
        for segment in logical_object.source_segments:
            key = (segment.handle_kind, segment.source_handle)
            if segment.start_offset is None or segment.end_offset is None:
                if key in whole_handles or key in ranges:
                    raise ValueError("D2 source evidence cannot have multiple owners")
                whole_handles.add(key)
                continue
            if key in whole_handles:
                raise ValueError("D2 source evidence cannot have multiple owners")
            existing = ranges.setdefault(key, [])
            if any(
                segment.start_offset < end and start < segment.end_offset
                for start, end in existing
            ):
                raise ValueError("D2 source segment ranges cannot overlap")
            existing.append((segment.start_offset, segment.end_offset))


def _validate_source_evidence_dispositions(
    *,
    source_ir: SourceIRSnapshot,
    logical_objects: tuple[D2LogicalObjectV1, ...],
    dispositions: tuple[D2SourceEvidenceDispositionV1, ...],
) -> tuple[str, ...]:
    """Prove complete non-table source coverage without defining requirements."""

    nodes = {
        node.source_node_id: node
        for node in source_ir.nodes
        if node.kind is not SourceNodeKind.TABLE
    }
    grouped: dict[str, list[D2SourceEvidenceDispositionV1]] = {}
    object_segments = {
        (logical_object.logical_object_id, segment.segment_id): segment
        for logical_object in logical_objects
        for segment in logical_object.source_segments
    }
    expected_segment_ids = {
        segment.segment_id
        for logical_object in logical_objects
        for segment in logical_object.source_segments
        if segment.owner_source_node_id in nodes
        and segment.handle_kind is D2SourceHandleKind.SOURCE_NODE
    }
    bound_segment_ids: list[str] = []
    unresolved_ids: list[str] = []

    for item in dispositions:
        if item.source_sha256 != source_ir.source_sha256:
            raise ValueError("D2 source disposition belongs to another source")
        node = nodes.get(item.source_node_id)
        if node is None:
            raise ValueError("D2 source disposition owner is not a non-table node")
        if item.source_order != node.source_order:
            raise ValueError("D2 source disposition source order drifted")
        allowed_evidence_refs = {node.source_node_id}
        if node.asset_ref is not None:
            allowed_evidence_refs.add(node.asset_ref)
        if not allowed_evidence_refs.intersection(item.evidence_refs):
            raise ValueError("D2 source disposition lacks source-bound evidence")
        if node.raw_text:
            if item.start_offset is None or item.end_offset is None:
                raise ValueError("text source disposition requires an exact range")
            if item.end_offset > len(node.raw_text):
                raise ValueError("D2 source disposition exceeds its source text")
        elif item.start_offset is not None or item.end_offset is not None:
            raise ValueError("textless source disposition cannot carry a range")

        if item.logical_object_id is not None:
            assert item.source_segment_id is not None
            segment = object_segments.get(
                (item.logical_object_id, item.source_segment_id)
            )
            if (
                segment is None
                or segment.handle_kind is not D2SourceHandleKind.SOURCE_NODE
                or segment.owner_source_node_id != node.source_node_id
                or segment.source_handle != node.source_node_id
                or segment.start_offset != item.start_offset
                or segment.end_offset != item.end_offset
            ):
                raise ValueError(
                    "D2 object-bound disposition does not match its source segment"
                )
            bound_segment_ids.append(segment.segment_id)
        if item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED:
            unresolved_ids.append(item.disposition_id)
        grouped.setdefault(node.source_node_id, []).append(item)

    if set(grouped) != set(nodes):
        raise ValueError("D2 must disposition every non-table SourceIR node")
    if (
        len(bound_segment_ids) != len(set(bound_segment_ids))
        or set(bound_segment_ids) != expected_segment_ids
    ):
        raise ValueError(
            "every non-table D2 source segment requires one exact disposition"
        )

    for node_id, node in nodes.items():
        observed = grouped[node_id]
        if not node.raw_text:
            if len(observed) != 1:
                raise ValueError("textless source evidence requires one disposition")
            continue
        coverage = bytearray(len(node.raw_text))
        for item in observed:
            assert item.start_offset is not None and item.end_offset is not None
            for offset in range(item.start_offset, item.end_offset):
                if coverage[offset]:
                    raise ValueError("D2 source disposition ranges cannot overlap")
                coverage[offset] = 1
        if any(
            not character.isspace() and not coverage[offset]
            for offset, character in enumerate(node.raw_text)
        ):
            raise ValueError(
                "D2 source dispositions must cover every non-whitespace character"
            )
    return tuple(unresolved_ids)


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> dict[str, SourceNode]:
    catalog: dict[str, SourceNode] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        table_id = node.table_payload.table_id
        if table_id in catalog:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        catalog[table_id] = node
    return catalog


def _source_node_content_sha256(node: SourceNode) -> str:
    if node.raw_text:
        return hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
    if node.table_payload is not None:
        return canonical_model_sha256(node.table_payload)
    if node.asset_ref is not None:
        return canonical_model_sha256(node)
    raise ValueError("D2 source node has no bindable content")


def _native_page_numbers(node: SourceNode) -> tuple[int, ...]:
    page_number = node.native_locator.page_number
    return () if page_number is None else (page_number,)


def _native_cell_page_numbers(cell: TableCellIR) -> tuple[int, ...]:
    page_number = cell.native_locator.page_number
    return () if page_number is None else (page_number,)


def _jsonable(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return json.loads(
            json.dumps(
                value,
                default=lambda item: item.model_dump(mode="json"),
                ensure_ascii=False,
            )
        )
    raise TypeError("D2 canonical payload must be a mapping")


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
    "D2_LOGICAL_STRUCTURE_PROPOSAL_SCHEMA_VERSION",
    "D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_SCHEMA_VERSION",
    "D2_LOGICAL_STRUCTURE_VALIDATOR_ID",
    "D2_LOGICAL_STRUCTURE_VALIDATOR_VERSION",
    "D2LogicalObjectKind",
    "D2LogicalObjectV1",
    "D2LogicalStructureProposalV1",
    "D2LogicalStructureStatus",
    "D2LogicalStructureValidationReceiptV1",
    "D2PhysicalTableDispositionKind",
    "D2PhysicalTableDispositionV1",
    "D2SourceHandleKind",
    "D2SourceSegmentV1",
    "D2ValidatedD1ArtifactIdentityV1",
    "compile_validated_d1_artifact_identity",
    "stable_d2_artifact_id",
    "stable_d2_derived_container_id",
    "stable_d2_logical_object_id",
    "stable_d2_logical_object_sha256",
    "stable_d2_logical_structure_proposal_sha256",
    "stable_d2_source_segment_id",
    "stable_d2_source_segment_sha256",
    "stable_d2_table_disposition_sha256",
    "validate_d2_logical_structure_proposal",
]
