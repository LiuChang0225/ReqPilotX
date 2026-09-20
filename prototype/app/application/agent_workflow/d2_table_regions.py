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
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
)


D2_TABLE_REGION_CANDIDATE_SCHEMA_VERSION = "d2-table-region-candidate/v1"
D2_TABLE_REGION_ARTIFACT_SCHEMA_VERSION = "d2-table-region-artifact/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")
_HOST_REGION_KEY_PREFIX = "host-auto-"
_MAX_PHYSICAL_TABLES = 2_000
_MAX_TABLE_CELLS = 500_000
_MAX_REGIONS = 100_000
_MAX_REGIONS_PER_TABLE = 100_000
_MAX_GROUP_MEMBERS = 100_000
_MAX_SPANS_PER_REGION = 100_000
_MAX_SUPPLEMENTAL_EVIDENCE = 16


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2TableRegionDispositionKind(StrEnum):
    LOGICAL_SCOPE_PART = "LOGICAL_SCOPE_PART"
    LOGICAL_OBJECT_PART = "LOGICAL_OBJECT_PART"
    NESTED_VALUE_REGION = "NESTED_VALUE_REGION"
    NON_BUSINESS_FALSE_REGION = "NON_BUSINESS_FALSE_REGION"
    UNRESOLVED = "UNRESOLVED"


class D2TableRegionGapPolicy(StrEnum):
    """Host policy for candidate cells that received no disposition."""

    REJECT = "REJECT"
    AUTO_FILL_UNRESOLVED = "AUTO_FILL_UNRESOLVED"


class D2TableRegionArtifactStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class D2EmptyPhysicalTableDispositionKind(StrEnum):
    NON_BUSINESS_FALSE_REGION = "NON_BUSINESS_FALSE_REGION"
    UNRESOLVED = "UNRESOLVED"


class D2TableRegionRowAlignment(StrEnum):
    FULL_ROWS = "FULL_ROWS"
    PARTIAL_ROWS = "PARTIAL_ROWS"
    MIXED_FULL_AND_PARTIAL_ROWS = "MIXED_FULL_AND_PARTIAL_ROWS"


class D2TableRegionRowMaterializerEligibility(StrEnum):
    """SourceIR-shape precheck; not a materializer acceptance receipt."""

    SHAPE_ELIGIBLE_FULL_ROWS = "SHAPE_ELIGIBLE_FULL_ROWS"
    INELIGIBLE_DISPOSITION = "INELIGIBLE_DISPOSITION"
    INELIGIBLE_PARTIAL_ROW = "INELIGIBLE_PARTIAL_ROW"
    INELIGIBLE_NONCONTIGUOUS_ROWS = "INELIGIBLE_NONCONTIGUOUS_ROWS"


class D2TableSupplementalEvidenceKind(StrEnum):
    TABLE_VISUAL_LOCATOR = "TABLE_VISUAL_LOCATOR"
    TABLE_VISUAL_ASSET = "TABLE_VISUAL_ASSET"
    VISUAL_TABLE_SNAPSHOT = "VISUAL_TABLE_SNAPSHOT"


class D2PhysicalTableCellOrdinalSpanV1(_StrictFrozenModel):
    """Half-open span over the Host-defined row/column-sorted TableIR cells."""

    start_cell_ordinal: StrictInt = Field(ge=0)
    end_cell_ordinal_exclusive: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def validate_non_empty(self) -> D2PhysicalTableCellOrdinalSpanV1:
        if self.end_cell_ordinal_exclusive <= self.start_cell_ordinal:
            raise ValueError("cell ordinal spans must be non-empty half-open intervals")
        return self


class _D2TableRegionCandidateBaseV1(_StrictFrozenModel):
    region_key: str = Field(pattern=_KEY_PATTERN.pattern)
    physical_table_id: str = Field(min_length=1, max_length=160)
    cell_spans: tuple[D2PhysicalTableCellOrdinalSpanV1, ...] = Field(
        min_length=1,
        max_length=_MAX_SPANS_PER_REGION,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("region_key")
    @classmethod
    def reserve_host_region_keys(cls, value: str) -> str:
        if value.startswith(_HOST_REGION_KEY_PREFIX):
            raise ValueError(
                "candidate region keys cannot use the Host-reserved prefix"
            )
        return value

    @field_validator("physical_table_id")
    @classmethod
    def reject_blank_table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("physical table IDs must not be blank")
        return value

    @field_validator("cell_spans")
    @classmethod
    def require_canonical_spans(
        cls,
        value: tuple[D2PhysicalTableCellOrdinalSpanV1, ...],
    ) -> tuple[D2PhysicalTableCellOrdinalSpanV1, ...]:
        keys = tuple(
            (span.start_cell_ordinal, span.end_cell_ordinal_exclusive) for span in value
        )
        if keys != tuple(sorted(keys)):
            raise ValueError("cell ordinal spans must be in canonical ascending order")
        for previous, current in zip(value, value[1:], strict=False):
            if previous.end_cell_ordinal_exclusive >= current.start_cell_ordinal:
                raise ValueError(
                    "cell ordinal spans must be non-overlapping and maximally merged"
                )
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reason codes must be unique canonical lowercase tokens")
        return value


class D2LogicalScopePartTableRegionCandidateV1(_D2TableRegionCandidateBaseV1):
    disposition: Literal[D2TableRegionDispositionKind.LOGICAL_SCOPE_PART]
    logical_scope_key: str = Field(pattern=_KEY_PATTERN.pattern)


class D2LogicalObjectPartTableRegionCandidateV1(_D2TableRegionCandidateBaseV1):
    disposition: Literal[D2TableRegionDispositionKind.LOGICAL_OBJECT_PART]
    logical_object_key: str = Field(pattern=_KEY_PATTERN.pattern)


class D2NestedValueTableRegionCandidateV1(_D2TableRegionCandidateBaseV1):
    disposition: Literal[D2TableRegionDispositionKind.NESTED_VALUE_REGION]
    nested_owner_region_key: str = Field(pattern=_KEY_PATTERN.pattern)


class D2NonBusinessFalseTableRegionCandidateV1(_D2TableRegionCandidateBaseV1):
    disposition: Literal[D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION]


class D2UnresolvedTableRegionCandidateV1(_D2TableRegionCandidateBaseV1):
    disposition: Literal[D2TableRegionDispositionKind.UNRESOLVED]


D2TableRegionCandidateV1 = Annotated[
    D2LogicalScopePartTableRegionCandidateV1
    | D2LogicalObjectPartTableRegionCandidateV1
    | D2NestedValueTableRegionCandidateV1
    | D2NonBusinessFalseTableRegionCandidateV1
    | D2UnresolvedTableRegionCandidateV1,
    Field(discriminator="disposition"),
]


class D2TableLogicalScopeCandidateV1(_StrictFrozenModel):
    """Explicit continuation topology expressed only with candidate region keys."""

    logical_scope_key: str = Field(pattern=_KEY_PATTERN.pattern)
    ordered_region_keys: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_GROUP_MEMBERS,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("ordered_region_keys")
    @classmethod
    def validate_region_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _KEY_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("logical scope region keys must be unique valid keys")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reason codes must be unique canonical lowercase tokens")
        return value


class D2EmptyPhysicalTableDispositionCandidateV1(_StrictFrozenModel):
    """Explicit semantic disposition for a TableIR with zero cell anchors."""

    physical_table_id: str = Field(min_length=1, max_length=160)
    disposition: D2EmptyPhysicalTableDispositionKind
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("physical_table_id")
    @classmethod
    def reject_blank_table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("physical table IDs must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("reason codes must be unique canonical lowercase tokens")
        return value


class D2TableRegionCandidateArtifactV1(_StrictFrozenModel):
    """Runner output: source-bound ordinal decisions without final IDs or hashes."""

    schema_version: Literal["d2-table-region-candidate/v1"] = (
        D2_TABLE_REGION_CANDIDATE_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    regions: tuple[D2TableRegionCandidateV1, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    logical_scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    empty_table_dispositions: tuple[D2EmptyPhysicalTableDispositionCandidateV1, ...] = (
        Field(default=(), max_length=_MAX_PHYSICAL_TABLES)
    )

    @model_validator(mode="after")
    def validate_candidate_topology(self) -> D2TableRegionCandidateArtifactV1:
        regions_by_key = {region.region_key: region for region in self.regions}
        if len(regions_by_key) != len(self.regions):
            raise ValueError("candidate region keys must be globally unique")

        empty_table_ids = tuple(
            item.physical_table_id for item in self.empty_table_dispositions
        )
        if len(empty_table_ids) != len(set(empty_table_ids)):
            raise ValueError("empty physical table dispositions must be unique")

        scopes_by_key = {
            scope.logical_scope_key: scope for scope in self.logical_scopes
        }
        if len(scopes_by_key) != len(self.logical_scopes):
            raise ValueError("candidate logical scope keys must be globally unique")

        scope_regions = {
            region.region_key: region
            for region in self.regions
            if isinstance(region, D2LogicalScopePartTableRegionCandidateV1)
        }
        referenced_scope_regions: set[str] = set()
        for scope in self.logical_scopes:
            for region_key in scope.ordered_region_keys:
                region = scope_regions.get(region_key)
                if region is None:
                    raise ValueError(
                        "logical scopes may reference only LOGICAL_SCOPE_PART regions"
                    )
                if region.logical_scope_key != scope.logical_scope_key:
                    raise ValueError(
                        "logical scope key must match every referenced region"
                    )
                if region_key in referenced_scope_regions:
                    raise ValueError(
                        "a scope region may belong to only one logical scope"
                    )
                referenced_scope_regions.add(region_key)
        if referenced_scope_regions != set(scope_regions):
            raise ValueError(
                "every LOGICAL_SCOPE_PART region requires exactly one scope declaration"
            )
        declared_scope_keys = {
            region.logical_scope_key for region in scope_regions.values()
        }
        if declared_scope_keys != set(scopes_by_key):
            raise ValueError("logical scope declarations must match scope-region keys")

        nested_edges: dict[str, str] = {}
        for region in self.regions:
            if not isinstance(region, D2NestedValueTableRegionCandidateV1):
                continue
            if region.nested_owner_region_key not in regions_by_key:
                raise ValueError(
                    "nested region owner must reference an existing region"
                )
            if region.nested_owner_region_key == region.region_key:
                raise ValueError("nested region cannot own itself")
            owner = regions_by_key[region.nested_owner_region_key]
            if isinstance(
                owner,
                (
                    D2NonBusinessFalseTableRegionCandidateV1,
                    D2UnresolvedTableRegionCandidateV1,
                ),
            ):
                raise ValueError(
                    "nested region owner must be a business-bearing region"
                )
            nested_edges[region.region_key] = region.nested_owner_region_key
        _require_acyclic_edges(nested_edges, "nested region ownership")
        _require_candidate_nested_business_owners(regions_by_key)
        return self


class D2TableCellSourceRefV1(_StrictFrozenModel):
    """Content-free Host expansion of one exact immutable TableIR cell."""

    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    canonical_cell_ordinal: StrictInt = Field(ge=0)
    cell_id: str = Field(min_length=1, max_length=160)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    page_number: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    native_locator_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    raw_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    formula_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN.pattern)
    display_value_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN.pattern,
    )
    cell_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_evidence(self) -> D2TableCellSourceRefV1:
        expected = stable_d2_table_cell_evidence_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            table_source_node_id=self.table_source_node_id,
            physical_table_id=self.physical_table_id,
            table_ir_sha256=self.table_ir_sha256,
            canonical_cell_ordinal=self.canonical_cell_ordinal,
            cell_id=self.cell_id,
            row=self.row,
            column=self.column,
            row_span=self.row_span,
            column_span=self.column_span,
            page_number=self.page_number,
            bbox=self.bbox,
            native_locator_sha256=self.native_locator_sha256,
            raw_text_sha256=self.raw_text_sha256,
            formula_sha256=self.formula_sha256,
            display_value_sha256=self.display_value_sha256,
        )
        if self.cell_evidence_sha256 != expected:
            raise ValueError("cell evidence SHA must bind the exact SourceIR cell")
        return self


class D2CompiledTableRegionV1(_StrictFrozenModel):
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    candidate_region_key: str = Field(pattern=_KEY_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    cell_spans: tuple[D2PhysicalTableCellOrdinalSpanV1, ...] = Field(
        min_length=1,
        max_length=_MAX_SPANS_PER_REGION,
    )
    cell_refs: tuple[D2TableCellSourceRefV1, ...] = Field(
        min_length=1,
        max_length=_MAX_TABLE_CELLS,
    )
    disposition: D2TableRegionDispositionKind
    logical_scope_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$",
    )
    logical_object_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-object-[0-9a-f]{32}$",
    )
    nested_owner_region_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-region-[0-9a-f]{32}$",
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    host_auto_filled: StrictBool = False
    covered_row_indices: tuple[StrictInt, ...] = Field(
        min_length=1,
        max_length=_MAX_TABLE_CELLS,
    )
    fully_covered_row_indices: tuple[StrictInt, ...] = Field(
        default=(),
        max_length=_MAX_TABLE_CELLS,
    )
    partially_covered_row_indices: tuple[StrictInt, ...] = Field(
        default=(),
        max_length=_MAX_TABLE_CELLS,
    )
    row_alignment: D2TableRegionRowAlignment
    row_materializer_eligibility: D2TableRegionRowMaterializerEligibility
    region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_region(self) -> D2CompiledTableRegionV1:
        refs = self.cell_refs
        ordinals = tuple(ref.canonical_cell_ordinal for ref in refs)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError("compiled region cell refs must be unique and canonical")
        expected_ordinals = tuple(
            ordinal
            for span in self.cell_spans
            for ordinal in range(
                span.start_cell_ordinal,
                span.end_cell_ordinal_exclusive,
            )
        )
        if ordinals != expected_ordinals:
            raise ValueError("compiled region cell refs must exactly expand its spans")
        if any(
            ref.source_ir_id != self.source_ir_id
            or ref.source_sha256 != self.source_sha256
            or ref.source_ir_canonical_sha256 != self.source_ir_canonical_sha256
            or ref.physical_table_id != self.physical_table_id
            or ref.table_source_node_id != self.table_source_node_id
            for ref in refs
        ):
            raise ValueError("compiled region cell refs must share its source binding")

        fields = (
            self.logical_scope_id,
            self.logical_object_id,
            self.nested_owner_region_id,
        )
        expected_present = {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART: (True, False, False),
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART: (False, True, False),
            D2TableRegionDispositionKind.NESTED_VALUE_REGION: (False, False, True),
            D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION: (
                False,
                False,
                False,
            ),
            D2TableRegionDispositionKind.UNRESOLVED: (False, False, False),
        }[self.disposition]
        if tuple(value is not None for value in fields) != expected_present:
            raise ValueError(
                "compiled region disposition fields are mutually exclusive"
            )
        if (
            self.host_auto_filled
            and self.disposition is not D2TableRegionDispositionKind.UNRESOLVED
        ):
            raise ValueError("Host auto-fill may create only UNRESOLVED regions")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in self.reason_codes
        ):
            raise ValueError("compiled reason codes must be canonical")

        for rows in (
            self.covered_row_indices,
            self.fully_covered_row_indices,
            self.partially_covered_row_indices,
        ):
            if rows != tuple(sorted(set(rows))):
                raise ValueError("row alignment indices must be unique and canonical")
        if set(self.fully_covered_row_indices).intersection(
            self.partially_covered_row_indices
        ):
            raise ValueError("full and partial row ledgers must not overlap")
        if self.covered_row_indices != tuple(
            sorted(
                (*self.fully_covered_row_indices, *self.partially_covered_row_indices)
            )
        ):
            raise ValueError(
                "row alignment ledgers must exactly partition covered rows"
            )
        expected_alignment = _row_alignment(
            full_rows=self.fully_covered_row_indices,
            partial_rows=self.partially_covered_row_indices,
        )
        if self.row_alignment is not expected_alignment:
            raise ValueError("row alignment must reflect full and partial row ledgers")
        expected_eligibility = _row_materializer_eligibility(
            disposition=self.disposition,
            full_rows=self.fully_covered_row_indices,
            partial_rows=self.partially_covered_row_indices,
        )
        if self.row_materializer_eligibility is not expected_eligibility:
            raise ValueError(
                "row materializer eligibility must be a conservative Host shape summary"
            )

        expected_id = stable_d2_table_region_id(
            physical_table_id=self.physical_table_id,
            table_source_node_id=self.table_source_node_id,
            table_ir_sha256=refs[0].table_ir_sha256,
            cell_local_evidence_sha256s=tuple(
                stable_d2_table_cell_local_evidence_sha256(ref) for ref in refs
            ),
        )
        if self.region_id != expected_id:
            raise ValueError("region ID must bind its exact physical source cells")
        expected_sha = stable_d2_compiled_table_region_sha256(
            region_id=self.region_id,
            candidate_region_key=self.candidate_region_key,
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            kit_bundle_sha256=self.kit_bundle_sha256,
            physical_table_id=self.physical_table_id,
            table_source_node_id=self.table_source_node_id,
            physical_table_source_order=self.physical_table_source_order,
            cell_spans=self.cell_spans,
            cell_refs=self.cell_refs,
            disposition=self.disposition,
            logical_scope_id=self.logical_scope_id,
            logical_object_id=self.logical_object_id,
            nested_owner_region_id=self.nested_owner_region_id,
            reason_codes=self.reason_codes,
            host_auto_filled=self.host_auto_filled,
            covered_row_indices=self.covered_row_indices,
            fully_covered_row_indices=self.fully_covered_row_indices,
            partially_covered_row_indices=self.partially_covered_row_indices,
            row_alignment=self.row_alignment,
            row_materializer_eligibility=self.row_materializer_eligibility,
        )
        if self.region_sha256 != expected_sha:
            raise ValueError("region SHA must bind its topology and evidence")
        return self


class D2CompiledTableLogicalScopeV1(_StrictFrozenModel):
    logical_scope_id: str = Field(pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$")
    candidate_scope_key: str = Field(pattern=_KEY_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_region_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_GROUP_MEMBERS,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_scope(self) -> D2CompiledTableLogicalScopeV1:
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("logical scope region IDs must be unique")
        if self.reason_codes != tuple(sorted(set(self.reason_codes))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in self.reason_codes
        ):
            raise ValueError("logical scope reason codes must be canonical")
        expected_id = stable_d2_table_logical_scope_id(
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            ordered_region_ids=self.ordered_region_ids,
        )
        if self.logical_scope_id != expected_id:
            raise ValueError("logical scope ID must bind its ordered regions")
        expected_sha = stable_d2_table_logical_scope_sha256(
            logical_scope_id=self.logical_scope_id,
            candidate_scope_key=self.candidate_scope_key,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            kit_bundle_sha256=self.kit_bundle_sha256,
            ordered_region_ids=self.ordered_region_ids,
            reason_codes=self.reason_codes,
        )
        if self.scope_sha256 != expected_sha:
            raise ValueError("logical scope SHA must bind its topology")
        return self


class D2CompiledTableLogicalObjectV1(_StrictFrozenModel):
    logical_object_id: str = Field(pattern=r"^d2-table-logical-object-[0-9a-f]{32}$")
    candidate_object_key: str = Field(pattern=_KEY_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_region_ids: tuple[str, ...] = Field(
        min_length=1,
        max_length=_MAX_GROUP_MEMBERS,
    )
    object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_object(self) -> D2CompiledTableLogicalObjectV1:
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("logical object region IDs must be unique")
        expected_id = stable_d2_table_logical_object_id(
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            ordered_region_ids=self.ordered_region_ids,
        )
        if self.logical_object_id != expected_id:
            raise ValueError("logical object ID must bind its ordered regions")
        expected_sha = stable_d2_table_logical_object_sha256(
            logical_object_id=self.logical_object_id,
            candidate_object_key=self.candidate_object_key,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            kit_bundle_sha256=self.kit_bundle_sha256,
            ordered_region_ids=self.ordered_region_ids,
        )
        if self.object_sha256 != expected_sha:
            raise ValueError("logical object SHA must bind its topology")
        return self


class D2TableSupplementalEvidenceRefV1(_StrictFrozenModel):
    """A content-free binding outside the TableIR cell anchor domain."""

    evidence_kind: D2TableSupplementalEvidenceKind
    evidence_ref: str = Field(min_length=1, max_length=240)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("evidence_ref")
    @classmethod
    def reject_blank_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("supplemental evidence refs must not be blank")
        return value


class D2PhysicalTableCellCoverageV1(_StrictFrozenModel):
    """Exact cover of TableIR cell anchors, never a full-source-text claim."""

    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_node_raw_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_notes_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    supplemental_visual_evidence: tuple[D2TableSupplementalEvidenceRefV1, ...] = Field(
        default=(), max_length=_MAX_SUPPLEMENTAL_EVIDENCE
    )
    coverage_scope: Literal["TABLE_IR_CELL_ANCHORS_ONLY"] = "TABLE_IR_CELL_ANCHORS_ONLY"
    supplemental_text_scope: Literal["HASH_BOUND_NOT_CELL_DISPOSITIONED"] = (
        "HASH_BOUND_NOT_CELL_DISPOSITIONED"
    )
    canonical_cell_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_TABLE_CELLS,
    )
    ordered_region_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS_PER_TABLE,
    )
    empty_table_disposition: D2EmptyPhysicalTableDispositionKind | None = None
    empty_table_reason_codes: tuple[str, ...] = Field(default=(), max_length=16)
    host_auto_filled_gap: StrictBool
    cell_coverage_complete: Literal[True] = True
    cell_coverage_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_coverage(self) -> D2PhysicalTableCellCoverageV1:
        if len(self.canonical_cell_ids) != len(set(self.canonical_cell_ids)):
            raise ValueError("physical table canonical cell IDs must be unique")
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("physical table region IDs must be unique")
        if self.canonical_cell_ids:
            if not self.ordered_region_ids:
                raise ValueError("non-empty physical tables require cell regions")
            if (
                self.empty_table_disposition is not None
                or self.empty_table_reason_codes
            ):
                raise ValueError(
                    "empty-table disposition fields are valid only without TableIR cells"
                )
        else:
            if self.ordered_region_ids:
                raise ValueError("empty physical tables cannot carry cell regions")
            if (
                self.empty_table_disposition is None
                or not self.empty_table_reason_codes
            ):
                raise ValueError(
                    "empty physical tables require an explicit disposition"
                )
        if self.empty_table_reason_codes != tuple(
            sorted(set(self.empty_table_reason_codes))
        ) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None
            for item in self.empty_table_reason_codes
        ):
            raise ValueError("empty-table reason codes must be canonical")
        supplemental_keys = tuple(
            (item.evidence_kind.value, item.evidence_ref)
            for item in self.supplemental_visual_evidence
        )
        if supplemental_keys != tuple(sorted(set(supplemental_keys))):
            raise ValueError(
                "supplemental visual evidence must be unique and canonical"
            )
        if self.host_auto_filled_gap and (
            self.empty_table_disposition
            not in {None, D2EmptyPhysicalTableDispositionKind.UNRESOLVED}
        ):
            raise ValueError("Host auto-fill may not mark an empty table false")
        expected = stable_d2_physical_table_cell_coverage_sha256(
            physical_table_id=self.physical_table_id,
            table_source_node_id=self.table_source_node_id,
            physical_table_source_order=self.physical_table_source_order,
            table_ir_sha256=self.table_ir_sha256,
            table_node_raw_text_sha256=self.table_node_raw_text_sha256,
            table_notes_sha256=self.table_notes_sha256,
            supplemental_visual_evidence=self.supplemental_visual_evidence,
            coverage_scope=self.coverage_scope,
            supplemental_text_scope=self.supplemental_text_scope,
            canonical_cell_ids=self.canonical_cell_ids,
            ordered_region_ids=self.ordered_region_ids,
            empty_table_disposition=self.empty_table_disposition,
            empty_table_reason_codes=self.empty_table_reason_codes,
            host_auto_filled_gap=self.host_auto_filled_gap,
        )
        if self.cell_coverage_sha256 != expected:
            raise ValueError(
                "physical table cell coverage SHA must bind exact evidence"
            )
        return self


class D2TableRegionArtifactV1(_StrictFrozenModel):
    """Host-compiled, content-free region topology with exact cell evidence."""

    schema_version: Literal["d2-table-region-artifact/v1"] = (
        D2_TABLE_REGION_ARTIFACT_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_status: SourceIRStatus
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    gap_policy: D2TableRegionGapPolicy
    status: D2TableRegionArtifactStatus
    physical_table_cell_coverage: tuple[D2PhysicalTableCellCoverageV1, ...] = Field(
        default=(),
        max_length=_MAX_PHYSICAL_TABLES,
    )
    regions: tuple[D2CompiledTableRegionV1, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    logical_scopes: tuple[D2CompiledTableLogicalScopeV1, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    logical_objects: tuple[D2CompiledTableLogicalObjectV1, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    unresolved_region_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    host_auto_filled_region_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_REGIONS,
    )
    unresolved_empty_table_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_PHYSICAL_TABLES,
    )
    host_auto_filled_empty_table_ids: tuple[str, ...] = Field(
        default=(),
        max_length=_MAX_PHYSICAL_TABLES,
    )
    artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_artifact(self) -> D2TableRegionArtifactV1:
        if self.source_ir_status is SourceIRStatus.FAILED:
            raise ValueError("failed SourceIR cannot produce table-region topology")
        if tuple(
            table.physical_table_source_order
            for table in self.physical_table_cell_coverage
        ) != tuple(
            sorted(
                table.physical_table_source_order
                for table in self.physical_table_cell_coverage
            )
        ):
            raise ValueError("physical table cell coverage must follow source order")
        if len(
            {table.physical_table_id for table in self.physical_table_cell_coverage}
        ) != len(self.physical_table_cell_coverage):
            raise ValueError("physical table cell coverage IDs must be unique")

        regions_by_id = {region.region_id: region for region in self.regions}
        if len(regions_by_id) != len(self.regions):
            raise ValueError("compiled region IDs must be unique")
        if any(
            region.source_ir_id != self.source_ir_id
            or region.source_sha256 != self.source_sha256
            or region.source_ir_canonical_sha256 != self.source_ir_canonical_sha256
            or region.kit_bundle_sha256 != self.kit_bundle_sha256
            for region in self.regions
        ):
            raise ValueError(
                "compiled region revision/Kit lineage differs from artifact"
            )
        if any(
            scope.source_ir_canonical_sha256 != self.source_ir_canonical_sha256
            or scope.kit_bundle_sha256 != self.kit_bundle_sha256
            for scope in self.logical_scopes
        ) or any(
            logical_object.source_ir_canonical_sha256 != self.source_ir_canonical_sha256
            or logical_object.kit_bundle_sha256 != self.kit_bundle_sha256
            for logical_object in self.logical_objects
        ):
            raise ValueError("compiled logical topology lineage differs from artifact")
        expected_region_order = tuple(
            sorted(
                self.regions,
                key=lambda region: (
                    region.physical_table_source_order,
                    region.cell_spans[0].start_cell_ordinal,
                    tuple(
                        (span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
                        for span in region.cell_spans
                    ),
                    region.region_id,
                ),
            )
        )
        if self.regions != expected_region_order:
            raise ValueError("compiled regions must follow canonical source-cell order")

        covered_region_ids: list[str] = []
        for table in self.physical_table_cell_coverage:
            table_regions = tuple(
                region
                for region in self.regions
                if region.physical_table_id == table.physical_table_id
            )
            if (
                tuple(region.region_id for region in table_regions)
                != table.ordered_region_ids
            ):
                raise ValueError("cell coverage region order must match artifact order")
            refs = sorted(
                (ref for region in table_regions for ref in region.cell_refs),
                key=lambda ref: ref.canonical_cell_ordinal,
            )
            if tuple(ref.canonical_cell_ordinal for ref in refs) != tuple(
                range(len(table.canonical_cell_ids))
            ):
                raise ValueError(
                    "every physical TableIR cell must be covered exactly once"
                )
            if tuple(ref.cell_id for ref in refs) != table.canonical_cell_ids:
                raise ValueError(
                    "physical cell coverage IDs differ from region evidence"
                )
            if table_regions and any(
                region.table_source_node_id != table.table_source_node_id
                or region.physical_table_source_order
                != table.physical_table_source_order
                or region.cell_refs[0].table_ir_sha256 != table.table_ir_sha256
                for region in table_regions
            ):
                raise ValueError(
                    "physical cell coverage source binding differs from its regions"
                )
            expected_auto_fill = any(
                region.host_auto_filled for region in table_regions
            ) or (
                table.empty_table_disposition
                is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
                and table.physical_table_id in self.host_auto_filled_empty_table_ids
            )
            if table.host_auto_filled_gap != expected_auto_fill:
                raise ValueError("physical cell auto-fill marker is inconsistent")
            _require_region_row_summaries(table_regions)
            covered_region_ids.extend(table.ordered_region_ids)
        if tuple(covered_region_ids) != tuple(
            region.region_id for region in self.regions
        ):
            raise ValueError("every compiled region requires one physical-table owner")

        scopes_by_id = {scope.logical_scope_id: scope for scope in self.logical_scopes}
        objects_by_id = {
            logical_object.logical_object_id: logical_object
            for logical_object in self.logical_objects
        }
        for region in self.regions:
            if region.logical_scope_id is not None:
                scope = scopes_by_id.get(region.logical_scope_id)
                if scope is None or region.region_id not in scope.ordered_region_ids:
                    raise ValueError(
                        "scope region must bind one compiled logical scope"
                    )
            if region.logical_object_id is not None:
                logical_object = objects_by_id.get(region.logical_object_id)
                if (
                    logical_object is None
                    or region.region_id not in logical_object.ordered_region_ids
                ):
                    raise ValueError(
                        "object region must bind one compiled logical object"
                    )
            if region.nested_owner_region_id is not None:
                if (
                    region.nested_owner_region_id not in regions_by_id
                    or region.nested_owner_region_id == region.region_id
                ):
                    raise ValueError("nested owner must be an existing other region")
        _require_compiled_nested_business_owners(regions_by_id)

        for scope in self.logical_scopes:
            members = tuple(
                region.region_id
                for region in self.regions
                if region.logical_scope_id == scope.logical_scope_id
            )
            if members != scope.ordered_region_ids:
                raise ValueError("logical scope must exactly own its ordered regions")
        for logical_object in self.logical_objects:
            members = tuple(
                region.region_id
                for region in self.regions
                if region.logical_object_id == logical_object.logical_object_id
            )
            if members != logical_object.ordered_region_ids:
                raise ValueError("logical object must exactly own its ordered regions")

        unresolved = tuple(
            region.region_id
            for region in self.regions
            if region.disposition is D2TableRegionDispositionKind.UNRESOLVED
        )
        auto_filled = tuple(
            region.region_id for region in self.regions if region.host_auto_filled
        )
        unresolved_empty_tables = tuple(
            table.physical_table_id
            for table in self.physical_table_cell_coverage
            if table.empty_table_disposition
            is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
        )
        auto_filled_empty_tables = tuple(
            table.physical_table_id
            for table in self.physical_table_cell_coverage
            if table.host_auto_filled_gap and not table.canonical_cell_ids
        )
        if self.unresolved_region_ids != unresolved:
            raise ValueError("unresolved region ledger must be exact and canonical")
        if self.host_auto_filled_region_ids != auto_filled:
            raise ValueError("Host auto-fill region ledger must be exact and canonical")
        if self.unresolved_empty_table_ids != unresolved_empty_tables:
            raise ValueError(
                "unresolved empty-table ledger must be exact and canonical"
            )
        if self.host_auto_filled_empty_table_ids != auto_filled_empty_tables:
            raise ValueError(
                "Host auto-filled empty-table ledger must be exact and canonical"
            )
        expected_status = (
            D2TableRegionArtifactStatus.PARTIAL
            if self.source_ir_status is SourceIRStatus.PARTIAL
            or unresolved
            or unresolved_empty_tables
            else D2TableRegionArtifactStatus.COMPLETE
        )
        if self.status is not expected_status:
            raise ValueError(
                "artifact status must reflect source and unresolved regions"
            )

        expected_id = stable_d2_table_region_artifact_id(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            source_ir_status=self.source_ir_status,
            kit_bundle_sha256=self.kit_bundle_sha256,
            d1_table_map_artifact_id=self.d1_table_map_artifact_id,
            d1_table_map_artifact_sha256=self.d1_table_map_artifact_sha256,
            gap_policy=self.gap_policy,
            status=self.status,
            physical_table_cell_coverage_sha256s=tuple(
                table.cell_coverage_sha256
                for table in self.physical_table_cell_coverage
            ),
            region_sha256s=tuple(region.region_sha256 for region in self.regions),
            logical_scope_sha256s=tuple(
                scope.scope_sha256 for scope in self.logical_scopes
            ),
            logical_object_sha256s=tuple(
                logical_object.object_sha256 for logical_object in self.logical_objects
            ),
            unresolved_region_ids=self.unresolved_region_ids,
            host_auto_filled_region_ids=self.host_auto_filled_region_ids,
            unresolved_empty_table_ids=self.unresolved_empty_table_ids,
            host_auto_filled_empty_table_ids=self.host_auto_filled_empty_table_ids,
        )
        if self.artifact_id != expected_id:
            raise ValueError("artifact ID must bind its source and compiled topology")
        expected_sha = stable_d2_table_region_artifact_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            source_ir_status=self.source_ir_status,
            kit_bundle_sha256=self.kit_bundle_sha256,
            d1_table_map_artifact_id=self.d1_table_map_artifact_id,
            d1_table_map_artifact_sha256=self.d1_table_map_artifact_sha256,
            gap_policy=self.gap_policy,
            status=self.status,
            physical_table_cell_coverage=self.physical_table_cell_coverage,
            regions=self.regions,
            logical_scopes=self.logical_scopes,
            logical_objects=self.logical_objects,
            unresolved_region_ids=self.unresolved_region_ids,
            host_auto_filled_region_ids=self.host_auto_filled_region_ids,
            unresolved_empty_table_ids=self.unresolved_empty_table_ids,
            host_auto_filled_empty_table_ids=self.host_auto_filled_empty_table_ids,
            artifact_id=self.artifact_id,
        )
        if self.artifact_sha256 != expected_sha:
            raise ValueError("artifact SHA must bind the complete compiled result")
        return self


@dataclass(frozen=True)
class _PhysicalTable:
    node: SourceNode
    table: TableIR
    table_ir_sha256: str
    cells: tuple[TableCellIR, ...]


@dataclass(frozen=True)
class _RegionWork:
    region_key: str
    physical_table_id: str
    cell_spans: tuple[D2PhysicalTableCellOrdinalSpanV1, ...]
    disposition: D2TableRegionDispositionKind
    reason_codes: tuple[str, ...]
    logical_scope_key: str | None = None
    logical_object_key: str | None = None
    nested_owner_region_key: str | None = None
    host_auto_filled: bool = False


def compile_d2_table_regions(
    source_ir: SourceIRSnapshot,
    candidate: D2TableRegionCandidateArtifactV1,
    *,
    expected_kit_bundle_sha256: str,
    expected_d1_table_map_artifact_id: str,
    expected_d1_table_map_artifact_sha256: str,
    gap_policy: D2TableRegionGapPolicy = D2TableRegionGapPolicy.REJECT,
) -> D2TableRegionArtifactV1:
    """Compile runner ordinals into exact SourceIR cells and stable topology.

    ``AUTO_FILL_UNRESOLVED`` creates one deterministic Host region per physical
    table containing all uncovered ordinals as maximally merged spans. It never
    guesses a logical owner and therefore always makes the artifact ``PARTIAL``.
    ``REJECT`` is the fail-closed default.

    The Host canonicalizes the outer region/scope collection order for stable
    replay. It rejects reordered spans and reordered members inside a logical
    scope because those sequences carry source-topology meaning.
    """

    _require_sha256(expected_kit_bundle_sha256, "expected_kit_bundle_sha256")
    if not expected_d1_table_map_artifact_id.strip():
        raise ValueError("expected D1 table-map artifact ID must not be blank")
    _require_sha256(
        expected_d1_table_map_artifact_sha256,
        "expected_d1_table_map_artifact_sha256",
    )
    source_canonical_sha256 = d2_table_region_source_ir_canonical_sha256(source_ir)
    if (
        candidate.source_ir_id != source_ir.source_ir_id
        or candidate.source_sha256 != source_ir.source_sha256
        or candidate.source_ir_canonical_sha256 != source_canonical_sha256
    ):
        raise ValueError("table-region candidate SourceIR identity is stale or drifted")
    if candidate.kit_bundle_sha256 != expected_kit_bundle_sha256:
        raise ValueError(
            "table-region candidate Agent Kit identity is stale or drifted"
        )
    if (
        candidate.d1_table_map_artifact_id != expected_d1_table_map_artifact_id
        or candidate.d1_table_map_artifact_sha256
        != expected_d1_table_map_artifact_sha256
    ):
        raise ValueError(
            "table-region candidate D1 table-map lineage is stale or drifted"
        )
    if source_ir.status is SourceIRStatus.FAILED:
        raise ValueError("failed SourceIR cannot be compiled into table regions")

    tables = _physical_table_catalog(source_ir)
    tables_by_id = {table.table.table_id: table for table in tables}
    empty_disposition_candidates = {
        item.physical_table_id: item for item in candidate.empty_table_dispositions
    }
    unknown_empty_table_ids = set(empty_disposition_candidates) - set(tables_by_id)
    if unknown_empty_table_ids:
        raise ValueError(
            "empty-table disposition references an unknown physical TableIR"
        )
    empty_dispositions: dict[
        str, tuple[D2EmptyPhysicalTableDispositionKind, tuple[str, ...], bool]
    ] = {}
    for physical in tables:
        table_id = physical.table.table_id
        declared = empty_disposition_candidates.get(table_id)
        if physical.cells:
            if declared is not None:
                raise ValueError(
                    "empty-table disposition cannot target a physical TableIR with cells"
                )
            continue
        if declared is not None:
            empty_dispositions[table_id] = (
                declared.disposition,
                declared.reason_codes,
                False,
            )
            continue
        if gap_policy is D2TableRegionGapPolicy.REJECT:
            raise ValueError(
                "empty physical TableIR requires an explicit false/unresolved "
                "disposition or AUTO_FILL_UNRESOLVED"
            )
        empty_dispositions[table_id] = (
            D2EmptyPhysicalTableDispositionKind.UNRESOLVED,
            ("host_autofilled_empty_table_without_cells",),
            True,
        )
    work_items = [_candidate_to_work(region) for region in candidate.regions]
    candidate_region_keys = {item.region_key for item in work_items}

    covered_ordinals: dict[str, set[int]] = {
        table.table.table_id: set() for table in tables
    }
    for item in work_items:
        physical = tables_by_id.get(item.physical_table_id)
        if physical is None:
            raise ValueError("candidate references an unknown physical TableIR")
        if not physical.cells:
            raise ValueError("cell regions cannot target an empty physical TableIR")
        for span in item.cell_spans:
            if span.end_cell_ordinal_exclusive > len(physical.cells):
                raise ValueError("candidate cell ordinal span exceeds physical TableIR")
            ordinals = set(
                range(span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
            )
            if covered_ordinals[item.physical_table_id].intersection(ordinals):
                raise ValueError(
                    "physical TableIR cells cannot belong to overlapping regions"
                )
            covered_ordinals[item.physical_table_id].update(ordinals)

    for physical in tables:
        table_id = physical.table.table_id
        if not physical.cells:
            continue
        missing = tuple(
            sorted(set(range(len(physical.cells))) - covered_ordinals[table_id])
        )
        if not missing:
            continue
        if gap_policy is D2TableRegionGapPolicy.REJECT:
            raise ValueError("candidate must disposition every physical TableIR cell")
        generated_key = _stable_id(
            _HOST_REGION_KEY_PREFIX.rstrip("-"),
            source_canonical_sha256,
            table_id,
            missing,
        )
        if generated_key in candidate_region_keys:
            raise ValueError("candidate collided with a Host-reserved region key")
        if len(work_items) >= _MAX_REGIONS:
            raise ValueError("Host unresolved auto-fill would exceed the region budget")
        work_items.append(
            _RegionWork(
                region_key=generated_key,
                physical_table_id=table_id,
                cell_spans=_ordinals_to_spans(missing),
                disposition=D2TableRegionDispositionKind.UNRESOLVED,
                reason_codes=("host_autofilled_candidate_gap",),
                host_auto_filled=True,
            )
        )
        covered_ordinals[table_id].update(missing)

    table_rank = {
        physical.table.table_id: physical.node.source_order for physical in tables
    }
    work_items.sort(key=lambda item: _region_work_sort_key(item, table_rank))
    work_rank = {item.region_key: index for index, item in enumerate(work_items)}

    for scope in candidate.logical_scopes:
        expected_keys = tuple(
            item.region_key
            for item in work_items
            if item.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART
            and item.logical_scope_key == scope.logical_scope_key
        )
        if scope.ordered_region_keys != expected_keys:
            raise ValueError(
                "logical scope region keys must follow canonical physical-cell order"
            )

    region_id_by_key: dict[str, str] = {}
    cell_refs_by_key: dict[str, tuple[D2TableCellSourceRefV1, ...]] = {}
    for item in work_items:
        physical = tables_by_id[item.physical_table_id]
        refs = tuple(
            _compile_cell_ref(
                source_ir=source_ir,
                source_canonical_sha256=source_canonical_sha256,
                physical=physical,
                ordinal=ordinal,
            )
            for span in item.cell_spans
            for ordinal in range(
                span.start_cell_ordinal,
                span.end_cell_ordinal_exclusive,
            )
        )
        cell_refs_by_key[item.region_key] = refs
        region_id_by_key[item.region_key] = stable_d2_table_region_id(
            physical_table_id=item.physical_table_id,
            table_source_node_id=physical.node.source_node_id,
            table_ir_sha256=physical.table_ir_sha256,
            cell_local_evidence_sha256s=tuple(
                stable_d2_table_cell_local_evidence_sha256(ref) for ref in refs
            ),
        )

    scope_id_by_key: dict[str, str] = {}
    scope_candidate_by_key = {
        scope.logical_scope_key: scope for scope in candidate.logical_scopes
    }
    for scope in candidate.logical_scopes:
        scope_id_by_key[scope.logical_scope_key] = stable_d2_table_logical_scope_id(
            source_ir_canonical_sha256=source_canonical_sha256,
            ordered_region_ids=tuple(
                region_id_by_key[key] for key in scope.ordered_region_keys
            ),
        )

    object_keys = sorted(
        {
            item.logical_object_key
            for item in work_items
            if item.logical_object_key is not None
        },
        key=lambda key: min(
            work_rank[item.region_key]
            for item in work_items
            if item.logical_object_key == key
        ),
    )
    object_region_keys: dict[str, tuple[str, ...]] = {
        key: tuple(
            item.region_key for item in work_items if item.logical_object_key == key
        )
        for key in object_keys
    }
    object_id_by_key = {
        key: stable_d2_table_logical_object_id(
            source_ir_canonical_sha256=source_canonical_sha256,
            ordered_region_ids=tuple(
                region_id_by_key[region_key] for region_key in object_region_keys[key]
            ),
        )
        for key in object_keys
    }

    regions: list[D2CompiledTableRegionV1] = []
    for item in work_items:
        physical = tables_by_id[item.physical_table_id]
        logical_scope_id = (
            scope_id_by_key[item.logical_scope_key]
            if item.logical_scope_key is not None
            else None
        )
        logical_object_id = (
            object_id_by_key[item.logical_object_key]
            if item.logical_object_key is not None
            else None
        )
        nested_owner_region_id = (
            region_id_by_key[item.nested_owner_region_key]
            if item.nested_owner_region_key is not None
            else None
        )
        refs = cell_refs_by_key[item.region_key]
        region_id = region_id_by_key[item.region_key]
        (
            covered_row_indices,
            fully_covered_row_indices,
            partially_covered_row_indices,
            row_alignment,
            row_materializer_eligibility,
        ) = _derive_region_row_summary(
            physical=physical,
            selected_refs=refs,
            disposition=item.disposition,
        )
        region_sha256 = stable_d2_compiled_table_region_sha256(
            region_id=region_id,
            candidate_region_key=item.region_key,
            source_ir_id=source_ir.source_ir_id,
            source_sha256=source_ir.source_sha256,
            source_ir_canonical_sha256=source_canonical_sha256,
            kit_bundle_sha256=expected_kit_bundle_sha256,
            physical_table_id=item.physical_table_id,
            table_source_node_id=physical.node.source_node_id,
            physical_table_source_order=physical.node.source_order,
            cell_spans=item.cell_spans,
            cell_refs=refs,
            disposition=item.disposition,
            logical_scope_id=logical_scope_id,
            logical_object_id=logical_object_id,
            nested_owner_region_id=nested_owner_region_id,
            reason_codes=item.reason_codes,
            host_auto_filled=item.host_auto_filled,
            covered_row_indices=covered_row_indices,
            fully_covered_row_indices=fully_covered_row_indices,
            partially_covered_row_indices=partially_covered_row_indices,
            row_alignment=row_alignment,
            row_materializer_eligibility=row_materializer_eligibility,
        )
        regions.append(
            D2CompiledTableRegionV1(
                region_id=region_id,
                candidate_region_key=item.region_key,
                source_ir_id=source_ir.source_ir_id,
                source_sha256=source_ir.source_sha256,
                source_ir_canonical_sha256=source_canonical_sha256,
                kit_bundle_sha256=expected_kit_bundle_sha256,
                physical_table_id=item.physical_table_id,
                table_source_node_id=physical.node.source_node_id,
                physical_table_source_order=physical.node.source_order,
                cell_spans=item.cell_spans,
                cell_refs=refs,
                disposition=item.disposition,
                logical_scope_id=logical_scope_id,
                logical_object_id=logical_object_id,
                nested_owner_region_id=nested_owner_region_id,
                reason_codes=item.reason_codes,
                host_auto_filled=item.host_auto_filled,
                covered_row_indices=covered_row_indices,
                fully_covered_row_indices=fully_covered_row_indices,
                partially_covered_row_indices=partially_covered_row_indices,
                row_alignment=row_alignment,
                row_materializer_eligibility=row_materializer_eligibility,
                region_sha256=region_sha256,
            )
        )
    compiled_regions = tuple(regions)

    logical_scopes: list[D2CompiledTableLogicalScopeV1] = []
    for scope_key in sorted(
        scope_id_by_key,
        key=lambda key: min(
            work_rank[region_key]
            for region_key in scope_candidate_by_key[key].ordered_region_keys
        ),
    ):
        scope_candidate = scope_candidate_by_key[scope_key]
        ordered_region_ids = tuple(
            region_id_by_key[key] for key in scope_candidate.ordered_region_keys
        )
        scope_id = scope_id_by_key[scope_key]
        scope_sha256 = stable_d2_table_logical_scope_sha256(
            logical_scope_id=scope_id,
            candidate_scope_key=scope_key,
            source_ir_canonical_sha256=source_canonical_sha256,
            kit_bundle_sha256=expected_kit_bundle_sha256,
            ordered_region_ids=ordered_region_ids,
            reason_codes=scope_candidate.reason_codes,
        )
        logical_scopes.append(
            D2CompiledTableLogicalScopeV1(
                logical_scope_id=scope_id,
                candidate_scope_key=scope_key,
                source_ir_canonical_sha256=source_canonical_sha256,
                kit_bundle_sha256=expected_kit_bundle_sha256,
                ordered_region_ids=ordered_region_ids,
                reason_codes=scope_candidate.reason_codes,
                scope_sha256=scope_sha256,
            )
        )

    logical_objects: list[D2CompiledTableLogicalObjectV1] = []
    for object_key in object_keys:
        ordered_region_ids = tuple(
            region_id_by_key[key] for key in object_region_keys[object_key]
        )
        logical_object_id = object_id_by_key[object_key]
        object_sha256 = stable_d2_table_logical_object_sha256(
            logical_object_id=logical_object_id,
            candidate_object_key=object_key,
            source_ir_canonical_sha256=source_canonical_sha256,
            kit_bundle_sha256=expected_kit_bundle_sha256,
            ordered_region_ids=ordered_region_ids,
        )
        logical_objects.append(
            D2CompiledTableLogicalObjectV1(
                logical_object_id=logical_object_id,
                candidate_object_key=object_key,
                source_ir_canonical_sha256=source_canonical_sha256,
                kit_bundle_sha256=expected_kit_bundle_sha256,
                ordered_region_ids=ordered_region_ids,
                object_sha256=object_sha256,
            )
        )

    physical_table_cell_coverage: list[D2PhysicalTableCellCoverageV1] = []
    for physical in tables:
        table_regions = tuple(
            region
            for region in compiled_regions
            if region.physical_table_id == physical.table.table_id
        )
        canonical_cell_ids = tuple(cell.cell_id for cell in physical.cells)
        ordered_region_ids = tuple(region.region_id for region in table_regions)
        empty_disposition = empty_dispositions.get(physical.table.table_id)
        empty_table_disposition = (
            empty_disposition[0] if empty_disposition is not None else None
        )
        empty_table_reason_codes = (
            empty_disposition[1] if empty_disposition is not None else ()
        )
        host_auto_filled_gap = any(
            region.host_auto_filled for region in table_regions
        ) or (empty_disposition is not None and empty_disposition[2])
        supplemental_visual_evidence = _table_supplemental_visual_evidence(
            source_ir=source_ir,
            physical=physical,
        )
        coverage_values = {
            "physical_table_id": physical.table.table_id,
            "table_source_node_id": physical.node.source_node_id,
            "physical_table_source_order": physical.node.source_order,
            "table_ir_sha256": physical.table_ir_sha256,
            "table_node_raw_text_sha256": _text_sha256(physical.node.raw_text),
            "table_notes_sha256": _canonical_sha256(physical.table.notes),
            "supplemental_visual_evidence": supplemental_visual_evidence,
            "coverage_scope": "TABLE_IR_CELL_ANCHORS_ONLY",
            "supplemental_text_scope": "HASH_BOUND_NOT_CELL_DISPOSITIONED",
            "canonical_cell_ids": canonical_cell_ids,
            "ordered_region_ids": ordered_region_ids,
            "empty_table_disposition": empty_table_disposition,
            "empty_table_reason_codes": empty_table_reason_codes,
            "host_auto_filled_gap": host_auto_filled_gap,
        }
        cell_coverage_sha256 = stable_d2_physical_table_cell_coverage_sha256(
            **coverage_values
        )
        physical_table_cell_coverage.append(
            D2PhysicalTableCellCoverageV1(
                **coverage_values,
                cell_coverage_sha256=cell_coverage_sha256,
            )
        )

    compiled_scopes = tuple(logical_scopes)
    compiled_objects = tuple(logical_objects)
    compiled_cell_coverage = tuple(physical_table_cell_coverage)
    unresolved_region_ids = tuple(
        region.region_id
        for region in compiled_regions
        if region.disposition is D2TableRegionDispositionKind.UNRESOLVED
    )
    host_auto_filled_region_ids = tuple(
        region.region_id for region in compiled_regions if region.host_auto_filled
    )
    unresolved_empty_table_ids = tuple(
        table.physical_table_id
        for table in compiled_cell_coverage
        if table.empty_table_disposition
        is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
    )
    host_auto_filled_empty_table_ids = tuple(
        table.physical_table_id
        for table in compiled_cell_coverage
        if table.host_auto_filled_gap and not table.canonical_cell_ids
    )
    status = (
        D2TableRegionArtifactStatus.PARTIAL
        if source_ir.status is SourceIRStatus.PARTIAL
        or unresolved_region_ids
        or unresolved_empty_table_ids
        else D2TableRegionArtifactStatus.COMPLETE
    )
    artifact_id = stable_d2_table_region_artifact_id(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_canonical_sha256,
        source_ir_status=source_ir.status,
        kit_bundle_sha256=expected_kit_bundle_sha256,
        d1_table_map_artifact_id=expected_d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=expected_d1_table_map_artifact_sha256,
        gap_policy=gap_policy,
        status=status,
        physical_table_cell_coverage_sha256s=tuple(
            table.cell_coverage_sha256 for table in compiled_cell_coverage
        ),
        region_sha256s=tuple(region.region_sha256 for region in compiled_regions),
        logical_scope_sha256s=tuple(scope.scope_sha256 for scope in compiled_scopes),
        logical_object_sha256s=tuple(
            logical_object.object_sha256 for logical_object in compiled_objects
        ),
        unresolved_region_ids=unresolved_region_ids,
        host_auto_filled_region_ids=host_auto_filled_region_ids,
        unresolved_empty_table_ids=unresolved_empty_table_ids,
        host_auto_filled_empty_table_ids=host_auto_filled_empty_table_ids,
    )
    artifact_sha256 = stable_d2_table_region_artifact_sha256(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_canonical_sha256,
        source_ir_status=source_ir.status,
        kit_bundle_sha256=expected_kit_bundle_sha256,
        d1_table_map_artifact_id=expected_d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=expected_d1_table_map_artifact_sha256,
        gap_policy=gap_policy,
        status=status,
        physical_table_cell_coverage=compiled_cell_coverage,
        regions=compiled_regions,
        logical_scopes=compiled_scopes,
        logical_objects=compiled_objects,
        unresolved_region_ids=unresolved_region_ids,
        host_auto_filled_region_ids=host_auto_filled_region_ids,
        unresolved_empty_table_ids=unresolved_empty_table_ids,
        host_auto_filled_empty_table_ids=host_auto_filled_empty_table_ids,
        artifact_id=artifact_id,
    )
    return D2TableRegionArtifactV1(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_canonical_sha256,
        source_ir_status=source_ir.status,
        kit_bundle_sha256=expected_kit_bundle_sha256,
        d1_table_map_artifact_id=expected_d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=expected_d1_table_map_artifact_sha256,
        gap_policy=gap_policy,
        status=status,
        physical_table_cell_coverage=compiled_cell_coverage,
        regions=compiled_regions,
        logical_scopes=compiled_scopes,
        logical_objects=compiled_objects,
        unresolved_region_ids=unresolved_region_ids,
        host_auto_filled_region_ids=host_auto_filled_region_ids,
        unresolved_empty_table_ids=unresolved_empty_table_ids,
        host_auto_filled_empty_table_ids=host_auto_filled_empty_table_ids,
        artifact_id=artifact_id,
        artifact_sha256=artifact_sha256,
    )


def d2_table_region_source_ir_canonical_sha256(source_ir: SourceIRSnapshot) -> str:
    return _canonical_sha256(source_ir.model_dump(mode="json", exclude={"created_at"}))


def stable_d2_table_cell_evidence_sha256(**values: object) -> str:
    return _canonical_sha256({"schema_version": "d2-table-cell-evidence/v1", **values})


def stable_d2_table_region_id(
    *,
    physical_table_id: str,
    table_source_node_id: str,
    table_ir_sha256: str,
    cell_local_evidence_sha256s: tuple[str, ...],
) -> str:
    return _stable_id(
        "d2-table-region",
        physical_table_id,
        table_source_node_id,
        table_ir_sha256,
        cell_local_evidence_sha256s,
    )


def stable_d2_table_cell_local_evidence_sha256(
    ref: D2TableCellSourceRefV1,
) -> str:
    """Bind only the physical table/cell fact, not the whole SourceIR revision."""

    return _canonical_sha256(
        {
            "schema_version": "d2-table-cell-local-evidence/v1",
            "physical_table_id": ref.physical_table_id,
            "table_source_node_id": ref.table_source_node_id,
            "table_ir_sha256": ref.table_ir_sha256,
            "canonical_cell_ordinal": ref.canonical_cell_ordinal,
            "cell_id": ref.cell_id,
            "row": ref.row,
            "column": ref.column,
            "row_span": ref.row_span,
            "column_span": ref.column_span,
            "page_number": ref.page_number,
            "bbox": ref.bbox,
            "native_locator_sha256": ref.native_locator_sha256,
            "raw_text_sha256": ref.raw_text_sha256,
            "formula_sha256": ref.formula_sha256,
            "display_value_sha256": ref.display_value_sha256,
        }
    )


def stable_d2_compiled_table_region_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-compiled-table-region/v1",
            **_json_values(values),
        }
    )


def stable_d2_table_logical_scope_id(
    *,
    source_ir_canonical_sha256: str,
    ordered_region_ids: tuple[str, ...],
) -> str:
    return _stable_id(
        "d2-table-logical-scope",
        source_ir_canonical_sha256,
        ordered_region_ids,
    )


def stable_d2_table_logical_scope_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-logical-scope/v1", **_json_values(values)}
    )


def stable_d2_table_logical_object_id(
    *,
    source_ir_canonical_sha256: str,
    ordered_region_ids: tuple[str, ...],
) -> str:
    return _stable_id(
        "d2-table-logical-object",
        source_ir_canonical_sha256,
        ordered_region_ids,
    )


def stable_d2_table_logical_object_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-logical-object/v1", **_json_values(values)}
    )


def stable_d2_physical_table_cell_coverage_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": "d2-physical-table-cell-coverage/v1",
            **_json_values(values),
        }
    )


def stable_d2_table_region_artifact_id(**values: object) -> str:
    return _stable_id("d2-table-region-artifact", _json_values(values))


def stable_d2_table_region_artifact_sha256(**values: object) -> str:
    return _canonical_sha256(
        {
            "schema_version": D2_TABLE_REGION_ARTIFACT_SCHEMA_VERSION,
            **_json_values(values),
        }
    )


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> tuple[_PhysicalTable, ...]:
    result: list[_PhysicalTable] = []
    seen_table_ids: set[str] = set()
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE:
            continue
        table = node.table_payload
        if table is None:
            raise ValueError("SourceIR table node lost its TableIR payload")
        if table.table_id in seen_table_ids:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        seen_table_ids.add(table.table_id)
        cells = tuple(
            sorted(table.cells, key=lambda cell: (cell.row, cell.column, cell.cell_id))
        )
        if len(cells) > _MAX_TABLE_CELLS:
            raise ValueError("physical TableIR exceeds the region cell budget")
        result.append(
            _PhysicalTable(
                node=node,
                table=table,
                table_ir_sha256=_canonical_sha256(table.model_dump(mode="json")),
                cells=cells,
            )
        )
        if len(result) > _MAX_PHYSICAL_TABLES:
            raise ValueError("SourceIR exceeds the physical table region budget")
    return tuple(result)


def _candidate_to_work(region: D2TableRegionCandidateV1) -> _RegionWork:
    return _RegionWork(
        region_key=region.region_key,
        physical_table_id=region.physical_table_id,
        cell_spans=region.cell_spans,
        disposition=region.disposition,
        reason_codes=region.reason_codes,
        logical_scope_key=(
            region.logical_scope_key
            if isinstance(region, D2LogicalScopePartTableRegionCandidateV1)
            else None
        ),
        logical_object_key=(
            region.logical_object_key
            if isinstance(region, D2LogicalObjectPartTableRegionCandidateV1)
            else None
        ),
        nested_owner_region_key=(
            region.nested_owner_region_key
            if isinstance(region, D2NestedValueTableRegionCandidateV1)
            else None
        ),
    )


def _compile_cell_ref(
    *,
    source_ir: SourceIRSnapshot,
    source_canonical_sha256: str,
    physical: _PhysicalTable,
    ordinal: int,
) -> D2TableCellSourceRefV1:
    cell = physical.cells[ordinal]
    locator_payload = cell.native_locator.model_dump(mode="json", exclude_none=True)
    native_locator_sha256 = _canonical_sha256(locator_payload)
    raw_text_sha256 = _text_sha256(cell.raw_text)
    formula_sha256 = _optional_text_sha256(cell.formula)
    display_value_sha256 = _optional_text_sha256(cell.display_value)
    page_number = (
        cell.native_locator.page_number or physical.node.native_locator.page_number
    )
    values = {
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "source_ir_canonical_sha256": source_canonical_sha256,
        "table_source_node_id": physical.node.source_node_id,
        "physical_table_id": physical.table.table_id,
        "table_ir_sha256": physical.table_ir_sha256,
        "canonical_cell_ordinal": ordinal,
        "cell_id": cell.cell_id,
        "row": cell.row,
        "column": cell.column,
        "row_span": cell.row_span,
        "column_span": cell.column_span,
        "page_number": page_number,
        "bbox": cell.native_locator.bbox,
        "native_locator_sha256": native_locator_sha256,
        "raw_text_sha256": raw_text_sha256,
        "formula_sha256": formula_sha256,
        "display_value_sha256": display_value_sha256,
    }
    return D2TableCellSourceRefV1(
        **values,
        cell_evidence_sha256=stable_d2_table_cell_evidence_sha256(**values),
    )


def _table_supplemental_visual_evidence(
    *,
    source_ir: SourceIRSnapshot,
    physical: _PhysicalTable,
) -> tuple[D2TableSupplementalEvidenceRefV1, ...]:
    evidence: list[D2TableSupplementalEvidenceRefV1] = []
    if physical.node.visual_locator is not None:
        evidence.append(
            D2TableSupplementalEvidenceRefV1(
                evidence_kind=D2TableSupplementalEvidenceKind.TABLE_VISUAL_LOCATOR,
                evidence_ref=f"table-visual-locator:{physical.node.source_node_id}",
                evidence_sha256=_canonical_sha256(
                    physical.node.visual_locator.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                ),
            )
        )
    if physical.table.visual_asset_ref is not None:
        asset = next(
            (
                item
                for item in source_ir.assets
                if item.asset_id == physical.table.visual_asset_ref
            ),
            None,
        )
        if asset is None:
            raise ValueError("table visual asset ref is missing from SourceIR")
        evidence.append(
            D2TableSupplementalEvidenceRefV1(
                evidence_kind=D2TableSupplementalEvidenceKind.TABLE_VISUAL_ASSET,
                evidence_ref=f"table-visual-asset:{asset.asset_id}",
                evidence_sha256=_canonical_sha256(asset.model_dump(mode="json")),
            )
        )
    for snapshot in source_ir.visual_table_snapshots:
        if snapshot.table_source_node_id != physical.node.source_node_id:
            continue
        evidence.append(
            D2TableSupplementalEvidenceRefV1(
                evidence_kind=D2TableSupplementalEvidenceKind.VISUAL_TABLE_SNAPSHOT,
                evidence_ref=(
                    f"visual-table-snapshot:{snapshot.visual_table_snapshot_id}"
                ),
                evidence_sha256=_canonical_sha256(snapshot.model_dump(mode="json")),
            )
        )
    return tuple(
        sorted(
            evidence,
            key=lambda item: (item.evidence_kind.value, item.evidence_ref),
        )
    )


def _derive_region_row_summary(
    *,
    physical: _PhysicalTable,
    selected_refs: tuple[D2TableCellSourceRefV1, ...],
    disposition: D2TableRegionDispositionKind,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    D2TableRegionRowAlignment,
    D2TableRegionRowMaterializerEligibility,
]:
    all_refs = tuple(
        _row_fact_ref_from_cell(cell=cell, ordinal=ordinal)
        for ordinal, cell in enumerate(physical.cells)
    )
    return _derive_row_summary_from_refs(
        all_refs=all_refs,
        selected_refs=selected_refs,
        disposition=disposition,
    )


@dataclass(frozen=True)
class _RowFactRef:
    cell_id: str
    row: int
    row_span: int


def _row_fact_ref_from_cell(*, cell: TableCellIR, ordinal: int) -> _RowFactRef:
    del ordinal
    return _RowFactRef(cell_id=cell.cell_id, row=cell.row, row_span=cell.row_span)


def _derive_row_summary_from_refs(
    *,
    all_refs: tuple[_RowFactRef | D2TableCellSourceRefV1, ...],
    selected_refs: tuple[D2TableCellSourceRefV1, ...],
    disposition: D2TableRegionDispositionKind,
) -> tuple[
    tuple[int, ...],
    tuple[int, ...],
    tuple[int, ...],
    D2TableRegionRowAlignment,
    D2TableRegionRowMaterializerEligibility,
]:
    all_by_row: dict[int, set[str]] = {}
    for ref in all_refs:
        for row in range(ref.row, ref.row + ref.row_span):
            all_by_row.setdefault(row, set()).add(ref.cell_id)
    selected_by_row: dict[int, set[str]] = {}
    for ref in selected_refs:
        for row in range(ref.row, ref.row + ref.row_span):
            selected_by_row.setdefault(row, set()).add(ref.cell_id)
    covered_rows = tuple(sorted(selected_by_row))
    full_rows = tuple(
        row
        for row in covered_rows
        if selected_by_row[row] == all_by_row.get(row, set())
    )
    partial_rows = tuple(row for row in covered_rows if row not in set(full_rows))
    alignment = _row_alignment(full_rows=full_rows, partial_rows=partial_rows)
    eligibility = _row_materializer_eligibility(
        disposition=disposition,
        full_rows=full_rows,
        partial_rows=partial_rows,
    )
    return covered_rows, full_rows, partial_rows, alignment, eligibility


def _row_alignment(
    *,
    full_rows: tuple[int, ...],
    partial_rows: tuple[int, ...],
) -> D2TableRegionRowAlignment:
    if full_rows and partial_rows:
        return D2TableRegionRowAlignment.MIXED_FULL_AND_PARTIAL_ROWS
    if partial_rows:
        return D2TableRegionRowAlignment.PARTIAL_ROWS
    if full_rows:
        return D2TableRegionRowAlignment.FULL_ROWS
    raise ValueError("a cell region must cover at least one physical row")


def _row_materializer_eligibility(
    *,
    disposition: D2TableRegionDispositionKind,
    full_rows: tuple[int, ...],
    partial_rows: tuple[int, ...],
) -> D2TableRegionRowMaterializerEligibility:
    if disposition is not D2TableRegionDispositionKind.LOGICAL_SCOPE_PART:
        return D2TableRegionRowMaterializerEligibility.INELIGIBLE_DISPOSITION
    if partial_rows:
        return D2TableRegionRowMaterializerEligibility.INELIGIBLE_PARTIAL_ROW
    if full_rows != tuple(range(full_rows[0], full_rows[-1] + 1)):
        return D2TableRegionRowMaterializerEligibility.INELIGIBLE_NONCONTIGUOUS_ROWS
    return D2TableRegionRowMaterializerEligibility.SHAPE_ELIGIBLE_FULL_ROWS


def _require_region_row_summaries(
    table_regions: tuple[D2CompiledTableRegionV1, ...],
) -> None:
    if not table_regions:
        return
    all_refs = tuple(ref for region in table_regions for ref in region.cell_refs)
    for region in table_regions:
        expected = _derive_row_summary_from_refs(
            all_refs=all_refs,
            selected_refs=region.cell_refs,
            disposition=region.disposition,
        )
        observed = (
            region.covered_row_indices,
            region.fully_covered_row_indices,
            region.partially_covered_row_indices,
            region.row_alignment,
            region.row_materializer_eligibility,
        )
        if observed != expected:
            raise ValueError(
                "region row alignment/materializer eligibility differs from cell facts"
            )


def _region_work_sort_key(
    item: _RegionWork,
    table_rank: dict[str, int],
) -> tuple[object, ...]:
    return (
        table_rank[item.physical_table_id],
        item.cell_spans[0].start_cell_ordinal,
        tuple(
            (span.start_cell_ordinal, span.end_cell_ordinal_exclusive)
            for span in item.cell_spans
        ),
        item.region_key,
    )


def _ordinals_to_spans(
    ordinals: tuple[int, ...],
) -> tuple[D2PhysicalTableCellOrdinalSpanV1, ...]:
    if not ordinals:
        raise ValueError("cannot create a region from no cell ordinals")
    spans: list[D2PhysicalTableCellOrdinalSpanV1] = []
    start = previous = ordinals[0]
    for ordinal in ordinals[1:]:
        if ordinal == previous + 1:
            previous = ordinal
            continue
        spans.append(
            D2PhysicalTableCellOrdinalSpanV1(
                start_cell_ordinal=start,
                end_cell_ordinal_exclusive=previous + 1,
            )
        )
        start = previous = ordinal
    spans.append(
        D2PhysicalTableCellOrdinalSpanV1(
            start_cell_ordinal=start,
            end_cell_ordinal_exclusive=previous + 1,
        )
    )
    return tuple(spans)


def _require_acyclic_edges(edges: dict[str, str], relation_name: str) -> None:
    for start in edges:
        path: set[str] = set()
        current = start
        while current in edges:
            if current in path:
                raise ValueError(f"{relation_name} must be acyclic")
            path.add(current)
            current = edges[current]


def _require_candidate_nested_business_owners(
    regions_by_key: dict[str, D2TableRegionCandidateV1],
) -> None:
    for region in regions_by_key.values():
        if not isinstance(region, D2NestedValueTableRegionCandidateV1):
            continue
        current = regions_by_key[region.nested_owner_region_key]
        while isinstance(current, D2NestedValueTableRegionCandidateV1):
            current = regions_by_key[current.nested_owner_region_key]
        if not isinstance(
            current,
            (
                D2LogicalScopePartTableRegionCandidateV1,
                D2LogicalObjectPartTableRegionCandidateV1,
            ),
        ):
            raise ValueError(
                "nested region ownership must terminate at a logical scope/object region"
            )


def _require_compiled_nested_business_owners(
    regions_by_id: dict[str, D2CompiledTableRegionV1],
) -> None:
    edges: dict[str, str] = {}
    for region in regions_by_id.values():
        if region.disposition is not D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            continue
        owner_id = region.nested_owner_region_id
        if (
            owner_id is None
            or owner_id not in regions_by_id
            or owner_id == region.region_id
        ):
            raise ValueError("nested owner must be an existing other region")
        owner = regions_by_id[owner_id]
        if owner.disposition in {
            D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
            D2TableRegionDispositionKind.UNRESOLVED,
        }:
            raise ValueError("nested owner must be a business-bearing region")
        edges[region.region_id] = owner_id
    _require_acyclic_edges(edges, "compiled nested region ownership")
    for region_id in edges:
        owner = regions_by_id[edges[region_id]]
        while owner.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            owner_id = owner.nested_owner_region_id
            if owner_id is None or owner_id not in regions_by_id:
                raise ValueError("nested owner chain is incomplete")
            owner = regions_by_id[owner_id]
        if owner.disposition not in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }:
            raise ValueError(
                "nested region ownership must terminate at a logical scope/object region"
            )


def _json_values(values: dict[str, object]) -> dict[str, object]:
    return {key: _json_value(value) for key, value in values.items()}


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _stable_id(prefix: str, *parts: object) -> str:
    digest = _canonical_sha256(parts)
    return f"{prefix}-{digest[:32]}"


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_text_sha256(value: str | None) -> str | None:
    return None if value is None else _text_sha256(value)


def _require_sha256(value: str, field_name: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


__all__ = [
    "D2CompiledTableLogicalObjectV1",
    "D2CompiledTableLogicalScopeV1",
    "D2CompiledTableRegionV1",
    "D2EmptyPhysicalTableDispositionCandidateV1",
    "D2EmptyPhysicalTableDispositionKind",
    "D2LogicalObjectPartTableRegionCandidateV1",
    "D2LogicalScopePartTableRegionCandidateV1",
    "D2NestedValueTableRegionCandidateV1",
    "D2NonBusinessFalseTableRegionCandidateV1",
    "D2PhysicalTableCellOrdinalSpanV1",
    "D2PhysicalTableCellCoverageV1",
    "D2TableCellSourceRefV1",
    "D2TableLogicalScopeCandidateV1",
    "D2TableRegionArtifactStatus",
    "D2TableRegionArtifactV1",
    "D2TableRegionCandidateArtifactV1",
    "D2TableRegionCandidateV1",
    "D2TableRegionDispositionKind",
    "D2TableRegionGapPolicy",
    "D2TableRegionRowAlignment",
    "D2TableRegionRowMaterializerEligibility",
    "D2TableSupplementalEvidenceKind",
    "D2TableSupplementalEvidenceRefV1",
    "D2UnresolvedTableRegionCandidateV1",
    "compile_d2_table_regions",
    "d2_table_region_source_ir_canonical_sha256",
    "stable_d2_compiled_table_region_sha256",
    "stable_d2_physical_table_cell_coverage_sha256",
    "stable_d2_table_cell_evidence_sha256",
    "stable_d2_table_cell_local_evidence_sha256",
    "stable_d2_table_logical_object_id",
    "stable_d2_table_logical_object_sha256",
    "stable_d2_table_logical_scope_id",
    "stable_d2_table_logical_scope_sha256",
    "stable_d2_table_region_artifact_id",
    "stable_d2_table_region_artifact_sha256",
    "stable_d2_table_region_id",
]
