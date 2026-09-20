from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
)
from app.application.agent_workflow.d2_table_regions import (
    D2CompiledTableRegionV1,
    D2EmptyPhysicalTableDispositionKind,
    D2TableRegionArtifactStatus,
    D2TableRegionArtifactV1,
    D2TableRegionDispositionKind,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableIR,
)


D2_REGION_PLACEMENT_PLAN_SCHEMA_VERSION = "d2-region-placement-plan/v1"
D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION = "logical-structure-proposal/v2"
D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_V2_SCHEMA_VERSION = (
    "logical-structure-validation-receipt/v2"
)
D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID = "reqpilotx-logical-structure-validator"
D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION = "1.2.0"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_PLACEMENT_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
_V1_OBJECT_ID_PATTERN = r"^(?:d2-logical-object|agent-logical-table)-[0-9a-f]{32}$"
_V2_OBJECT_ID_PATTERN = (
    r"^(?:(?:d2-logical-object|agent-logical-table|d2-table-logical-scope|"
    r"d2-v2-region-object)-[0-9a-f]{32})$"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2BaseTableReplacementDisposition(StrEnum):
    REPLACED_BY_REGION_TOPOLOGY = "REPLACED_BY_REGION_TOPOLOGY"
    PRESERVED_AS_UNRESOLVED_LINEAGE = "PRESERVED_AS_UNRESOLVED_LINEAGE"


class D2RegionLogicalObjectPlacementMode(StrEnum):
    NEW_REGION_OBJECT = "NEW_REGION_OBJECT"
    MERGE_RETAINED_BASE = "MERGE_RETAINED_BASE"


class D2V2LogicalObjectOrigin(StrEnum):
    RETAINED_BASE = "RETAINED_BASE"
    RETAINED_BASE_WITH_REGION_CONTENT = "RETAINED_BASE_WITH_REGION_CONTENT"
    REGION_LOGICAL_SCOPE = "REGION_LOGICAL_SCOPE"
    REGION_LOGICAL_OBJECT = "REGION_LOGICAL_OBJECT"


class D2V2SourceSegmentKind(StrEnum):
    BASE_SOURCE_SEGMENT = "BASE_SOURCE_SEGMENT"
    TABLE_REGION = "TABLE_REGION"


class D2TableSupplementalEvidenceKindV1(StrEnum):
    TABLE_NODE_RAW_TEXT = "TABLE_NODE_RAW_TEXT"
    TABLE_NOTES = "TABLE_NOTES"
    TABLE_VISUAL = "TABLE_VISUAL"


class D2TableSupplementalDispositionKindV1(StrEnum):
    ABSENT = "ABSENT"
    CELL_DERIVED = "CELL_DERIVED"
    OWNED_BY_OBJECT = "OWNED_BY_OBJECT"
    OWNED_BY_SCOPE = "OWNED_BY_SCOPE"
    NESTED = "NESTED"
    NON_BUSINESS = "NON_BUSINESS"
    UNRESOLVED = "UNRESOLVED"


class D2SupplementalObjectOwnerReferenceKindV1(StrEnum):
    RETAINED_BASE_OBJECT = "RETAINED_BASE_OBJECT"
    REGION_LOGICAL_OBJECT_PLACEMENT = "REGION_LOGICAL_OBJECT_PLACEMENT"


class D2TableRawTextCellDerivationV1(StrEnum):
    ORDERED_CELL_RAW_TEXT_JOIN_LF_V1 = "ORDERED_CELL_RAW_TEXT_JOIN_LF/v1"


class D2RetainedBaseObjectPlacementV1(_StrictFrozenModel):
    base_logical_object_id: str = Field(pattern=_V1_OBJECT_ID_PATTERN)
    parent_logical_object_id: str | None = Field(
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )


class D2BaseTableReplacementCandidateV1(_StrictFrozenModel):
    base_logical_object_id: str = Field(pattern=_V1_OBJECT_ID_PATTERN)
    disposition: D2BaseTableReplacementDisposition
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )


class D2RegionLogicalScopePlacementV1(_StrictFrozenModel):
    logical_scope_id: str = Field(pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$")
    parent_logical_object_id: str | None = Field(
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )


class D2RegionLogicalObjectPlacementV1(_StrictFrozenModel):
    """Map one or more region logical objects to one V2 non-table object."""

    placement_key: str = Field(pattern=_PLACEMENT_KEY_PATTERN.pattern)
    source_region_logical_object_ids: tuple[str, ...] = Field(min_length=1)
    mode: D2RegionLogicalObjectPlacementMode
    target_base_logical_object_id: str | None = Field(
        default=None,
        pattern=_V1_OBJECT_ID_PATTERN,
    )
    object_kind: D2LogicalObjectKind | None = None
    parent_logical_object_id: str | None = Field(
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("source_region_logical_object_ids")
    @classmethod
    def validate_source_objects(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"d2-table-logical-object-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("region logical-object IDs must be unique and valid")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_mode(self) -> D2RegionLogicalObjectPlacementV1:
        if self.mode is D2RegionLogicalObjectPlacementMode.NEW_REGION_OBJECT:
            if self.target_base_logical_object_id is not None:
                raise ValueError("NEW_REGION_OBJECT cannot target a base object")
            if (
                self.object_kind is None
                or self.object_kind is D2LogicalObjectKind.TABLE
            ):
                raise ValueError("NEW_REGION_OBJECT requires a non-table object kind")
        else:
            if self.target_base_logical_object_id is None:
                raise ValueError("MERGE_RETAINED_BASE requires one base target")
            if self.object_kind is not None:
                raise ValueError("MERGE_RETAINED_BASE inherits its base object kind")
        return self


class D2TableSupplementalEvidencePlacementV1(_StrictFrozenModel):
    """Explicit semantic placement for non-cell table evidence.

    ABSENT is Host-derived and therefore intentionally unavailable here. Missing
    declarations for present evidence become Host-issued UNRESOLVED ledgers.
    """

    physical_table_id: str = Field(min_length=1, max_length=160)
    evidence_kind: D2TableSupplementalEvidenceKindV1
    evidence_ref: str = Field(min_length=1, max_length=240)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    disposition: Literal[
        D2TableSupplementalDispositionKindV1.CELL_DERIVED,
        D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
        D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
        D2TableSupplementalDispositionKindV1.NESTED,
        D2TableSupplementalDispositionKindV1.NON_BUSINESS,
        D2TableSupplementalDispositionKindV1.UNRESOLVED,
    ]
    owner_reference_kind: D2SupplementalObjectOwnerReferenceKindV1 | None = None
    owner_reference: str | None = Field(default=None, max_length=240)
    owner_region_logical_scope_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$",
    )
    nested_owner_region_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-region-[0-9a-f]{32}$",
    )
    cell_derivation: D2TableRawTextCellDerivationV1 | None = None
    derived_cell_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("physical_table_id", "evidence_ref", "owner_reference")
    @classmethod
    def reject_blank_values(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("supplemental evidence values must not be blank")
        return value

    @field_validator("derived_cell_ids")
    @classmethod
    def validate_derived_cells(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("derived cell IDs must be unique and non-empty")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_placement(self) -> D2TableSupplementalEvidencePlacementV1:
        object_owner = (
            self.owner_reference_kind is not None and self.owner_reference is not None
        )
        scope_owner = self.owner_region_logical_scope_id is not None
        nested_owner = self.nested_owner_region_id is not None
        cell_derived = self.cell_derivation is not None and bool(self.derived_cell_ids)
        expected = {
            D2TableSupplementalDispositionKindV1.CELL_DERIVED: (
                False,
                False,
                False,
                True,
            ),
            D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT: (
                True,
                False,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE: (
                False,
                True,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.NESTED: (
                False,
                False,
                True,
                False,
            ),
            D2TableSupplementalDispositionKindV1.NON_BUSINESS: (
                False,
                False,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.UNRESOLVED: (
                False,
                False,
                False,
                False,
            ),
        }[self.disposition]
        if (object_owner, scope_owner, nested_owner, cell_derived) != expected:
            raise ValueError(
                "supplemental evidence placement fields do not match disposition"
            )
        if (self.owner_reference_kind is None) != (self.owner_reference is None):
            raise ValueError(
                "supplemental object owner fields must be supplied together"
            )
        if (
            self.disposition is D2TableSupplementalDispositionKindV1.CELL_DERIVED
            and self.evidence_kind
            is not D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
        ):
            raise ValueError("CELL_DERIVED is valid only for table-node raw text")
        return self


class D2RegionPlacementPlanV1(_StrictFrozenModel):
    """Narrow runner-neutral output consumed by the V2 Host compiler."""

    schema_version: Literal["d2-region-placement-plan/v1"] = (
        D2_REGION_PLACEMENT_PLAN_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_proposal_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_region_artifact_id: str = Field(
        pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$"
    )
    table_region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    retained_base_objects: tuple[D2RetainedBaseObjectPlacementV1, ...] = ()
    base_table_replacements: tuple[D2BaseTableReplacementCandidateV1, ...] = ()
    region_logical_scope_placements: tuple[D2RegionLogicalScopePlacementV1, ...] = ()
    region_logical_object_placements: tuple[D2RegionLogicalObjectPlacementV1, ...] = ()
    supplemental_evidence_placements: tuple[
        D2TableSupplementalEvidencePlacementV1, ...
    ] = ()
    plan_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_plan(self) -> D2RegionPlacementPlanV1:
        _require_unique(
            (item.base_logical_object_id for item in self.retained_base_objects),
            "retained base object placements",
        )
        _require_unique(
            (item.base_logical_object_id for item in self.base_table_replacements),
            "base table replacement declarations",
        )
        _require_unique(
            (item.logical_scope_id for item in self.region_logical_scope_placements),
            "region logical-scope placements",
        )
        _require_unique(
            (item.placement_key for item in self.region_logical_object_placements),
            "region logical-object placement keys",
        )
        _require_unique(
            (
                item.target_base_logical_object_id
                for item in self.region_logical_object_placements
                if item.target_base_logical_object_id is not None
            ),
            "merged retained targets",
        )
        _require_unique(
            (
                (item.physical_table_id, item.evidence_kind.value)
                for item in self.supplemental_evidence_placements
            ),
            "supplemental evidence placements",
        )
        if self.plan_sha256 != stable_d2_region_placement_plan_sha256(self):
            raise ValueError("region placement plan SHA does not bind the plan")
        return self


class D2V2BaseSourceSegmentRefV1(_StrictFrozenModel):
    segment_kind: Literal[D2V2SourceSegmentKind.BASE_SOURCE_SEGMENT] = (
        D2V2SourceSegmentKind.BASE_SOURCE_SEGMENT
    )
    segment_id: str = Field(pattern=r"^d2-v2-source-segment-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = ()
    base_source_segment_id: str = Field(pattern=r"^d2-source-segment-[0-9a-f]{32}$")
    base_source_segment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    handle_kind: D2SourceHandleKind
    source_handle: str = Field(min_length=1, max_length=160)
    owner_source_node_id: str = Field(min_length=1, max_length=160)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)
    segment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _canonical_pages(value)

    @model_validator(mode="after")
    def validate_segment(self) -> D2V2BaseSourceSegmentRefV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("base source segment offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("base source segment range must be non-empty")
        expected_id = stable_d2_v2_source_segment_id(
            segment_kind=self.segment_kind,
            source_ir_id=self.source_ir_id,
            source_identity=self.base_source_segment_id,
            source_identity_sha256=self.base_source_segment_sha256,
        )
        if self.segment_id != expected_id:
            raise ValueError("V2 base segment ID does not bind its V1 segment")
        expected_sha = stable_d2_v2_source_segment_sha256(self)
        if self.segment_sha256 != expected_sha:
            raise ValueError("V2 base segment SHA does not bind its evidence")
        return self


class D2V2TableRegionSourceSegmentRefV1(_StrictFrozenModel):
    """Exact region reference; TableIR cell evidence remains in the region artifact."""

    segment_kind: Literal[D2V2SourceSegmentKind.TABLE_REGION] = (
        D2V2SourceSegmentKind.TABLE_REGION
    )
    segment_id: str = Field(pattern=r"^d2-v2-source-segment-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = ()
    table_region_artifact_id: str = Field(
        pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$"
    )
    table_region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    table_region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    segment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _canonical_pages(value)

    @model_validator(mode="after")
    def validate_segment(self) -> D2V2TableRegionSourceSegmentRefV1:
        expected_id = stable_d2_v2_source_segment_id(
            segment_kind=self.segment_kind,
            source_ir_id=self.source_ir_id,
            source_identity=self.table_region_id,
            source_identity_sha256=self.table_region_sha256,
        )
        if self.segment_id != expected_id:
            raise ValueError("V2 region segment ID does not bind its table region")
        expected_sha = stable_d2_v2_source_segment_sha256(self)
        if self.segment_sha256 != expected_sha:
            raise ValueError("V2 region segment SHA does not bind its evidence")
        return self


D2V2SourceSegmentV1 = Annotated[
    D2V2BaseSourceSegmentRefV1 | D2V2TableRegionSourceSegmentRefV1,
    Field(discriminator="segment_kind"),
]


class D2LogicalObjectV2(_StrictFrozenModel):
    logical_object_id: str = Field(pattern=_V2_OBJECT_ID_PATTERN)
    origin: D2V2LogicalObjectOrigin
    object_kind: D2LogicalObjectKind
    parent_logical_object_id: str | None = Field(
        default=None,
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = ()
    source_segments: tuple[D2V2SourceSegmentV1, ...] = ()
    nested_evidence_region_ids: tuple[str, ...] = ()
    supplemental_evidence_ids: tuple[str, ...] = ()
    base_logical_object_id: str | None = Field(
        default=None,
        pattern=_V1_OBJECT_ID_PATTERN,
    )
    base_logical_object_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN.pattern,
    )
    region_logical_scope_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$",
    )
    region_logical_scope_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN.pattern,
    )
    source_region_logical_object_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        return _canonical_pages(value)

    @field_validator("nested_evidence_region_ids")
    @classmethod
    def validate_nested_regions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"d2-table-region-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("nested evidence region IDs must be unique and valid")
        return value

    @field_validator("supplemental_evidence_ids")
    @classmethod
    def validate_supplemental_evidence_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"d2-v2-table-supplement-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("supplemental evidence IDs must be unique and valid")
        return value

    @field_validator("source_region_logical_object_ids")
    @classmethod
    def validate_region_object_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"d2-table-logical-object-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("source region logical-object IDs must be unique")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_object(self) -> D2LogicalObjectV2:
        if self.parent_logical_object_id == self.logical_object_id:
            raise ValueError("a V2 logical object cannot own itself")
        segment_keys = tuple(_segment_sort_key(item) for item in self.source_segments)
        if segment_keys != tuple(sorted(segment_keys)):
            raise ValueError("V2 source segments must follow canonical source order")
        if len({item.segment_id for item in self.source_segments}) != len(
            self.source_segments
        ):
            raise ValueError("a V2 logical object cannot repeat a source segment")

        base_present = (
            self.base_logical_object_id is not None
            and self.base_logical_object_sha256 is not None
        )
        scope_present = (
            self.region_logical_scope_id is not None
            and self.region_logical_scope_sha256 is not None
        )
        kinds = tuple(item.segment_kind for item in self.source_segments)
        if self.origin is D2V2LogicalObjectOrigin.RETAINED_BASE:
            if (
                not base_present
                or scope_present
                or self.source_region_logical_object_ids
            ):
                raise ValueError("RETAINED_BASE requires only base lineage")
            if any(
                kind is not D2V2SourceSegmentKind.BASE_SOURCE_SEGMENT for kind in kinds
            ):
                raise ValueError("RETAINED_BASE may reference only base segments")
        elif self.origin is D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT:
            if (
                not base_present
                or scope_present
                or not self.source_region_logical_object_ids
            ):
                raise ValueError(
                    "merged retained objects require base and region lineage"
                )
            if D2V2SourceSegmentKind.TABLE_REGION not in kinds:
                raise ValueError("merged retained objects require region segments")
        elif self.origin is D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE:
            if (
                base_present
                or not scope_present
                or self.source_region_logical_object_ids
            ):
                raise ValueError("region scope objects require only scope lineage")
            if (
                self.logical_object_id != self.region_logical_scope_id
                or self.object_kind is not D2LogicalObjectKind.TABLE
                or not self.source_segments
                or any(kind is not D2V2SourceSegmentKind.TABLE_REGION for kind in kinds)
            ):
                raise ValueError(
                    "region scope objects require exact TABLE region segments"
                )
        else:
            if (
                base_present
                or scope_present
                or not self.source_region_logical_object_ids
            ):
                raise ValueError(
                    "region logical objects require only region-object lineage"
                )
            if (
                self.object_kind is D2LogicalObjectKind.TABLE
                or not self.source_segments
                or any(kind is not D2V2SourceSegmentKind.TABLE_REGION for kind in kinds)
            ):
                raise ValueError(
                    "region logical objects require non-table region segments"
                )

        if (self.base_logical_object_id is None) != (
            self.base_logical_object_sha256 is None
        ) or (self.region_logical_scope_id is None) != (
            self.region_logical_scope_sha256 is None
        ):
            raise ValueError(
                "V2 logical-object lineage fields must be supplied together"
            )
        if self.object_sha256 != stable_d2_v2_logical_object_sha256(self):
            raise ValueError("V2 logical-object SHA does not bind its evidence")
        return self


class D2V2TableRegionDispositionV1(_StrictFrozenModel):
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    physical_table_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    disposition: D2TableRegionDispositionKind
    logical_object_id: str | None = Field(
        default=None,
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    source_segment_id: str | None = Field(
        default=None,
        pattern=r"^d2-v2-source-segment-[0-9a-f]{32}$",
    )
    nested_owner_region_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-region-[0-9a-f]{32}$",
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    disposition_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_disposition(self) -> D2V2TableRegionDispositionV1:
        object_bound = self.disposition in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }
        nested = self.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
        if (
            (self.logical_object_id is not None) != object_bound
            or (self.source_segment_id is not None) != object_bound
            or (self.nested_owner_region_id is not None) != nested
        ):
            raise ValueError("V2 region disposition fields do not match its kind")
        if self.disposition_sha256 != stable_d2_v2_region_disposition_sha256(self):
            raise ValueError("V2 region disposition SHA does not bind its decision")
        return self


class D2V2BaseTableReplacementV1(_StrictFrozenModel):
    base_logical_object_id: str = Field(pattern=_V1_OBJECT_ID_PATTERN)
    base_logical_object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_logical_table_scope_id: str = Field(
        pattern=r"^agent-logical-table-[0-9a-f]{32}$"
    )
    base_logical_table_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    physical_table_ids: tuple[str, ...] = Field(min_length=1)
    disposition: D2BaseTableReplacementDisposition
    region_ids: tuple[str, ...] = ()
    replacement_logical_object_ids: tuple[str, ...] = ()
    nested_region_ids: tuple[str, ...] = ()
    false_region_ids: tuple[str, ...] = ()
    unresolved_region_ids: tuple[str, ...] = ()
    empty_physical_table_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    replacement_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator(
        "physical_table_ids",
        "region_ids",
        "replacement_logical_object_ids",
        "nested_region_ids",
        "false_region_ids",
        "unresolved_region_ids",
        "empty_physical_table_ids",
    )
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("replacement ledgers require unique non-empty values")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_replacement(self) -> D2V2BaseTableReplacementV1:
        unresolved = bool(self.unresolved_region_ids or self.empty_physical_table_ids)
        expected = (
            D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
            if unresolved
            else D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
        )
        if self.disposition is not expected:
            raise ValueError("base table replacement disposition is not evidence-safe")
        if self.replacement_sha256 != stable_d2_v2_base_table_replacement_sha256(self):
            raise ValueError("base table replacement SHA does not bind its topology")
        return self


class D2V2TableSupplementalEvidenceDispositionV1(_StrictFrozenModel):
    evidence_id: str = Field(pattern=r"^d2-v2-table-supplement-[0-9a-f]{32}$")
    physical_table_id: str = Field(min_length=1, max_length=160)
    evidence_kind: D2TableSupplementalEvidenceKindV1
    evidence_ref: str = Field(min_length=1, max_length=240)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    disposition: D2TableSupplementalDispositionKindV1
    owner_logical_object_id: str | None = Field(
        default=None,
        pattern=_V2_OBJECT_ID_PATTERN,
    )
    nested_owner_region_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-region-[0-9a-f]{32}$",
    )
    cell_derivation: D2TableRawTextCellDerivationV1 | None = None
    derived_cell_ids: tuple[str, ...] = ()
    host_auto_dispositioned: bool = False
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    disposition_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("derived_cell_ids")
    @classmethod
    def validate_derived_cells(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("derived cell IDs must be unique and non-empty")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _canonical_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_disposition(
        self,
    ) -> D2V2TableSupplementalEvidenceDispositionV1:
        owned = self.disposition in {
            D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
            D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
            D2TableSupplementalDispositionKindV1.NESTED,
        }
        nested = self.disposition is D2TableSupplementalDispositionKindV1.NESTED
        derived = self.disposition is D2TableSupplementalDispositionKindV1.CELL_DERIVED
        if (self.owner_logical_object_id is not None) != owned:
            raise ValueError("owned supplemental evidence requires one logical owner")
        if (self.nested_owner_region_id is not None) != nested:
            raise ValueError("nested supplemental evidence requires one region owner")
        if (self.cell_derivation is not None) != derived or bool(
            self.derived_cell_ids
        ) != derived:
            raise ValueError("CELL_DERIVED requires an exact derivation ledger")
        if self.host_auto_dispositioned and self.disposition not in {
            D2TableSupplementalDispositionKindV1.ABSENT,
            D2TableSupplementalDispositionKindV1.UNRESOLVED,
        }:
            raise ValueError("Host may auto-disposition only ABSENT or UNRESOLVED")
        expected_id = stable_d2_v2_table_supplemental_evidence_id(
            physical_table_id=self.physical_table_id,
            evidence_kind=self.evidence_kind,
            evidence_ref=self.evidence_ref,
            evidence_sha256=self.evidence_sha256,
        )
        if self.evidence_id != expected_id:
            raise ValueError("supplemental evidence ID does not bind its source")
        if self.disposition_sha256 != (
            stable_d2_v2_table_supplemental_disposition_sha256(self)
        ):
            raise ValueError("supplemental disposition SHA does not bind its decision")
        return self


class D2V2PhysicalTableEvidenceLedgerV1(_StrictFrozenModel):
    """Bind cell topology plus raw-text, notes, and visual residual evidence."""

    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    table_source_node_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    raw_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    notes_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    visual_residual_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    physical_region_coverage_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_region_ids: tuple[str, ...] = ()
    empty_table_disposition: D2EmptyPhysicalTableDispositionKind | None = None
    supplemental_dispositions: tuple[
        D2V2TableSupplementalEvidenceDispositionV1, ...
    ] = Field(min_length=3, max_length=3)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("ordered_region_ids")
    @classmethod
    def validate_regions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("physical table region ledger cannot repeat regions")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> D2V2PhysicalTableEvidenceLedgerV1:
        if self.empty_table_disposition is not None and self.ordered_region_ids:
            raise ValueError("empty tables cannot claim cell-backed regions")
        expected_kinds = tuple(D2TableSupplementalEvidenceKindV1)
        if tuple(item.evidence_kind for item in self.supplemental_dispositions) != (
            expected_kinds
        ):
            raise ValueError(
                "physical table supplemental evidence must have one canonical ledger per kind"
            )
        if any(
            item.physical_table_id != self.physical_table_id
            for item in self.supplemental_dispositions
        ):
            raise ValueError("supplemental evidence belongs to another physical table")
        if self.evidence_sha256 != stable_d2_v2_physical_table_evidence_sha256(self):
            raise ValueError("physical table evidence SHA does not bind residuals")
        return self


class D2ValidatedBaseLogicalStructureIdentityV1(_StrictFrozenModel):
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    status: D2LogicalStructureStatus
    logical_object_ids: tuple[str, ...]
    logical_table_scope_ids: tuple[str, ...]
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)


class D2TableRegionArtifactIdentityV1(_StrictFrozenModel):
    artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    status: D2TableRegionArtifactStatus
    region_ids: tuple[str, ...]
    logical_scope_ids: tuple[str, ...]
    logical_object_ids: tuple[str, ...]
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)


class D2LogicalStructureProposalV2(_StrictFrozenModel):
    schema_version: Literal["logical-structure-proposal/v2"] = (
        D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_identity: AgentKitIdentityV1
    base_logical_structure: D2ValidatedBaseLogicalStructureIdentityV1
    table_region_artifact: D2TableRegionArtifactIdentityV1
    region_placement_plan_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    status: D2LogicalStructureStatus
    logical_objects: tuple[D2LogicalObjectV2, ...] = ()
    retained_base_logical_object_ids: tuple[str, ...] = ()
    base_table_replacements: tuple[D2V2BaseTableReplacementV1, ...] = ()
    table_region_dispositions: tuple[D2V2TableRegionDispositionV1, ...] = ()
    physical_table_evidence: tuple[D2V2PhysicalTableEvidenceLedgerV1, ...] = ()
    unresolved_region_ids: tuple[str, ...] = ()
    unresolved_empty_table_ids: tuple[str, ...] = ()
    unresolved_supplemental_evidence_ids: tuple[str, ...] = ()
    nested_region_ids: tuple[str, ...] = ()
    false_region_ids: tuple[str, ...] = ()
    unresolved_codes: tuple[str, ...] = ()
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("unresolved_codes")
    @classmethod
    def validate_unresolved_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _canonical_reason_codes(value)

    @model_validator(mode="after")
    def validate_proposal(self) -> D2LogicalStructureProposalV2:
        object_order = tuple(
            (item.source_order, item.logical_object_id) for item in self.logical_objects
        )
        if object_order != tuple(sorted(object_order)):
            raise ValueError("V2 logical objects must follow canonical source order")
        _require_unique(
            (item.logical_object_id for item in self.logical_objects),
            "V2 logical-object IDs",
        )
        segment_ids = tuple(
            segment.segment_id
            for logical_object in self.logical_objects
            for segment in logical_object.source_segments
        )
        _require_unique(segment_ids, "V2 source segment ownership")
        _require_unique(
            (item.region_id for item in self.table_region_dispositions),
            "V2 region dispositions",
        )
        _require_unique(
            (item.physical_table_id for item in self.physical_table_evidence),
            "V2 physical table evidence",
        )
        _require_unique(
            (item.base_logical_object_id for item in self.base_table_replacements),
            "V2 base table replacements",
        )
        object_ids = {item.logical_object_id for item in self.logical_objects}
        for item in self.logical_objects:
            if (
                item.parent_logical_object_id is not None
                and item.parent_logical_object_id not in object_ids
            ):
                raise ValueError("V2 logical-object parent does not exist")
        _reject_parent_cycles(self.logical_objects)

        region_segment_by_id = {
            segment.table_region_id: segment
            for item in self.logical_objects
            for segment in item.source_segments
            if isinstance(segment, D2V2TableRegionSourceSegmentRefV1)
        }
        for disposition in self.table_region_dispositions:
            if disposition.source_segment_id is None:
                continue
            segment = region_segment_by_id.get(disposition.region_id)
            if (
                segment is None
                or segment.segment_id != disposition.source_segment_id
                or segment.table_region_sha256 != disposition.region_sha256
            ):
                raise ValueError(
                    "object-bound region dispositions require one exact source segment"
                )
        nested_evidence = {
            region_id
            for item in self.logical_objects
            for region_id in item.nested_evidence_region_ids
        }
        if nested_evidence != set(self.nested_region_ids):
            raise ValueError("every nested region requires one owner evidence ledger")
        supplemental_dispositions = tuple(
            disposition
            for table in self.physical_table_evidence
            for disposition in table.supplemental_dispositions
        )
        owned_supplemental_ids = {
            item.evidence_id
            for item in supplemental_dispositions
            if item.owner_logical_object_id is not None
        }
        object_supplemental_ids = {
            evidence_id
            for item in self.logical_objects
            for evidence_id in item.supplemental_evidence_ids
        }
        if owned_supplemental_ids != object_supplemental_ids:
            raise ValueError(
                "owned supplemental evidence requires one logical-object ledger"
            )
        listed_supplemental_ids = tuple(
            evidence_id
            for item in self.logical_objects
            for evidence_id in item.supplemental_evidence_ids
        )
        if len(listed_supplemental_ids) != len(set(listed_supplemental_ids)):
            raise ValueError("supplemental evidence cannot have multiple owners")
        expected_supplemental_by_owner: dict[str, set[str]] = {}
        for disposition in supplemental_dispositions:
            if disposition.owner_logical_object_id is not None:
                expected_supplemental_by_owner.setdefault(
                    disposition.owner_logical_object_id,
                    set(),
                ).add(disposition.evidence_id)
        for item in self.logical_objects:
            if set(item.supplemental_evidence_ids) != (
                expected_supplemental_by_owner.get(item.logical_object_id, set())
            ):
                raise ValueError(
                    "logical-object supplemental ledger differs from disposition owner"
                )
        unresolved_supplemental_ids = tuple(
            item.evidence_id
            for item in supplemental_dispositions
            if item.disposition is D2TableSupplementalDispositionKindV1.UNRESOLVED
        )
        if self.unresolved_supplemental_evidence_ids != (unresolved_supplemental_ids):
            raise ValueError("unresolved supplemental evidence ledger must be exact")
        if self.proposal_sha256 != stable_d2_logical_structure_proposal_v2_sha256(self):
            raise ValueError("V2 proposal SHA does not bind the complete proposal")
        return self


class D2LogicalStructureValidationReceiptV2(_StrictFrozenModel):
    schema_version: Literal["logical-structure-validation-receipt/v2"] = (
        D2_LOGICAL_STRUCTURE_VALIDATION_RECEIPT_V2_SCHEMA_VERSION
    )
    artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_identity: AgentKitIdentityV1
    base_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    base_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_region_artifact_id: str = Field(
        pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$"
    )
    table_region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_placement_plan_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    status: D2LogicalStructureStatus
    logical_object_ids: tuple[str, ...]
    source_segment_ids: tuple[str, ...]
    retained_base_logical_object_ids: tuple[str, ...]
    replaced_base_table_object_ids: tuple[str, ...]
    unresolved_base_table_object_ids: tuple[str, ...]
    table_region_ids: tuple[str, ...]
    nested_region_ids: tuple[str, ...]
    false_region_ids: tuple[str, ...]
    unresolved_region_ids: tuple[str, ...]
    unresolved_empty_table_ids: tuple[str, ...]
    unresolved_supplemental_evidence_ids: tuple[str, ...]
    physical_table_evidence_sha256s: tuple[str, ...]
    proposal_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    validator_id: Literal["reqpilotx-logical-structure-validator"] = (
        D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID
    )
    validator_version: Literal["1.2.0"] = D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_receipt(self) -> D2LogicalStructureValidationReceiptV2:
        if self.artifact_sha256 != self.proposal_sha256:
            raise ValueError("V2 artifact SHA must equal its proposal SHA")
        expected_id = stable_d2_v2_artifact_id(
            run_id=self.run_id,
            source_sha256=self.source_sha256,
            proposal_sha256=self.proposal_sha256,
        )
        if self.artifact_id != expected_id:
            raise ValueError("V2 artifact ID does not bind its proposal")
        if self.receipt_sha256 != stable_d2_v2_validation_receipt_sha256(self):
            raise ValueError("V2 validation receipt SHA does not bind the receipt")
        return self


class D2LogicalStructureCompilationV2(_StrictFrozenModel):
    proposal: D2LogicalStructureProposalV2
    validation_receipt: D2LogicalStructureValidationReceiptV2


@dataclass(frozen=True)
class _TableSupplementalFact:
    physical_table_id: str
    evidence_kind: D2TableSupplementalEvidenceKindV1
    evidence_ref: str
    evidence_sha256: str
    present: bool


def build_d2_table_supplemental_object_owner_placements(
    source_ir: SourceIRSnapshot,
    table_region_artifact: D2TableRegionArtifactV1,
    *,
    owner_reference_kind: D2SupplementalObjectOwnerReferenceKindV1,
    owner_reference: str,
    reason_codes: tuple[str, ...],
) -> tuple[D2TableSupplementalEvidencePlacementV1, ...]:
    """Build explicit owner placements for every present table residual."""

    reason_codes = _canonical_reason_codes(reason_codes)
    facts = _table_supplemental_catalog(source_ir, table_region_artifact)
    return tuple(
        D2TableSupplementalEvidencePlacementV1(
            physical_table_id=fact.physical_table_id,
            evidence_kind=fact.evidence_kind,
            evidence_ref=fact.evidence_ref,
            evidence_sha256=fact.evidence_sha256,
            disposition=D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
            owner_reference_kind=owner_reference_kind,
            owner_reference=owner_reference,
            reason_codes=reason_codes,
        )
        for fact in facts.values()
        if fact.present
    )


def build_d2_region_placement_plan(
    *,
    base_proposal: D2LogicalStructureProposalV1,
    base_validation_receipt: D2LogicalStructureValidationReceiptV1,
    table_region_artifact: D2TableRegionArtifactV1,
    retained_base_objects: tuple[D2RetainedBaseObjectPlacementV1, ...],
    base_table_replacements: tuple[D2BaseTableReplacementCandidateV1, ...],
    region_logical_scope_placements: tuple[D2RegionLogicalScopePlacementV1, ...] = (),
    region_logical_object_placements: tuple[D2RegionLogicalObjectPlacementV1, ...] = (),
    supplemental_evidence_placements: tuple[
        D2TableSupplementalEvidencePlacementV1, ...
    ] = (),
) -> D2RegionPlacementPlanV1:
    values = {
        "run_id": base_proposal.run_id,
        "source_sha256": base_proposal.source_sha256,
        "source_ir_id": base_proposal.source_ir_id,
        "source_ir_canonical_sha256": base_proposal.source_ir_canonical_sha256,
        "kit_bundle_sha256": base_proposal.kit_identity.bundle_sha256,
        "base_proposal_sha256": base_proposal.proposal_sha256,
        "base_validation_receipt_sha256": base_validation_receipt.receipt_sha256,
        "table_region_artifact_id": table_region_artifact.artifact_id,
        "table_region_artifact_sha256": table_region_artifact.artifact_sha256,
        "retained_base_objects": tuple(
            sorted(
                retained_base_objects,
                key=lambda item: item.base_logical_object_id,
            )
        ),
        "base_table_replacements": tuple(
            sorted(
                base_table_replacements,
                key=lambda item: item.base_logical_object_id,
            )
        ),
        "region_logical_scope_placements": tuple(
            sorted(
                region_logical_scope_placements,
                key=lambda item: item.logical_scope_id,
            )
        ),
        "region_logical_object_placements": tuple(
            sorted(
                region_logical_object_placements,
                key=lambda item: item.placement_key,
            )
        ),
        "supplemental_evidence_placements": tuple(
            sorted(
                supplemental_evidence_placements,
                key=lambda item: (
                    item.physical_table_id,
                    item.evidence_kind.value,
                ),
            )
        ),
    }
    draft = D2RegionPlacementPlanV1.model_construct(
        **values,
        plan_sha256="0" * 64,
    )
    return D2RegionPlacementPlanV1(
        **values,
        plan_sha256=stable_d2_region_placement_plan_sha256(draft),
    )


def compile_d2_logical_structure_v2(
    source_ir: SourceIRSnapshot,
    *,
    base_proposal: D2LogicalStructureProposalV1,
    base_validation_receipt: D2LogicalStructureValidationReceiptV1,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureCompilationV2:
    """Compile a region-capable successor without mutating the validated V1."""

    _validate_compiler_inputs(
        source_ir,
        base_proposal=base_proposal,
        base_validation_receipt=base_validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    proposal = _build_proposal_v2(
        source_ir,
        base_proposal=base_proposal,
        base_validation_receipt=base_validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    receipt = validate_d2_logical_structure_proposal_v2(
        source_ir,
        proposal,
        base_proposal=base_proposal,
        base_validation_receipt=base_validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    return D2LogicalStructureCompilationV2(
        proposal=proposal,
        validation_receipt=receipt,
    )


def validate_d2_logical_structure_proposal_v2(
    source_ir: SourceIRSnapshot,
    proposal: D2LogicalStructureProposalV2,
    *,
    base_proposal: D2LogicalStructureProposalV1,
    base_validation_receipt: D2LogicalStructureValidationReceiptV1,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureValidationReceiptV2:
    _validate_compiler_inputs(
        source_ir,
        base_proposal=base_proposal,
        base_validation_receipt=base_validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    expected = _build_proposal_v2(
        source_ir,
        base_proposal=base_proposal,
        base_validation_receipt=base_validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    if proposal != expected:
        raise ValueError("V2 proposal differs from the Host-compiled successor")

    artifact_id = stable_d2_v2_artifact_id(
        run_id=proposal.run_id,
        source_sha256=proposal.source_sha256,
        proposal_sha256=proposal.proposal_sha256,
    )
    replaced = tuple(
        item.base_logical_object_id
        for item in proposal.base_table_replacements
        if item.disposition
        is D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
    )
    unresolved_base = tuple(
        item.base_logical_object_id
        for item in proposal.base_table_replacements
        if item.disposition
        is D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
    )
    evidence_sha256 = _canonical_sha256(
        {
            "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
            "base_validation_receipt_sha256": (base_validation_receipt.receipt_sha256),
            "base_evidence_sha256": base_validation_receipt.evidence_sha256,
            "table_region_artifact_sha256": table_region_artifact.artifact_sha256,
            "region_placement_plan_sha256": placement_plan.plan_sha256,
            "logical_object_sha256s": [
                item.object_sha256 for item in proposal.logical_objects
            ],
            "region_disposition_sha256s": [
                item.disposition_sha256 for item in proposal.table_region_dispositions
            ],
            "base_table_replacement_sha256s": [
                item.replacement_sha256 for item in proposal.base_table_replacements
            ],
            "physical_table_evidence_sha256s": [
                item.evidence_sha256 for item in proposal.physical_table_evidence
            ],
        }
    )
    values = {
        "artifact_id": artifact_id,
        "artifact_sha256": proposal.proposal_sha256,
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "base_artifact_id": base_validation_receipt.artifact_id,
        "base_artifact_sha256": base_validation_receipt.artifact_sha256,
        "base_validation_receipt_sha256": base_validation_receipt.receipt_sha256,
        "table_region_artifact_id": table_region_artifact.artifact_id,
        "table_region_artifact_sha256": table_region_artifact.artifact_sha256,
        "region_placement_plan_sha256": placement_plan.plan_sha256,
        "status": proposal.status,
        "logical_object_ids": tuple(
            item.logical_object_id for item in proposal.logical_objects
        ),
        "source_segment_ids": tuple(
            segment.segment_id
            for item in proposal.logical_objects
            for segment in item.source_segments
        ),
        "retained_base_logical_object_ids": (proposal.retained_base_logical_object_ids),
        "replaced_base_table_object_ids": replaced,
        "unresolved_base_table_object_ids": unresolved_base,
        "table_region_ids": tuple(
            item.region_id for item in proposal.table_region_dispositions
        ),
        "nested_region_ids": proposal.nested_region_ids,
        "false_region_ids": proposal.false_region_ids,
        "unresolved_region_ids": proposal.unresolved_region_ids,
        "unresolved_empty_table_ids": proposal.unresolved_empty_table_ids,
        "unresolved_supplemental_evidence_ids": (
            proposal.unresolved_supplemental_evidence_ids
        ),
        "physical_table_evidence_sha256s": tuple(
            item.evidence_sha256 for item in proposal.physical_table_evidence
        ),
        "proposal_sha256": proposal.proposal_sha256,
        "evidence_sha256": evidence_sha256,
        "validator_id": D2_LOGICAL_STRUCTURE_V2_VALIDATOR_ID,
        "validator_version": D2_LOGICAL_STRUCTURE_V2_VALIDATOR_VERSION,
    }
    draft = D2LogicalStructureValidationReceiptV2.model_construct(
        **values,
        receipt_sha256="0" * 64,
    )
    return D2LogicalStructureValidationReceiptV2(
        **values,
        receipt_sha256=stable_d2_v2_validation_receipt_sha256(draft),
    )


def _validate_compiler_inputs(
    source_ir: SourceIRSnapshot,
    *,
    base_proposal: D2LogicalStructureProposalV1,
    base_validation_receipt: D2LogicalStructureValidationReceiptV1,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> None:
    if source_ir.status is SourceIRStatus.FAILED:
        raise ValueError("V2 cannot consume a failed SourceIR snapshot")
    current_source_sha = source_ir_canonical_sha256(source_ir)
    if (
        base_proposal.source_ir_id != source_ir.source_ir_id
        or base_proposal.source_sha256 != source_ir.source_sha256
        or base_proposal.source_ir_canonical_sha256 != current_source_sha
    ):
        raise ValueError("validated base proposal belongs to another SourceIR")
    if base_proposal.kit_identity != expected_kit_identity:
        raise ValueError("validated base proposal Agent Kit identity drifted")
    if (
        base_validation_receipt.run_id != base_proposal.run_id
        or base_validation_receipt.source_sha256 != base_proposal.source_sha256
        or base_validation_receipt.source_ir_id != base_proposal.source_ir_id
        or base_validation_receipt.source_ir_canonical_sha256
        != base_proposal.source_ir_canonical_sha256
        or base_validation_receipt.kit_identity != expected_kit_identity
        or base_validation_receipt.proposal_sha256 != base_proposal.proposal_sha256
        or base_validation_receipt.artifact_sha256 != base_proposal.proposal_sha256
        or base_validation_receipt.status is not base_proposal.status
        or base_validation_receipt.logical_object_ids
        != tuple(item.logical_object_id for item in base_proposal.logical_objects)
        or base_validation_receipt.logical_table_scope_ids
        != tuple(item.logical_object_id for item in base_proposal.logical_table_scopes)
    ):
        raise ValueError("validated base proposal/receipt lineage drifted")
    if (
        table_region_artifact.source_ir_id != source_ir.source_ir_id
        or table_region_artifact.source_sha256 != source_ir.source_sha256
        or table_region_artifact.source_ir_canonical_sha256 != current_source_sha
    ):
        raise ValueError("table-region artifact belongs to another SourceIR")
    if table_region_artifact.kit_bundle_sha256 != expected_kit_identity.bundle_sha256:
        raise ValueError("table-region artifact Agent Kit identity drifted")
    if (
        table_region_artifact.d1_table_map_artifact_id
        != base_proposal.d1_table_map_artifact.artifact_id
        or table_region_artifact.d1_table_map_artifact_sha256
        != base_proposal.d1_table_map_artifact.artifact_sha256
    ):
        raise ValueError("table-region artifact D1 table-map lineage drifted")
    if (
        placement_plan.run_id != base_proposal.run_id
        or placement_plan.source_sha256 != source_ir.source_sha256
        or placement_plan.source_ir_id != source_ir.source_ir_id
        or placement_plan.source_ir_canonical_sha256 != current_source_sha
        or placement_plan.kit_bundle_sha256 != expected_kit_identity.bundle_sha256
        or placement_plan.base_proposal_sha256 != base_proposal.proposal_sha256
        or placement_plan.base_validation_receipt_sha256
        != base_validation_receipt.receipt_sha256
        or placement_plan.table_region_artifact_id != table_region_artifact.artifact_id
        or placement_plan.table_region_artifact_sha256
        != table_region_artifact.artifact_sha256
    ):
        raise ValueError("region placement plan lineage drifted")

    source_tables = _source_table_catalog(source_ir)
    coverage_ids = {
        item.physical_table_id
        for item in table_region_artifact.physical_table_cell_coverage
    }
    if coverage_ids != set(source_tables):
        raise ValueError("table-region artifact must cover every physical TableIR")
    coverage_by_id = {
        item.physical_table_id: item
        for item in table_region_artifact.physical_table_cell_coverage
    }
    for table_id in source_tables:
        _, table = source_tables[table_id]
        if coverage_by_id[table_id].table_ir_sha256 != _canonical_sha256(
            table.model_dump(mode="json")
        ):
            raise ValueError("table-region coverage TableIR identity drifted")

    base_non_table = {
        item.logical_object_id: item
        for item in base_proposal.logical_objects
        if item.object_kind is not D2LogicalObjectKind.TABLE
    }
    base_tables = {
        item.logical_object_id: item
        for item in base_proposal.logical_objects
        if item.object_kind is D2LogicalObjectKind.TABLE
    }
    retained_ids = {
        item.base_logical_object_id for item in placement_plan.retained_base_objects
    }
    retained_placement_by_id = {
        item.base_logical_object_id: item
        for item in placement_plan.retained_base_objects
    }
    replacement_ids = {
        item.base_logical_object_id for item in placement_plan.base_table_replacements
    }
    if retained_ids != set(base_non_table):
        raise ValueError(
            "every base non-table/container object must be retained exactly once"
        )
    if replacement_ids != set(base_tables):
        raise ValueError("every base TABLE object requires one replacement declaration")
    for object_id, base_object in base_non_table.items():
        if (
            base_object.parent_logical_object_id not in base_tables
            and retained_placement_by_id[object_id].parent_logical_object_id
            != base_object.parent_logical_object_id
        ):
            raise ValueError("retained base hierarchy cannot be silently rewritten")

    artifact_scope_ids = {
        item.logical_scope_id for item in table_region_artifact.logical_scopes
    }
    placement_scope_ids = {
        item.logical_scope_id for item in placement_plan.region_logical_scope_placements
    }
    if placement_scope_ids != artifact_scope_ids:
        raise ValueError("every region logical scope requires one parent placement")
    artifact_region_object_ids = {
        item.logical_object_id for item in table_region_artifact.logical_objects
    }
    placed_region_object_ids = tuple(
        object_id
        for item in placement_plan.region_logical_object_placements
        for object_id in item.source_region_logical_object_ids
    )
    if (
        len(placed_region_object_ids) != len(set(placed_region_object_ids))
        or set(placed_region_object_ids) != artifact_region_object_ids
    ):
        raise ValueError(
            "every LOGICAL_OBJECT_PART region object requires one explicit mapping"
        )
    for item in placement_plan.region_logical_object_placements:
        if item.mode is D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE:
            target = base_non_table.get(item.target_base_logical_object_id or "")
            if target is None:
                raise ValueError(
                    "region content may merge only into a retained base object"
                )
            if (
                item.parent_logical_object_id
                != retained_placement_by_id[
                    target.logical_object_id
                ].parent_logical_object_id
            ):
                raise ValueError(
                    "merged region content must preserve its retained parent"
                )

    supplemental_facts = _table_supplemental_catalog(
        source_ir,
        table_region_artifact,
    )
    region_placement_keys = {
        item.placement_key for item in placement_plan.region_logical_object_placements
    }
    regions_by_id = {item.region_id: item for item in table_region_artifact.regions}
    for item in placement_plan.supplemental_evidence_placements:
        fact = supplemental_facts.get((item.physical_table_id, item.evidence_kind))
        if fact is None or not fact.present:
            raise ValueError("only Host may mark absent table supplemental evidence")
        if (
            item.evidence_ref != fact.evidence_ref
            or item.evidence_sha256 != fact.evidence_sha256
        ):
            raise ValueError("supplemental evidence placement identity drifted")
        if item.disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT:
            if (
                item.owner_reference_kind
                is D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
                and item.owner_reference not in base_non_table
            ) or (
                item.owner_reference_kind
                is D2SupplementalObjectOwnerReferenceKindV1.REGION_LOGICAL_OBJECT_PLACEMENT
                and item.owner_reference not in region_placement_keys
            ):
                raise ValueError("supplemental evidence object owner does not exist")
        elif (
            item.disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE
            and item.owner_region_logical_scope_id not in artifact_scope_ids
        ):
            raise ValueError("supplemental evidence scope owner does not exist")
        elif item.disposition is D2TableSupplementalDispositionKindV1.NESTED:
            if item.nested_owner_region_id not in regions_by_id:
                raise ValueError("supplemental nested owner region does not exist")
        elif item.disposition is D2TableSupplementalDispositionKindV1.CELL_DERIVED:
            node, table = source_tables[item.physical_table_id]
            cells = tuple(
                sorted(
                    table.cells, key=lambda cell: (cell.row, cell.column, cell.cell_id)
                )
            )
            expected_cell_ids = tuple(cell.cell_id for cell in cells)
            derived_text = "\n".join(cell.raw_text for cell in cells)
            if (
                item.derived_cell_ids != expected_cell_ids
                or node.raw_text != derived_text
                or _text_sha256(derived_text) != item.evidence_sha256
            ):
                raise ValueError("CELL_DERIVED raw text derivation is not exact")

    scope_by_id = {
        item.logical_object_id: item for item in base_proposal.logical_table_scopes
    }
    regions_by_table: dict[str, list[D2CompiledTableRegionV1]] = {}
    for region in table_region_artifact.regions:
        regions_by_table.setdefault(region.physical_table_id, []).append(region)
    replacement_candidate_by_id = {
        item.base_logical_object_id: item
        for item in placement_plan.base_table_replacements
    }
    for object_id, item in base_tables.items():
        scope = scope_by_id[item.logical_table_scope_ids[0]]
        regions = tuple(
            region
            for table_id in scope.physical_table_ids
            for region in regions_by_table.get(table_id, ())
        )
        unresolved = any(
            region.disposition is D2TableRegionDispositionKind.UNRESOLVED
            for region in regions
        ) or bool(
            set(scope.physical_table_ids).intersection(
                table_region_artifact.unresolved_empty_table_ids
            )
        )
        expected = (
            D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
            if unresolved
            else D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
        )
        if replacement_candidate_by_id[object_id].disposition is not expected:
            raise ValueError(
                "base TABLE replacement declaration conflicts with region evidence"
            )


def _build_proposal_v2(
    source_ir: SourceIRSnapshot,
    *,
    base_proposal: D2LogicalStructureProposalV1,
    base_validation_receipt: D2LogicalStructureValidationReceiptV1,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureProposalV2:
    region_by_id = {item.region_id: item for item in table_region_artifact.regions}
    region_rank = {
        item.region_id: index
        for index, item in enumerate(table_region_artifact.regions)
    }
    region_object_by_id = {
        item.logical_object_id: item for item in table_region_artifact.logical_objects
    }
    retained_placement_by_id = {
        item.base_logical_object_id: item
        for item in placement_plan.retained_base_objects
    }

    target_by_region_object_id: dict[str, str] = {}
    placement_target_ids: dict[str, str] = {}
    for placement in placement_plan.region_logical_object_placements:
        ordered_region_ids = tuple(
            region_id
            for region in table_region_artifact.regions
            for region_id in (region.region_id,)
            if region.logical_object_id in placement.source_region_logical_object_ids
        )
        if placement.mode is D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE:
            target_id = placement.target_base_logical_object_id or ""
        else:
            assert placement.object_kind is not None
            target_id = stable_d2_v2_region_logical_object_id(
                source_ir_canonical_sha256=base_proposal.source_ir_canonical_sha256,
                object_kind=placement.object_kind,
                ordered_region_ids=ordered_region_ids,
            )
        placement_target_ids[placement.placement_key] = target_id
        for source_object_id in placement.source_region_logical_object_ids:
            target_by_region_object_id[source_object_id] = target_id

    direct_owner_by_region_id: dict[str, str] = {}
    for region in table_region_artifact.regions:
        if region.logical_scope_id is not None:
            direct_owner_by_region_id[region.region_id] = region.logical_scope_id
        elif region.logical_object_id is not None:
            direct_owner_by_region_id[region.region_id] = target_by_region_object_id[
                region.logical_object_id
            ]
    nested_evidence_by_owner: dict[str, list[str]] = {}
    for region in table_region_artifact.regions:
        if region.disposition is not D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            continue
        owner_id = _resolve_nested_logical_owner(
            region,
            region_by_id=region_by_id,
            direct_owner_by_region_id=direct_owner_by_region_id,
        )
        nested_evidence_by_owner.setdefault(owner_id, []).append(region.region_id)

    (
        physical_table_evidence,
        supplemental_evidence_by_owner,
        unresolved_supplemental_evidence_ids,
    ) = _build_physical_table_evidence(
        source_ir,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        placement_target_ids=placement_target_ids,
        region_by_id=region_by_id,
        direct_owner_by_region_id=direct_owner_by_region_id,
    )
    supplemental_rank = {
        item.evidence_id: index
        for index, item in enumerate(
            disposition
            for table in physical_table_evidence
            for disposition in table.supplemental_dispositions
        )
    }

    region_segment_by_id = {
        region.region_id: _region_segment_ref(
            source_ir=source_ir,
            table_region_artifact=table_region_artifact,
            region=region,
        )
        for region in table_region_artifact.regions
        if region.disposition
        in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }
    }

    merged_placement_by_base_id = {
        item.target_base_logical_object_id: item
        for item in placement_plan.region_logical_object_placements
        if item.target_base_logical_object_id is not None
    }
    logical_objects: list[D2LogicalObjectV2] = []
    for base_object in base_proposal.logical_objects:
        if base_object.object_kind is D2LogicalObjectKind.TABLE:
            continue
        retained = retained_placement_by_id[base_object.logical_object_id]
        merged = merged_placement_by_base_id.get(base_object.logical_object_id)
        region_ids = ()
        source_region_object_ids = ()
        merged_reasons: tuple[str, ...] = ()
        if merged is not None:
            source_region_object_ids = tuple(
                sorted(
                    merged.source_region_logical_object_ids,
                    key=lambda item: min(
                        region_rank[region_id]
                        for region_id in region_object_by_id[item].ordered_region_ids
                    ),
                )
            )
            region_ids = tuple(
                region_id
                for region in table_region_artifact.regions
                for region_id in (region.region_id,)
                if region.logical_object_id in source_region_object_ids
            )
            merged_reasons = merged.reason_codes
        segments = tuple(
            sorted(
                (
                    *(
                        _base_segment_ref(
                            source_ir=source_ir,
                            segment=segment,
                        )
                        for segment in base_object.source_segments
                    ),
                    *(region_segment_by_id[item] for item in region_ids),
                ),
                key=_segment_sort_key,
            )
        )
        pages = tuple(
            sorted(
                {
                    *base_object.page_numbers,
                    *(page for segment in segments for page in segment.page_numbers),
                }
            )
        )
        source_order = min(
            (base_object.source_order, *(segment.source_order for segment in segments))
        )
        nested_ids = tuple(
            sorted(
                nested_evidence_by_owner.get(base_object.logical_object_id, ()),
                key=region_rank.__getitem__,
            )
        )
        origin = (
            D2V2LogicalObjectOrigin.RETAINED_BASE_WITH_REGION_CONTENT
            if merged is not None
            else D2V2LogicalObjectOrigin.RETAINED_BASE
        )
        values = {
            "logical_object_id": base_object.logical_object_id,
            "origin": origin,
            "object_kind": base_object.object_kind,
            "parent_logical_object_id": retained.parent_logical_object_id,
            "source_order": source_order,
            "page_numbers": pages,
            "source_segments": segments,
            "nested_evidence_region_ids": nested_ids,
            "supplemental_evidence_ids": tuple(
                sorted(
                    supplemental_evidence_by_owner.get(
                        base_object.logical_object_id,
                        (),
                    ),
                    key=supplemental_rank.__getitem__,
                )
            ),
            "base_logical_object_id": base_object.logical_object_id,
            "base_logical_object_sha256": base_object.object_sha256,
            "region_logical_scope_id": None,
            "region_logical_scope_sha256": None,
            "source_region_logical_object_ids": source_region_object_ids,
            "reason_codes": _merge_reason_codes(
                base_object.reason_codes,
                retained.reason_codes,
                merged_reasons,
            ),
        }
        logical_objects.append(_logical_object_v2(values))

    scope_placement_by_id = {
        item.logical_scope_id: item
        for item in placement_plan.region_logical_scope_placements
    }
    for scope in table_region_artifact.logical_scopes:
        placement = scope_placement_by_id[scope.logical_scope_id]
        segments = tuple(
            region_segment_by_id[item] for item in scope.ordered_region_ids
        )
        nested_ids = tuple(
            sorted(
                nested_evidence_by_owner.get(scope.logical_scope_id, ()),
                key=region_rank.__getitem__,
            )
        )
        values = {
            "logical_object_id": scope.logical_scope_id,
            "origin": D2V2LogicalObjectOrigin.REGION_LOGICAL_SCOPE,
            "object_kind": D2LogicalObjectKind.TABLE,
            "parent_logical_object_id": placement.parent_logical_object_id,
            "source_order": min(item.source_order for item in segments),
            "page_numbers": tuple(
                sorted({page for item in segments for page in item.page_numbers})
            ),
            "source_segments": segments,
            "nested_evidence_region_ids": nested_ids,
            "supplemental_evidence_ids": tuple(
                sorted(
                    supplemental_evidence_by_owner.get(scope.logical_scope_id, ()),
                    key=supplemental_rank.__getitem__,
                )
            ),
            "base_logical_object_id": None,
            "base_logical_object_sha256": None,
            "region_logical_scope_id": scope.logical_scope_id,
            "region_logical_scope_sha256": scope.scope_sha256,
            "source_region_logical_object_ids": (),
            "reason_codes": _merge_reason_codes(
                scope.reason_codes,
                placement.reason_codes,
            ),
        }
        logical_objects.append(_logical_object_v2(values))

    for placement in placement_plan.region_logical_object_placements:
        if placement.mode is D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE:
            continue
        assert placement.object_kind is not None
        target_id = placement_target_ids[placement.placement_key]
        source_object_ids = tuple(
            sorted(
                placement.source_region_logical_object_ids,
                key=lambda item: min(
                    region_rank[region_id]
                    for region_id in region_object_by_id[item].ordered_region_ids
                ),
            )
        )
        region_ids = tuple(
            region_id
            for region in table_region_artifact.regions
            for region_id in (region.region_id,)
            if region.logical_object_id in source_object_ids
        )
        segments = tuple(region_segment_by_id[item] for item in region_ids)
        values = {
            "logical_object_id": target_id,
            "origin": D2V2LogicalObjectOrigin.REGION_LOGICAL_OBJECT,
            "object_kind": placement.object_kind,
            "parent_logical_object_id": placement.parent_logical_object_id,
            "source_order": min(item.source_order for item in segments),
            "page_numbers": tuple(
                sorted({page for item in segments for page in item.page_numbers})
            ),
            "source_segments": segments,
            "nested_evidence_region_ids": tuple(
                sorted(
                    nested_evidence_by_owner.get(target_id, ()),
                    key=region_rank.__getitem__,
                )
            ),
            "supplemental_evidence_ids": tuple(
                sorted(
                    supplemental_evidence_by_owner.get(target_id, ()),
                    key=supplemental_rank.__getitem__,
                )
            ),
            "base_logical_object_id": None,
            "base_logical_object_sha256": None,
            "region_logical_scope_id": None,
            "region_logical_scope_sha256": None,
            "source_region_logical_object_ids": source_object_ids,
            "reason_codes": placement.reason_codes,
        }
        logical_objects.append(_logical_object_v2(values))

    compiled_objects = tuple(
        sorted(
            logical_objects,
            key=lambda item: (item.source_order, item.logical_object_id),
        )
    )
    final_object_ids = {item.logical_object_id for item in compiled_objects}
    if len(final_object_ids) != len(compiled_objects):
        raise ValueError("region placement produced colliding V2 logical-object IDs")
    for item in compiled_objects:
        if (
            item.parent_logical_object_id is not None
            and item.parent_logical_object_id not in final_object_ids
        ):
            raise ValueError("region placement parent does not exist in V2")
    _reject_parent_cycles(compiled_objects)

    dispositions: list[D2V2TableRegionDispositionV1] = []
    for region in table_region_artifact.regions:
        logical_object_id = None
        source_segment_id = None
        if region.logical_scope_id is not None:
            logical_object_id = region.logical_scope_id
        elif region.logical_object_id is not None:
            logical_object_id = target_by_region_object_id[region.logical_object_id]
        if logical_object_id is not None:
            source_segment_id = region_segment_by_id[region.region_id].segment_id
        values = {
            "region_id": region.region_id,
            "region_sha256": region.region_sha256,
            "physical_table_id": region.physical_table_id,
            "physical_table_source_order": region.physical_table_source_order,
            "disposition": region.disposition,
            "logical_object_id": logical_object_id,
            "source_segment_id": source_segment_id,
            "nested_owner_region_id": region.nested_owner_region_id,
            "reason_codes": region.reason_codes,
        }
        draft = D2V2TableRegionDispositionV1.model_construct(
            **values,
            disposition_sha256="0" * 64,
        )
        dispositions.append(
            D2V2TableRegionDispositionV1(
                **values,
                disposition_sha256=stable_d2_v2_region_disposition_sha256(draft),
            )
        )
    compiled_dispositions = tuple(dispositions)

    replacements = _build_base_table_replacements(
        base_proposal=base_proposal,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        target_by_region_object_id=target_by_region_object_id,
        empty_table_ids=set(table_region_artifact.unresolved_empty_table_ids),
    )

    unresolved_region_ids = tuple(
        item.region_id
        for item in table_region_artifact.regions
        if item.disposition is D2TableRegionDispositionKind.UNRESOLVED
    )
    nested_region_ids = tuple(
        item.region_id
        for item in table_region_artifact.regions
        if item.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
    )
    false_region_ids = tuple(
        item.region_id
        for item in table_region_artifact.regions
        if item.disposition is D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION
    )
    partial = (
        base_proposal.status is D2LogicalStructureStatus.PARTIAL
        or table_region_artifact.status is D2TableRegionArtifactStatus.PARTIAL
        or bool(unresolved_region_ids)
        or bool(table_region_artifact.unresolved_empty_table_ids)
        or bool(unresolved_supplemental_evidence_ids)
        or any(
            item.disposition
            is D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
            for item in replacements
        )
    )
    status = (
        D2LogicalStructureStatus.PARTIAL
        if partial
        else D2LogicalStructureStatus.COMPLETE
    )
    unresolved_codes = set(base_proposal.unresolved_codes)
    if base_proposal.status is D2LogicalStructureStatus.PARTIAL:
        unresolved_codes.add("base_logical_structure_partial")
    if table_region_artifact.status is D2TableRegionArtifactStatus.PARTIAL:
        unresolved_codes.add("table_region_topology_partial")
    if unresolved_region_ids:
        unresolved_codes.add("unresolved_table_regions")
    if table_region_artifact.unresolved_empty_table_ids:
        unresolved_codes.add("empty_table_ir_unresolved")
    if unresolved_supplemental_evidence_ids:
        unresolved_codes.add("table_supplemental_evidence_unresolved")
    if any(
        item.disposition
        is D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
        for item in replacements
    ):
        unresolved_codes.add("base_table_replacement_unresolved")

    base_identity = D2ValidatedBaseLogicalStructureIdentityV1(
        artifact_id=base_validation_receipt.artifact_id,
        artifact_sha256=base_validation_receipt.artifact_sha256,
        proposal_sha256=base_proposal.proposal_sha256,
        validation_receipt_sha256=base_validation_receipt.receipt_sha256,
        evidence_sha256=base_validation_receipt.evidence_sha256,
        status=base_proposal.status,
        logical_object_ids=tuple(
            item.logical_object_id for item in base_proposal.logical_objects
        ),
        logical_table_scope_ids=tuple(
            item.logical_object_id for item in base_proposal.logical_table_scopes
        ),
        kit_bundle_sha256=expected_kit_identity.bundle_sha256,
    )
    region_identity = D2TableRegionArtifactIdentityV1(
        artifact_id=table_region_artifact.artifact_id,
        artifact_sha256=table_region_artifact.artifact_sha256,
        status=table_region_artifact.status,
        region_ids=tuple(item.region_id for item in table_region_artifact.regions),
        logical_scope_ids=tuple(
            item.logical_scope_id for item in table_region_artifact.logical_scopes
        ),
        logical_object_ids=tuple(
            item.logical_object_id for item in table_region_artifact.logical_objects
        ),
        kit_bundle_sha256=table_region_artifact.kit_bundle_sha256,
    )
    values = {
        "run_id": base_proposal.run_id,
        "source_sha256": base_proposal.source_sha256,
        "source_ir_id": base_proposal.source_ir_id,
        "source_ir_canonical_sha256": base_proposal.source_ir_canonical_sha256,
        "kit_identity": expected_kit_identity,
        "base_logical_structure": base_identity,
        "table_region_artifact": region_identity,
        "region_placement_plan_sha256": placement_plan.plan_sha256,
        "status": status,
        "logical_objects": compiled_objects,
        "retained_base_logical_object_ids": tuple(
            item.logical_object_id
            for item in base_proposal.logical_objects
            if item.object_kind is not D2LogicalObjectKind.TABLE
        ),
        "base_table_replacements": replacements,
        "table_region_dispositions": compiled_dispositions,
        "physical_table_evidence": physical_table_evidence,
        "unresolved_region_ids": unresolved_region_ids,
        "unresolved_empty_table_ids": (
            table_region_artifact.unresolved_empty_table_ids
        ),
        "unresolved_supplemental_evidence_ids": (unresolved_supplemental_evidence_ids),
        "nested_region_ids": nested_region_ids,
        "false_region_ids": false_region_ids,
        "unresolved_codes": tuple(sorted(unresolved_codes)),
    }
    draft = D2LogicalStructureProposalV2.model_construct(
        **values,
        proposal_sha256="0" * 64,
    )
    return D2LogicalStructureProposalV2(
        **values,
        proposal_sha256=stable_d2_logical_structure_proposal_v2_sha256(draft),
    )


def _build_base_table_replacements(
    *,
    base_proposal: D2LogicalStructureProposalV1,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    target_by_region_object_id: dict[str, str],
    empty_table_ids: set[str],
) -> tuple[D2V2BaseTableReplacementV1, ...]:
    scope_by_id = {
        item.logical_object_id: item for item in base_proposal.logical_table_scopes
    }
    candidate_by_id = {
        item.base_logical_object_id: item
        for item in placement_plan.base_table_replacements
    }
    result: list[D2V2BaseTableReplacementV1] = []
    for base_object in base_proposal.logical_objects:
        if base_object.object_kind is not D2LogicalObjectKind.TABLE:
            continue
        scope = scope_by_id[base_object.logical_table_scope_ids[0]]
        physical_ids = tuple(scope.physical_table_ids)
        regions = tuple(
            item
            for item in table_region_artifact.regions
            if item.physical_table_id in set(physical_ids)
        )
        logical_object_ids = tuple(
            dict.fromkeys(
                (
                    item.logical_scope_id
                    if item.logical_scope_id is not None
                    else target_by_region_object_id.get(item.logical_object_id or "")
                )
                for item in regions
                if item.logical_scope_id is not None
                or item.logical_object_id is not None
            )
        )
        values = {
            "base_logical_object_id": base_object.logical_object_id,
            "base_logical_object_sha256": base_object.object_sha256,
            "base_logical_table_scope_id": scope.logical_object_id,
            "base_logical_table_scope_sha256": scope.scope_sha256,
            "physical_table_ids": physical_ids,
            "disposition": candidate_by_id[base_object.logical_object_id].disposition,
            "region_ids": tuple(item.region_id for item in regions),
            "replacement_logical_object_ids": logical_object_ids,
            "nested_region_ids": tuple(
                item.region_id
                for item in regions
                if item.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
            ),
            "false_region_ids": tuple(
                item.region_id
                for item in regions
                if item.disposition
                is D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION
            ),
            "unresolved_region_ids": tuple(
                item.region_id
                for item in regions
                if item.disposition is D2TableRegionDispositionKind.UNRESOLVED
            ),
            "empty_physical_table_ids": tuple(
                item for item in physical_ids if item in empty_table_ids
            ),
            "reason_codes": candidate_by_id[base_object.logical_object_id].reason_codes,
        }
        draft = D2V2BaseTableReplacementV1.model_construct(
            **values,
            replacement_sha256="0" * 64,
        )
        result.append(
            D2V2BaseTableReplacementV1(
                **values,
                replacement_sha256=stable_d2_v2_base_table_replacement_sha256(draft),
            )
        )
    return tuple(result)


def _build_physical_table_evidence(
    source_ir: SourceIRSnapshot,
    *,
    table_region_artifact: D2TableRegionArtifactV1,
    placement_plan: D2RegionPlacementPlanV1,
    placement_target_ids: dict[str, str],
    region_by_id: dict[str, D2CompiledTableRegionV1],
    direct_owner_by_region_id: dict[str, str],
) -> tuple[
    tuple[D2V2PhysicalTableEvidenceLedgerV1, ...],
    dict[str, tuple[str, ...]],
    tuple[str, ...],
]:
    coverage_by_id = {
        item.physical_table_id: item
        for item in table_region_artifact.physical_table_cell_coverage
    }
    facts = _table_supplemental_catalog(source_ir, table_region_artifact)
    placement_by_key = {
        (item.physical_table_id, item.evidence_kind): item
        for item in placement_plan.supplemental_evidence_placements
    }
    result: list[D2V2PhysicalTableEvidenceLedgerV1] = []
    evidence_by_owner: dict[str, list[str]] = {}
    unresolved_ids: list[str] = []
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        table = node.table_payload
        coverage = coverage_by_id[table.table_id]
        supplemental_dispositions: list[D2V2TableSupplementalEvidenceDispositionV1] = []
        for evidence_kind in D2TableSupplementalEvidenceKindV1:
            fact = facts[(table.table_id, evidence_kind)]
            placement = placement_by_key.get((table.table_id, evidence_kind))
            owner_logical_object_id: str | None = None
            nested_owner_region_id: str | None = None
            cell_derivation: D2TableRawTextCellDerivationV1 | None = None
            derived_cell_ids: tuple[str, ...] = ()
            if placement is None:
                disposition = (
                    D2TableSupplementalDispositionKindV1.UNRESOLVED
                    if fact.present
                    else D2TableSupplementalDispositionKindV1.ABSENT
                )
                host_auto = True
                reason_codes = (
                    ("supplemental_evidence_unplaced",)
                    if fact.present
                    else ("host_confirmed_absent",)
                )
            else:
                disposition = placement.disposition
                host_auto = False
                reason_codes = placement.reason_codes
                cell_derivation = placement.cell_derivation
                derived_cell_ids = placement.derived_cell_ids
                if disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT:
                    if (
                        placement.owner_reference_kind
                        is D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
                    ):
                        owner_logical_object_id = placement.owner_reference
                    else:
                        owner_logical_object_id = placement_target_ids[
                            placement.owner_reference or ""
                        ]
                elif disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE:
                    owner_logical_object_id = placement.owner_region_logical_scope_id
                elif disposition is D2TableSupplementalDispositionKindV1.NESTED:
                    nested_owner_region_id = placement.nested_owner_region_id
                    owner_logical_object_id = _resolve_region_logical_owner(
                        nested_owner_region_id or "",
                        region_by_id=region_by_id,
                        direct_owner_by_region_id=direct_owner_by_region_id,
                    )
            evidence_id = stable_d2_v2_table_supplemental_evidence_id(
                physical_table_id=table.table_id,
                evidence_kind=evidence_kind,
                evidence_ref=fact.evidence_ref,
                evidence_sha256=fact.evidence_sha256,
            )
            disposition_values = {
                "evidence_id": evidence_id,
                "physical_table_id": table.table_id,
                "evidence_kind": evidence_kind,
                "evidence_ref": fact.evidence_ref,
                "evidence_sha256": fact.evidence_sha256,
                "disposition": disposition,
                "owner_logical_object_id": owner_logical_object_id,
                "nested_owner_region_id": nested_owner_region_id,
                "cell_derivation": cell_derivation,
                "derived_cell_ids": derived_cell_ids,
                "host_auto_dispositioned": host_auto,
                "reason_codes": reason_codes,
            }
            draft_disposition = (
                D2V2TableSupplementalEvidenceDispositionV1.model_construct(
                    **disposition_values,
                    disposition_sha256="0" * 64,
                )
            )
            compiled_disposition = D2V2TableSupplementalEvidenceDispositionV1(
                **disposition_values,
                disposition_sha256=(
                    stable_d2_v2_table_supplemental_disposition_sha256(
                        draft_disposition
                    )
                ),
            )
            supplemental_dispositions.append(compiled_disposition)
            if owner_logical_object_id is not None:
                evidence_by_owner.setdefault(owner_logical_object_id, []).append(
                    evidence_id
                )
            if disposition is D2TableSupplementalDispositionKindV1.UNRESOLVED:
                unresolved_ids.append(evidence_id)
        values = {
            "physical_table_id": table.table_id,
            "table_source_node_id": node.source_node_id,
            "physical_table_source_order": node.source_order,
            "table_source_node_sha256": _canonical_sha256(node.model_dump(mode="json")),
            "table_ir_sha256": _canonical_sha256(table.model_dump(mode="json")),
            "raw_text_sha256": _text_sha256(node.raw_text),
            "notes_sha256": _canonical_sha256(list(table.notes)),
            "visual_residual_sha256": _canonical_sha256(
                {
                    "source_visual_locator": (
                        None
                        if node.visual_locator is None
                        else node.visual_locator.model_dump(mode="json")
                    ),
                    "source_asset_ref": node.asset_ref,
                    "table_visual_asset_ref": table.visual_asset_ref,
                }
            ),
            "physical_region_coverage_sha256": coverage.cell_coverage_sha256,
            "ordered_region_ids": coverage.ordered_region_ids,
            "empty_table_disposition": coverage.empty_table_disposition,
            "supplemental_dispositions": tuple(supplemental_dispositions),
        }
        draft = D2V2PhysicalTableEvidenceLedgerV1.model_construct(
            **values,
            evidence_sha256="0" * 64,
        )
        result.append(
            D2V2PhysicalTableEvidenceLedgerV1(
                **values,
                evidence_sha256=stable_d2_v2_physical_table_evidence_sha256(draft),
            )
        )
    return (
        tuple(result),
        {key: tuple(value) for key, value in evidence_by_owner.items()},
        tuple(unresolved_ids),
    )


def _base_segment_ref(
    *,
    source_ir: SourceIRSnapshot,
    segment: D2SourceSegmentV1,
) -> D2V2BaseSourceSegmentRefV1:
    segment_id = stable_d2_v2_source_segment_id(
        segment_kind=D2V2SourceSegmentKind.BASE_SOURCE_SEGMENT,
        source_ir_id=source_ir.source_ir_id,
        source_identity=segment.segment_id,
        source_identity_sha256=segment.segment_sha256,
    )
    values = {
        "segment_id": segment_id,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "source_order": segment.source_order,
        "page_numbers": segment.page_numbers,
        "base_source_segment_id": segment.segment_id,
        "base_source_segment_sha256": segment.segment_sha256,
        "handle_kind": segment.handle_kind,
        "source_handle": segment.source_handle,
        "owner_source_node_id": segment.owner_source_node_id,
        "start_offset": segment.start_offset,
        "end_offset": segment.end_offset,
    }
    draft = D2V2BaseSourceSegmentRefV1.model_construct(
        **values,
        segment_sha256="0" * 64,
    )
    return D2V2BaseSourceSegmentRefV1(
        **values,
        segment_sha256=stable_d2_v2_source_segment_sha256(draft),
    )


def _region_segment_ref(
    *,
    source_ir: SourceIRSnapshot,
    table_region_artifact: D2TableRegionArtifactV1,
    region: D2CompiledTableRegionV1,
) -> D2V2TableRegionSourceSegmentRefV1:
    segment_id = stable_d2_v2_source_segment_id(
        segment_kind=D2V2SourceSegmentKind.TABLE_REGION,
        source_ir_id=source_ir.source_ir_id,
        source_identity=region.region_id,
        source_identity_sha256=region.region_sha256,
    )
    pages = tuple(
        sorted(
            {ref.page_number for ref in region.cell_refs if ref.page_number is not None}
        )
    )
    values = {
        "segment_id": segment_id,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "source_order": region.physical_table_source_order,
        "page_numbers": pages,
        "table_region_artifact_id": table_region_artifact.artifact_id,
        "table_region_artifact_sha256": table_region_artifact.artifact_sha256,
        "table_region_id": region.region_id,
        "table_region_sha256": region.region_sha256,
        "physical_table_id": region.physical_table_id,
        "table_source_node_id": region.table_source_node_id,
    }
    draft = D2V2TableRegionSourceSegmentRefV1.model_construct(
        **values,
        segment_sha256="0" * 64,
    )
    return D2V2TableRegionSourceSegmentRefV1(
        **values,
        segment_sha256=stable_d2_v2_source_segment_sha256(draft),
    )


def _logical_object_v2(values: dict[str, object]) -> D2LogicalObjectV2:
    draft = D2LogicalObjectV2.model_construct(
        **values,
        object_sha256="0" * 64,
    )
    return D2LogicalObjectV2(
        **values,
        object_sha256=stable_d2_v2_logical_object_sha256(draft),
    )


def _resolve_nested_logical_owner(
    region: D2CompiledTableRegionV1,
    *,
    region_by_id: dict[str, D2CompiledTableRegionV1],
    direct_owner_by_region_id: dict[str, str],
) -> str:
    owner_region_id = region.nested_owner_region_id
    if owner_region_id is None:
        raise ValueError("nested region does not declare an owner region")
    return _resolve_region_logical_owner(
        owner_region_id,
        region_by_id=region_by_id,
        direct_owner_by_region_id=direct_owner_by_region_id,
        initial_seen={region.region_id},
    )


def _resolve_region_logical_owner(
    region_id: str,
    *,
    region_by_id: dict[str, D2CompiledTableRegionV1],
    direct_owner_by_region_id: dict[str, str],
    initial_seen: set[str] | None = None,
) -> str:
    current: str | None = region_id
    seen = set(initial_seen or ())
    while current is not None:
        if current in seen:
            raise ValueError("nested region owner lineage contains a cycle")
        seen.add(current)
        owner = direct_owner_by_region_id.get(current)
        if owner is not None:
            return owner
        current_region = region_by_id.get(current)
        if current_region is None:
            break
        current = current_region.nested_owner_region_id
    raise ValueError("region does not resolve to a logical-object owner")


def stable_d2_region_placement_plan_sha256(
    plan: D2RegionPlacementPlanV1,
) -> str:
    payload = plan.model_dump(mode="json", exclude={"plan_sha256"})
    for key, sort_key in (
        ("retained_base_objects", "base_logical_object_id"),
        ("base_table_replacements", "base_logical_object_id"),
        ("region_logical_scope_placements", "logical_scope_id"),
        ("region_logical_object_placements", "placement_key"),
    ):
        payload[key] = sorted(payload[key], key=lambda item: item[sort_key])
    payload["supplemental_evidence_placements"] = sorted(
        payload["supplemental_evidence_placements"],
        key=lambda item: (item["physical_table_id"], item["evidence_kind"]),
    )
    for item in payload["region_logical_object_placements"]:
        item["source_region_logical_object_ids"] = sorted(
            item["source_region_logical_object_ids"]
        )
    return _canonical_sha256(payload)


def stable_d2_v2_source_segment_id(
    *,
    segment_kind: D2V2SourceSegmentKind,
    source_ir_id: str,
    source_identity: str,
    source_identity_sha256: str,
) -> str:
    digest = _canonical_sha256(
        {
            "segment_kind": segment_kind.value,
            "source_ir_id": source_ir_id,
            "source_identity": source_identity,
            "source_identity_sha256": source_identity_sha256,
        }
    )
    return f"d2-v2-source-segment-{digest[:32]}"


def stable_d2_v2_source_segment_sha256(
    segment: D2V2BaseSourceSegmentRefV1 | D2V2TableRegionSourceSegmentRefV1,
) -> str:
    return _canonical_sha256(
        segment.model_dump(mode="json", exclude={"segment_sha256"})
    )


def stable_d2_v2_region_logical_object_id(
    *,
    source_ir_canonical_sha256: str,
    object_kind: D2LogicalObjectKind,
    ordered_region_ids: tuple[str, ...],
) -> str:
    digest = _canonical_sha256(
        {
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "object_kind": object_kind.value,
            "ordered_region_ids": list(ordered_region_ids),
        }
    )
    return f"d2-v2-region-object-{digest[:32]}"


def stable_d2_v2_logical_object_sha256(item: D2LogicalObjectV2) -> str:
    return _canonical_sha256(item.model_dump(mode="json", exclude={"object_sha256"}))


def stable_d2_v2_region_disposition_sha256(
    item: D2V2TableRegionDispositionV1,
) -> str:
    return _canonical_sha256(
        item.model_dump(mode="json", exclude={"disposition_sha256"})
    )


def stable_d2_v2_base_table_replacement_sha256(
    item: D2V2BaseTableReplacementV1,
) -> str:
    return _canonical_sha256(
        item.model_dump(mode="json", exclude={"replacement_sha256"})
    )


def stable_d2_v2_table_supplemental_evidence_id(
    *,
    physical_table_id: str,
    evidence_kind: D2TableSupplementalEvidenceKindV1,
    evidence_ref: str,
    evidence_sha256: str,
) -> str:
    digest = _canonical_sha256(
        {
            "physical_table_id": physical_table_id,
            "evidence_kind": evidence_kind.value,
            "evidence_ref": evidence_ref,
            "evidence_sha256": evidence_sha256,
        }
    )
    return f"d2-v2-table-supplement-{digest[:32]}"


def stable_d2_v2_table_supplemental_disposition_sha256(
    item: D2V2TableSupplementalEvidenceDispositionV1,
) -> str:
    return _canonical_sha256(
        item.model_dump(mode="json", exclude={"disposition_sha256"})
    )


def stable_d2_v2_physical_table_evidence_sha256(
    item: D2V2PhysicalTableEvidenceLedgerV1,
) -> str:
    return _canonical_sha256(item.model_dump(mode="json", exclude={"evidence_sha256"}))


def stable_d2_logical_structure_proposal_v2_sha256(
    proposal: D2LogicalStructureProposalV2,
) -> str:
    return _canonical_sha256(
        proposal.model_dump(mode="json", exclude={"proposal_sha256"})
    )


def stable_d2_v2_artifact_id(
    *,
    run_id: str,
    source_sha256: str,
    proposal_sha256: str,
) -> str:
    digest = _canonical_sha256(
        {
            "schema_version": D2_LOGICAL_STRUCTURE_PROPOSAL_V2_SCHEMA_VERSION,
            "run_id": run_id,
            "source_sha256": source_sha256,
            "proposal_sha256": proposal_sha256,
        }
    )
    return f"agent-artifact-{digest[:32]}"


def stable_d2_v2_validation_receipt_sha256(
    receipt: D2LogicalStructureValidationReceiptV2,
) -> str:
    return _canonical_sha256(
        receipt.model_dump(mode="json", exclude={"receipt_sha256"})
    )


def _source_table_catalog(
    source_ir: SourceIRSnapshot,
) -> dict[str, tuple[SourceNode, TableIR]]:
    result: dict[str, tuple[SourceNode, TableIR]] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        table = node.table_payload
        if table.table_id in result:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        result[table.table_id] = (node, table)
    return result


def _table_supplemental_catalog(
    source_ir: SourceIRSnapshot,
    table_region_artifact: D2TableRegionArtifactV1,
) -> dict[
    tuple[str, D2TableSupplementalEvidenceKindV1],
    _TableSupplementalFact,
]:
    source_tables = _source_table_catalog(source_ir)
    coverage_by_id = {
        item.physical_table_id: item
        for item in table_region_artifact.physical_table_cell_coverage
    }
    if set(coverage_by_id) != set(source_tables):
        raise ValueError("table-region artifact supplemental coverage drifted")
    result: dict[
        tuple[str, D2TableSupplementalEvidenceKindV1],
        _TableSupplementalFact,
    ] = {}
    for table_id, (node, table) in source_tables.items():
        coverage = coverage_by_id[table_id]
        raw_sha = _text_sha256(node.raw_text)
        notes_sha = _canonical_sha256(list(table.notes))
        visual_payload = [
            item.model_dump(mode="json")
            for item in coverage.supplemental_visual_evidence
        ]
        visual_sha = _canonical_sha256(visual_payload)
        if (
            coverage.table_node_raw_text_sha256 != raw_sha
            or coverage.table_notes_sha256 != notes_sha
        ):
            raise ValueError("table-region supplemental evidence hash drifted")
        facts = (
            _TableSupplementalFact(
                physical_table_id=table_id,
                evidence_kind=(D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT),
                evidence_ref=f"table-node-raw-text:{table_id}",
                evidence_sha256=raw_sha,
                present=bool(node.raw_text),
            ),
            _TableSupplementalFact(
                physical_table_id=table_id,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_NOTES,
                evidence_ref=f"table-notes:{table_id}",
                evidence_sha256=notes_sha,
                present=bool(table.notes),
            ),
            _TableSupplementalFact(
                physical_table_id=table_id,
                evidence_kind=D2TableSupplementalEvidenceKindV1.TABLE_VISUAL,
                evidence_ref=f"table-visual:{table_id}",
                evidence_sha256=visual_sha,
                present=bool(visual_payload),
            ),
        )
        for fact in facts:
            result[(table_id, fact.evidence_kind)] = fact
    return result


def _segment_sort_key(
    item: D2V2BaseSourceSegmentRefV1 | D2V2TableRegionSourceSegmentRefV1,
) -> tuple[object, ...]:
    return (
        item.source_order,
        item.segment_kind.value,
        (
            item.base_source_segment_id
            if isinstance(item, D2V2BaseSourceSegmentRefV1)
            else item.table_region_id
        ),
        item.segment_id,
    )


def _reject_parent_cycles(objects: tuple[D2LogicalObjectV2, ...]) -> None:
    object_by_id = {item.logical_object_id: item for item in objects}
    for object_id in object_by_id:
        seen: set[str] = set()
        current: str | None = object_id
        while current is not None:
            if current in seen:
                raise ValueError("V2 logical-object parentage contains a cycle")
            seen.add(current)
            owner = object_by_id.get(current)
            if owner is None:
                raise ValueError("V2 logical-object parent does not exist")
            current = owner.parent_logical_object_id


def _merge_reason_codes(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({item for group in groups for item in group}))


def _canonical_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    if value != tuple(sorted(set(value))) or any(
        _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
    ):
        raise ValueError("reason codes must be unique canonical lowercase tokens")
    return value


def _canonical_pages(value: tuple[int, ...]) -> tuple[int, ...]:
    if value != tuple(sorted(set(value))) or any(item < 1 for item in value):
        raise ValueError("page numbers must be unique sorted positive integers")
    return value


def _require_unique(values: object, label: str) -> None:
    materialized = tuple(values)  # type: ignore[arg-type]
    if len(materialized) != len(set(materialized)):
        raise ValueError(f"{label} must be unique")


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=lambda item: item.model_dump(mode="json"),
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "D2BaseTableReplacementCandidateV1",
    "D2BaseTableReplacementDisposition",
    "D2LogicalObjectV2",
    "D2LogicalStructureCompilationV2",
    "D2LogicalStructureProposalV2",
    "D2LogicalStructureValidationReceiptV2",
    "D2RegionLogicalObjectPlacementMode",
    "D2RegionLogicalObjectPlacementV1",
    "D2RegionLogicalScopePlacementV1",
    "D2RegionPlacementPlanV1",
    "D2RetainedBaseObjectPlacementV1",
    "D2SupplementalObjectOwnerReferenceKindV1",
    "D2TableRawTextCellDerivationV1",
    "D2TableSupplementalDispositionKindV1",
    "D2TableSupplementalEvidenceKindV1",
    "D2TableSupplementalEvidencePlacementV1",
    "D2V2BaseSourceSegmentRefV1",
    "D2V2BaseTableReplacementV1",
    "D2V2LogicalObjectOrigin",
    "D2V2PhysicalTableEvidenceLedgerV1",
    "D2V2SourceSegmentKind",
    "D2V2TableRegionDispositionV1",
    "D2V2TableSupplementalEvidenceDispositionV1",
    "D2V2TableRegionSourceSegmentRefV1",
    "build_d2_region_placement_plan",
    "build_d2_table_supplemental_object_owner_placements",
    "compile_d2_logical_structure_v2",
    "stable_d2_logical_structure_proposal_v2_sha256",
    "stable_d2_region_placement_plan_sha256",
    "stable_d2_v2_artifact_id",
    "stable_d2_v2_base_table_replacement_sha256",
    "stable_d2_v2_logical_object_sha256",
    "stable_d2_v2_physical_table_evidence_sha256",
    "stable_d2_v2_region_disposition_sha256",
    "stable_d2_v2_region_logical_object_id",
    "stable_d2_v2_source_segment_id",
    "stable_d2_v2_source_segment_sha256",
    "stable_d2_v2_table_supplemental_disposition_sha256",
    "stable_d2_v2_table_supplemental_evidence_id",
    "stable_d2_v2_validation_receipt_sha256",
    "validate_d2_logical_structure_proposal_v2",
]
