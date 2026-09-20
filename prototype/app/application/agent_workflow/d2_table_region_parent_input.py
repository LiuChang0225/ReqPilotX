from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.d2_table_regions import (
    D2CompiledTableRegionV1,
    D2EmptyPhysicalTableDispositionKind,
    D2PhysicalTableCellCoverageV1,
    D2TableCellSourceRefV1,
    D2TableRegionArtifactStatus,
    D2TableRegionArtifactV1,
    D2TableRegionDispositionKind,
    D2TableSupplementalEvidenceRefV1,
    d2_table_region_source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
)


D2_TABLE_REGION_PARENT_INPUT_SCHEMA_VERSION = "d2-table-region-parent-input/v1"
D2_TABLE_REGION_SOURCE_SEGMENT_SCHEMA_VERSION = "d2-table-region-source-segment/v1"
D2_TABLE_REGION_PARENT_INPUT_RECEIPT_SCHEMA_VERSION = (
    "d2-table-region-parent-input-receipt/v1"
)
D2_TABLE_REGION_PARENT_INPUT_ADAPTER_ID = "reqpilotx-table-region-parent-input"
D2_TABLE_REGION_PARENT_INPUT_ADAPTER_VERSION = "1.0.0"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2TableRegionParentInputStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class D2TableRegionVerbatimCellV1(_StrictFrozenModel):
    """One exact SourceIR cell retained with its Host-issued evidence binding."""

    canonical_cell_ordinal: StrictInt = Field(ge=0)
    cell_id: str = Field(min_length=1, max_length=160)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(ge=1)
    column_span: StrictInt = Field(ge=1)
    page_number: StrictInt | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None
    raw_text: str
    raw_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    cell_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    verbatim_cell_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_verbatim_cell(self) -> D2TableRegionVerbatimCellV1:
        if _text_sha256(self.raw_text) != self.raw_text_sha256:
            raise ValueError("verbatim cell text SHA does not match raw_text")
        expected = stable_d2_table_region_verbatim_cell_sha256(
            canonical_cell_ordinal=self.canonical_cell_ordinal,
            cell_id=self.cell_id,
            row=self.row,
            column=self.column,
            row_span=self.row_span,
            column_span=self.column_span,
            page_number=self.page_number,
            bbox=self.bbox,
            raw_text=self.raw_text,
            raw_text_sha256=self.raw_text_sha256,
            cell_evidence_sha256=self.cell_evidence_sha256,
        )
        if self.verbatim_cell_sha256 != expected:
            raise ValueError("verbatim cell SHA must bind its text and evidence")
        return self


class D2TableRegionSourceSegmentV1(_StrictFrozenModel):
    """Private exact text for one cell in a business-bearing table region.

    One-cell granularity prevents adjacent values such as ``24`` and ``V``
    from becoming the invented token ``24V``. ``source_position`` is the Host
    order key; stable local identity does not bind the surrounding revision.
    """

    schema_version: Literal["d2-table-region-source-segment/v1"] = (
        D2_TABLE_REGION_SOURCE_SEGMENT_SCHEMA_VERSION
    )
    segment_id: str = Field(pattern=r"^d2-table-region-source-segment-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    candidate_region_key: str = Field(min_length=1, max_length=160)
    disposition: Literal[
        D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
        D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
    ]
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    source_position: tuple[StrictInt, StrictInt]
    logical_scope_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$",
    )
    candidate_logical_scope_key: str | None = Field(default=None, max_length=160)
    logical_object_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-logical-object-[0-9a-f]{32}$",
    )
    candidate_logical_object_key: str | None = Field(
        default=None,
        max_length=160,
    )
    page_numbers: tuple[StrictInt, ...]
    cell_ids: tuple[str, ...] = Field(min_length=1, max_length=1)
    verbatim_cells: tuple[D2TableRegionVerbatimCellV1, ...] = Field(
        min_length=1,
        max_length=1,
    )
    source_text: str
    source_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=1)
    segment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("candidate_region_key")
    @classmethod
    def reject_blank_region_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("candidate region key must not be blank")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("segment pages must be positive, unique, and sorted")
        return value

    @model_validator(mode="after")
    def validate_segment(self) -> D2TableRegionSourceSegmentV1:
        cell = self.verbatim_cells[0]
        ordinals = tuple(cell.canonical_cell_ordinal for cell in self.verbatim_cells)
        if ordinals != tuple(sorted(set(ordinals))):
            raise ValueError("segment cells must follow unique canonical cell order")
        if self.cell_ids != tuple(cell.cell_id for cell in self.verbatim_cells):
            raise ValueError("segment cell IDs must exactly match verbatim cells")
        if len(self.cell_ids) != len(set(self.cell_ids)):
            raise ValueError("segment cannot repeat a SourceIR cell")
        if self.source_position != (
            self.physical_table_source_order,
            cell.canonical_cell_ordinal,
        ):
            raise ValueError("segment source position must be Host row-major order")
        expected_pages = tuple(
            sorted(
                {
                    cell.page_number
                    for cell in self.verbatim_cells
                    if cell.page_number is not None
                }
            )
        )
        if self.page_numbers != expected_pages:
            raise ValueError("segment pages must derive from its exact cells")
        if self.source_text != "".join(cell.raw_text for cell in self.verbatim_cells):
            raise ValueError("segment text must concatenate exact cells without edits")
        if _text_sha256(self.source_text) != self.source_text_sha256:
            raise ValueError("segment source text SHA mismatch")
        if self.evidence_refs != tuple(
            cell.cell_evidence_sha256 for cell in self.verbatim_cells
        ):
            raise ValueError("segment evidence refs must follow exact cell order")

        scope_bound = (
            self.logical_scope_id is not None
            and self.candidate_logical_scope_key is not None
            and self.logical_object_id is None
            and self.candidate_logical_object_key is None
        )
        object_bound = (
            self.logical_object_id is not None
            and self.candidate_logical_object_key is not None
            and self.logical_scope_id is None
            and self.candidate_logical_scope_key is None
        )
        if self.disposition is D2TableRegionDispositionKind.LOGICAL_SCOPE_PART:
            if not scope_bound:
                raise ValueError("logical-scope segment requires only scope identity")
        elif not object_bound:
            raise ValueError("logical-object segment requires only object identity")

        expected_id = stable_d2_table_region_source_segment_id(
            source_sha256=self.source_sha256,
            physical_table_id=self.physical_table_id,
            canonical_cell_ordinal=cell.canonical_cell_ordinal,
            cell_id=cell.cell_id,
            raw_text_sha256=cell.raw_text_sha256,
        )
        if self.segment_id != expected_id:
            raise ValueError("segment ID must bind its exact region and cells")
        expected_sha = stable_d2_table_region_source_segment_sha256(
            **self.model_dump(mode="json", exclude={"segment_sha256"})
        )
        if self.segment_sha256 != expected_sha:
            raise ValueError("segment SHA must bind its content and evidence")
        return self


class D2TableRegionParentScopeV1(_StrictFrozenModel):
    logical_scope_id: str = Field(pattern=r"^d2-table-logical-scope-[0-9a-f]{32}$")
    candidate_logical_scope_key: str = Field(min_length=1, max_length=160)
    compiled_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_region_ids: tuple[str, ...] = Field(min_length=1)
    ordered_segment_ids: tuple[str, ...] = Field(min_length=1)
    nested_region_ids: tuple[str, ...] = ()
    parent_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_scope(self) -> D2TableRegionParentScopeV1:
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("parent scope region IDs must be unique")
        if len(self.ordered_segment_ids) != len(set(self.ordered_segment_ids)):
            raise ValueError("parent scope segment IDs must be unique")
        if len(self.nested_region_ids) != len(set(self.nested_region_ids)):
            raise ValueError("nested region ledger must be unique")
        expected = stable_d2_table_region_parent_scope_sha256(
            **self.model_dump(mode="json", exclude={"parent_scope_sha256"})
        )
        if self.parent_scope_sha256 != expected:
            raise ValueError("parent scope SHA must bind ordered region segments")
        return self


class D2TableRegionParentObjectV1(_StrictFrozenModel):
    logical_object_id: str = Field(pattern=r"^d2-table-logical-object-[0-9a-f]{32}$")
    candidate_logical_object_key: str = Field(min_length=1, max_length=160)
    compiled_object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_region_ids: tuple[str, ...] = Field(min_length=1)
    ordered_segment_ids: tuple[str, ...] = Field(min_length=1)
    nested_region_ids: tuple[str, ...] = ()
    parent_object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_object(self) -> D2TableRegionParentObjectV1:
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("parent object region IDs must be unique")
        if len(self.ordered_segment_ids) != len(set(self.ordered_segment_ids)):
            raise ValueError("parent object segment IDs must be unique")
        if len(self.nested_region_ids) != len(set(self.nested_region_ids)):
            raise ValueError("nested region ledger must be unique")
        expected = stable_d2_table_region_parent_object_sha256(
            **self.model_dump(mode="json", exclude={"parent_object_sha256"})
        )
        if self.parent_object_sha256 != expected:
            raise ValueError("parent object SHA must bind ordered region segments")
        return self


class D2TableRegionExcludedEvidenceV1(_StrictFrozenModel):
    """Exact evidence intentionally excluded from independent parent scope."""

    evidence_id: str = Field(
        pattern=r"^d2-table-region-excluded-evidence-[0-9a-f]{32}$"
    )
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    candidate_region_key: str = Field(min_length=1, max_length=160)
    disposition: Literal[
        D2TableRegionDispositionKind.NESTED_VALUE_REGION,
        D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION,
    ]
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    nested_owner_region_id: str | None = Field(
        default=None,
        pattern=r"^d2-table-region-[0-9a-f]{32}$",
    )
    owner_source_segment_ids: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    cell_ids: tuple[str, ...] = Field(min_length=1)
    verbatim_cells: tuple[D2TableRegionVerbatimCellV1, ...] = Field(min_length=1)
    source_text: str
    source_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("excluded evidence reasons must be canonical")
        return value

    @field_validator("owner_source_segment_ids")
    @classmethod
    def validate_owner_segments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"d2-table-region-source-segment-[0-9a-f]{32}", item) is None
            for item in value
        ):
            raise ValueError("owner source segment IDs must be unique stable IDs")
        return value

    @model_validator(mode="after")
    def validate_evidence(self) -> D2TableRegionExcludedEvidenceV1:
        _validate_content_evidence(
            cell_ids=self.cell_ids,
            verbatim_cells=self.verbatim_cells,
            source_text=self.source_text,
            source_text_sha256=self.source_text_sha256,
            evidence_refs=self.evidence_refs,
            label="excluded region",
        )
        if self.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            if self.nested_owner_region_id is None:
                raise ValueError("nested evidence requires its owner region")
        elif self.nested_owner_region_id is not None or self.owner_source_segment_ids:
            raise ValueError("non-business evidence cannot claim a semantic owner")
        expected_id = stable_d2_table_region_excluded_evidence_id(
            region_id=self.region_id,
            region_sha256=self.region_sha256,
            disposition=self.disposition,
            cell_ids=self.cell_ids,
        )
        if self.evidence_id != expected_id:
            raise ValueError("excluded evidence ID must bind exact region cells")
        expected_sha = stable_d2_table_region_excluded_evidence_sha256(
            **self.model_dump(mode="json", exclude={"evidence_sha256"})
        )
        if self.evidence_sha256 != expected_sha:
            raise ValueError("excluded evidence SHA must bind content and disposition")
        return self


class D2TableRegionUnresolvedScopeV1(_StrictFrozenModel):
    """Evidence scope requiring a later decision, never a semantic segment."""

    unresolved_scope_id: str = Field(
        pattern=r"^d2-table-region-unresolved-scope-[0-9a-f]{32}$"
    )
    region_id: str = Field(pattern=r"^d2-table-region-[0-9a-f]{32}$")
    region_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    candidate_region_key: str = Field(min_length=1, max_length=160)
    disposition: Literal[D2TableRegionDispositionKind.UNRESOLVED] = (
        D2TableRegionDispositionKind.UNRESOLVED
    )
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    host_auto_filled: StrictBool
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    cell_ids: tuple[str, ...] = Field(min_length=1)
    verbatim_cells: tuple[D2TableRegionVerbatimCellV1, ...] = Field(min_length=1)
    source_text: str
    source_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_refs: tuple[str, ...] = Field(min_length=1)
    unresolved_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("unresolved reasons must be canonical")
        return value

    @model_validator(mode="after")
    def validate_unresolved(self) -> D2TableRegionUnresolvedScopeV1:
        _validate_content_evidence(
            cell_ids=self.cell_ids,
            verbatim_cells=self.verbatim_cells,
            source_text=self.source_text,
            source_text_sha256=self.source_text_sha256,
            evidence_refs=self.evidence_refs,
            label="unresolved region",
        )
        expected_id = stable_d2_table_region_unresolved_scope_id(
            region_id=self.region_id,
            region_sha256=self.region_sha256,
            cell_ids=self.cell_ids,
        )
        if self.unresolved_scope_id != expected_id:
            raise ValueError("unresolved scope ID must bind exact region cells")
        expected_sha = stable_d2_table_region_unresolved_scope_sha256(
            **self.model_dump(mode="json", exclude={"unresolved_scope_sha256"})
        )
        if self.unresolved_scope_sha256 != expected_sha:
            raise ValueError("unresolved scope SHA must bind content and evidence")
        return self


class D2TableRegionPhysicalCellPartitionV1(_StrictFrozenModel):
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    canonical_cell_ids: tuple[str, ...] = ()
    ordered_region_ids: tuple[str, ...] = ()
    empty_table_disposition: D2EmptyPhysicalTableDispositionKind | None = None
    empty_table_reason_codes: tuple[str, ...] = ()
    cell_coverage_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    partition_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_partition(self) -> D2TableRegionPhysicalCellPartitionV1:
        if len(self.canonical_cell_ids) != len(set(self.canonical_cell_ids)):
            raise ValueError("physical cell partition cannot repeat a cell")
        if len(self.ordered_region_ids) != len(set(self.ordered_region_ids)):
            raise ValueError("physical cell partition cannot repeat a region")
        if bool(self.canonical_cell_ids) != bool(self.ordered_region_ids):
            raise ValueError("non-empty cell partitions require regions and vice versa")
        if self.canonical_cell_ids:
            if (
                self.empty_table_disposition is not None
                or self.empty_table_reason_codes
            ):
                raise ValueError(
                    "non-empty partition cannot claim empty-table disposition"
                )
        elif self.empty_table_disposition is None or not self.empty_table_reason_codes:
            raise ValueError("empty partition requires an explicit disposition")
        expected = stable_d2_table_region_physical_partition_sha256(
            **self.model_dump(mode="json", exclude={"partition_sha256"})
        )
        if self.partition_sha256 != expected:
            raise ValueError("physical partition SHA must bind exact cell coverage")
        return self


class D2TableRegionSupplementalEvidenceV1(_StrictFrozenModel):
    """Private evidence outside the exact TableIR cell-anchor partition."""

    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    physical_table_source_order: StrictInt = Field(ge=0)
    table_ir_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_node_raw_text: str
    table_node_raw_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    table_notes: tuple[str, ...] = ()
    table_notes_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    supplemental_visual_evidence: tuple[D2TableSupplementalEvidenceRefV1, ...] = ()
    semantic_scope: Literal["SUPPLEMENTAL_NOT_CELL_DISPOSITIONED"] = (
        "SUPPLEMENTAL_NOT_CELL_DISPOSITIONED"
    )
    empty_table_disposition: D2EmptyPhysicalTableDispositionKind | None = None
    empty_table_reason_codes: tuple[str, ...] = ()
    residual_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_residual(self) -> D2TableRegionSupplementalEvidenceV1:
        if _text_sha256(self.table_node_raw_text) != self.table_node_raw_text_sha256:
            raise ValueError("table-node raw text SHA mismatch")
        if _canonical_sha256(self.table_notes) != self.table_notes_sha256:
            raise ValueError("table notes SHA mismatch")
        keys = tuple(
            (item.evidence_kind.value, item.evidence_ref)
            for item in self.supplemental_visual_evidence
        )
        if keys != tuple(sorted(set(keys))):
            raise ValueError("supplemental visual evidence must be canonical")
        expected = stable_d2_table_region_supplemental_evidence_sha256(
            **self.model_dump(mode="json", exclude={"residual_evidence_sha256"})
        )
        if self.residual_evidence_sha256 != expected:
            raise ValueError("supplemental evidence SHA must bind exact residuals")
        return self


class D2TableRegionParentInputV1(_StrictFrozenModel):
    """Content-bearing adapter result restricted to private stage input."""

    schema_version: Literal["d2-table-region-parent-input/v1"] = (
        D2_TABLE_REGION_PARENT_INPUT_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_stage_input_only: Literal[True] = True
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_status: SourceIRStatus
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_artifact_status: D2TableRegionArtifactStatus
    status: D2TableRegionParentInputStatus
    physical_partitions: tuple[D2TableRegionPhysicalCellPartitionV1, ...] = ()
    supplemental_table_evidence: tuple[D2TableRegionSupplementalEvidenceV1, ...] = ()
    ordered_region_ids: tuple[str, ...] = ()
    ordered_region_sha256s: tuple[str, ...] = ()
    source_segments: tuple[D2TableRegionSourceSegmentV1, ...] = ()
    logical_scopes: tuple[D2TableRegionParentScopeV1, ...] = ()
    logical_objects: tuple[D2TableRegionParentObjectV1, ...] = ()
    nested_region_evidence: tuple[D2TableRegionExcludedEvidenceV1, ...] = ()
    non_business_region_evidence: tuple[D2TableRegionExcludedEvidenceV1, ...] = ()
    unresolved_scopes: tuple[D2TableRegionUnresolvedScopeV1, ...] = ()
    stage_input_id: str = Field(pattern=r"^d2-table-region-parent-input-[0-9a-f]{32}$")
    stage_input_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_stage_input(self) -> D2TableRegionParentInputV1:
        if self.source_ir_status is SourceIRStatus.FAILED:
            raise ValueError("failed SourceIR cannot produce parent stage input")
        if tuple(
            item.physical_table_source_order for item in self.physical_partitions
        ) != tuple(
            sorted(
                item.physical_table_source_order for item in self.physical_partitions
            )
        ):
            raise ValueError("physical partitions must follow source order")
        if tuple(
            item.physical_table_id for item in self.supplemental_table_evidence
        ) != tuple(item.physical_table_id for item in self.physical_partitions):
            raise ValueError("every physical table requires supplemental evidence")
        if any(
            residual.physical_table_source_order
            != partition.physical_table_source_order
            or residual.table_source_node_id != partition.table_source_node_id
            or residual.table_ir_sha256 != partition.table_ir_sha256
            or residual.empty_table_disposition != partition.empty_table_disposition
            or residual.empty_table_reason_codes != partition.empty_table_reason_codes
            for residual, partition in zip(
                self.supplemental_table_evidence,
                self.physical_partitions,
                strict=True,
            )
        ):
            raise ValueError("supplemental evidence and cell partition lineage differ")

        segment_by_id = {item.segment_id: item for item in self.source_segments}
        if len(segment_by_id) != len(self.source_segments):
            raise ValueError("parent stage input source segment IDs must be unique")
        expected_segment_order = tuple(
            sorted(
                self.source_segments,
                key=lambda item: (
                    item.source_position,
                    item.segment_id,
                ),
            )
        )
        if self.source_segments != expected_segment_order:
            raise ValueError(
                "parent source segments must follow canonical source order"
            )

        scope_by_id = {item.logical_scope_id: item for item in self.logical_scopes}
        if len(scope_by_id) != len(self.logical_scopes):
            raise ValueError("logical parent scopes must be unique")
        for scope in self.logical_scopes:
            members = tuple(
                segment
                for segment in self.source_segments
                if segment.logical_scope_id == scope.logical_scope_id
            )
            if _ordered_unique(item.region_id for item in members) != (
                scope.ordered_region_ids
            ):
                raise ValueError("logical scope regions lost cross-table source order")
            if tuple(item.segment_id for item in members) != scope.ordered_segment_ids:
                raise ValueError("logical scope segments lost cross-table source order")

        object_by_id = {item.logical_object_id: item for item in self.logical_objects}
        if len(object_by_id) != len(self.logical_objects):
            raise ValueError("logical parent objects must be unique")
        for logical_object in self.logical_objects:
            members = tuple(
                segment
                for segment in self.source_segments
                if segment.logical_object_id == logical_object.logical_object_id
            )
            if (
                _ordered_unique(item.region_id for item in members)
                != logical_object.ordered_region_ids
                or tuple(item.segment_id for item in members)
                != logical_object.ordered_segment_ids
            ):
                raise ValueError("logical object segments must remain source ordered")

        if any(
            item.disposition is not D2TableRegionDispositionKind.NESTED_VALUE_REGION
            for item in self.nested_region_evidence
        ):
            raise ValueError("nested evidence ledger may contain only nested regions")
        if any(
            item.disposition
            is not D2TableRegionDispositionKind.NON_BUSINESS_FALSE_REGION
            for item in self.non_business_region_evidence
        ):
            raise ValueError(
                "non-business evidence ledger may contain only false regions"
            )

        semantic_by_region: dict[str, tuple[D2TableRegionSourceSegmentV1, ...]] = {
            region_id: tuple(
                item for item in self.source_segments if item.region_id == region_id
            )
            for region_id in _ordered_unique(
                item.region_id for item in self.source_segments
            )
        }
        nonsemantic_records = (
            *self.nested_region_evidence,
            *self.non_business_region_evidence,
            *self.unresolved_scopes,
        )
        nonsemantic_by_region = {item.region_id: item for item in nonsemantic_records}
        if len(nonsemantic_by_region) != len(nonsemantic_records):
            raise ValueError("non-semantic region dispositions must be unique")
        represented_region_ids = set(semantic_by_region) | set(nonsemantic_by_region)
        if set(semantic_by_region).intersection(nonsemantic_by_region):
            raise ValueError("one region cannot be both semantic and excluded")
        if represented_region_ids != set(self.ordered_region_ids) or len(
            self.ordered_region_ids
        ) != len(set(self.ordered_region_ids)):
            raise ValueError(
                "parent dispositions must match canonical artifact regions"
            )
        if tuple(
            (
                semantic_by_region[region_id][0].region_sha256
                if region_id in semantic_by_region
                else nonsemantic_by_region[region_id].region_sha256
            )
            for region_id in self.ordered_region_ids
        ) != (self.ordered_region_sha256s):
            raise ValueError("region SHA ledger must exactly match parent dispositions")

        partition_region_ids = tuple(
            region_id
            for partition in self.physical_partitions
            for region_id in partition.ordered_region_ids
        )
        if partition_region_ids != self.ordered_region_ids:
            raise ValueError("physical partitions must own every region exactly once")
        for partition in self.physical_partitions:
            records = tuple(
                (
                    semantic_by_region[region_id]
                    if region_id in semantic_by_region
                    else (nonsemantic_by_region[region_id],)
                )
                for region_id in partition.ordered_region_ids
            )
            if any(
                item.physical_table_id != partition.physical_table_id
                or item.table_source_node_id != partition.table_source_node_id
                or item.physical_table_source_order
                != partition.physical_table_source_order
                for region_records in records
                for item in region_records
            ):
                raise ValueError("physical partition and region source binding differ")
            cells = tuple(
                sorted(
                    (
                        cell
                        for region_records in records
                        for record in region_records
                        for cell in record.verbatim_cells
                    ),
                    key=lambda item: item.canonical_cell_ordinal,
                )
            )
            if tuple(cell.canonical_cell_ordinal for cell in cells) != tuple(
                range(len(partition.canonical_cell_ids))
            ):
                raise ValueError("physical cells must be partitioned exactly once")
            if tuple(cell.cell_id for cell in cells) != partition.canonical_cell_ids:
                raise ValueError("physical cell ledger differs from exact evidence")

        expected_status = (
            D2TableRegionParentInputStatus.PARTIAL
            if self.source_ir_status is SourceIRStatus.PARTIAL
            or self.region_artifact_status is D2TableRegionArtifactStatus.PARTIAL
            or self.unresolved_scopes
            or any(
                item.empty_table_disposition
                is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
                for item in self.physical_partitions
            )
            else D2TableRegionParentInputStatus.COMPLETE
        )
        if self.status is not expected_status:
            raise ValueError("parent input status must reflect unresolved evidence")
        expected_id = stable_d2_table_region_parent_input_id(
            source_ir_id=self.source_ir_id,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            kit_bundle_sha256=self.kit_bundle_sha256,
            d1_table_map_artifact_id=self.d1_table_map_artifact_id,
            d1_table_map_artifact_sha256=self.d1_table_map_artifact_sha256,
            region_artifact_id=self.region_artifact_id,
            region_artifact_sha256=self.region_artifact_sha256,
        )
        if self.stage_input_id != expected_id:
            raise ValueError("stage-input ID must bind immutable source lineage")
        expected_sha = stable_d2_table_region_parent_input_sha256(
            **self.model_dump(mode="json", exclude={"stage_input_sha256"})
        )
        if self.stage_input_sha256 != expected_sha:
            raise ValueError("stage-input SHA must bind all content and dispositions")
        return self


class D2TableRegionParentInputReceiptV1(_StrictFrozenModel):
    """Content-free proof for one private region-to-parent materialization."""

    schema_version: Literal["d2-table-region-parent-input-receipt/v1"] = (
        D2_TABLE_REGION_PARENT_INPUT_RECEIPT_SCHEMA_VERSION
    )
    adapter_id: Literal["reqpilotx-table-region-parent-input"] = (
        D2_TABLE_REGION_PARENT_INPUT_ADAPTER_ID
    )
    adapter_version: Literal["1.0.0"] = D2_TABLE_REGION_PARENT_INPUT_ADAPTER_VERSION
    content_free: Literal[True] = True
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    stage_input_id: str = Field(pattern=r"^d2-table-region-parent-input-[0-9a-f]{32}$")
    stage_input_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_segment_ids: tuple[str, ...] = ()
    source_segment_sha256s: tuple[str, ...] = ()
    logical_scope_sha256s: tuple[str, ...] = ()
    logical_object_sha256s: tuple[str, ...] = ()
    excluded_evidence_sha256s: tuple[str, ...] = ()
    unresolved_scope_sha256s: tuple[str, ...] = ()
    physical_partition_sha256s: tuple[str, ...] = ()
    supplemental_evidence_sha256s: tuple[str, ...] = ()
    receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_receipt(self) -> D2TableRegionParentInputReceiptV1:
        if len(self.source_segment_ids) != len(set(self.source_segment_ids)):
            raise ValueError("receipt cannot repeat source segment IDs")
        if len(self.source_segment_ids) != len(self.source_segment_sha256s):
            raise ValueError("receipt segment IDs and SHAs must be one-to-one")
        expected = stable_d2_table_region_parent_input_receipt_sha256(
            **self.model_dump(mode="json", exclude={"receipt_sha256"})
        )
        if self.receipt_sha256 != expected:
            raise ValueError("receipt SHA must bind content-free materialization proof")
        return self


class D2TableRegionParentInputCompilationV1(_StrictFrozenModel):
    stage_input: D2TableRegionParentInputV1
    receipt: D2TableRegionParentInputReceiptV1

    @model_validator(mode="after")
    def validate_compilation(self) -> D2TableRegionParentInputCompilationV1:
        stage_input = self.stage_input
        receipt = self.receipt
        if (
            receipt.source_ir_id != stage_input.source_ir_id
            or receipt.source_sha256 != stage_input.source_sha256
            or receipt.source_ir_canonical_sha256
            != stage_input.source_ir_canonical_sha256
            or receipt.kit_bundle_sha256 != stage_input.kit_bundle_sha256
            or receipt.d1_table_map_artifact_id != stage_input.d1_table_map_artifact_id
            or receipt.d1_table_map_artifact_sha256
            != stage_input.d1_table_map_artifact_sha256
            or receipt.region_artifact_id != stage_input.region_artifact_id
            or receipt.region_artifact_sha256 != stage_input.region_artifact_sha256
            or receipt.stage_input_id != stage_input.stage_input_id
            or receipt.stage_input_sha256 != stage_input.stage_input_sha256
        ):
            raise ValueError("receipt lineage differs from private stage input")
        return self


def build_d2_table_region_parent_input(
    source_ir: SourceIRSnapshot,
    region_artifact: D2TableRegionArtifactV1,
    *,
    expected_kit_bundle_sha256: str,
    expected_d1_table_map_artifact_id: str,
    expected_d1_table_map_artifact_sha256: str,
    expected_region_artifact_id: str,
    expected_region_artifact_sha256: str,
) -> D2TableRegionParentInputCompilationV1:
    """Materialize exact table-region text without changing D2/D3 serving.

    The caller must pin the already validated region artifact identity. This
    rejects a different, internally valid artifact replayed for the same
    SourceIR and Kit.
    """

    _require_sha256(expected_kit_bundle_sha256, "expected_kit_bundle_sha256")
    _require_sha256(
        expected_d1_table_map_artifact_sha256,
        "expected_d1_table_map_artifact_sha256",
    )
    _require_sha256(
        expected_region_artifact_sha256,
        "expected_region_artifact_sha256",
    )
    # Re-run strict artifact validation so model_copy/model_construct cannot
    # bypass the artifact's own stable-ID and SHA contract.
    artifact = D2TableRegionArtifactV1.model_validate(
        region_artifact.model_dump(mode="json")
    )
    source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    source_canonical_sha256 = d2_table_region_source_ir_canonical_sha256(source)
    if (
        artifact.source_ir_id != source.source_ir_id
        or artifact.source_sha256 != source.source_sha256
        or artifact.source_ir_canonical_sha256 != source_canonical_sha256
    ):
        raise ValueError("table-region artifact SourceIR identity is stale or drifted")
    if artifact.kit_bundle_sha256 != expected_kit_bundle_sha256:
        raise ValueError("table-region artifact Agent Kit identity is stale or drifted")
    if (
        artifact.d1_table_map_artifact_id != expected_d1_table_map_artifact_id
        or artifact.d1_table_map_artifact_sha256
        != expected_d1_table_map_artifact_sha256
    ):
        raise ValueError("table-region artifact D1 table-map lineage drifted")
    if (
        artifact.artifact_id != expected_region_artifact_id
        or artifact.artifact_sha256 != expected_region_artifact_sha256
    ):
        raise ValueError(
            "table-region artifact identity is stale, drifted, or replayed"
        )
    if source.status is SourceIRStatus.FAILED:
        raise ValueError("failed SourceIR cannot produce table-region parent input")

    tables = _physical_table_catalog(source)
    tables_by_id = {item.table.table_id: item for item in tables}
    if tuple(item.table.table_id for item in tables) != tuple(
        item.physical_table_id for item in artifact.physical_table_cell_coverage
    ):
        raise ValueError("table-region artifact physical-table scope drifted")
    for physical, coverage in zip(
        tables,
        artifact.physical_table_cell_coverage,
        strict=True,
    ):
        if (
            coverage.table_source_node_id != physical.node.source_node_id
            or coverage.physical_table_source_order != physical.node.source_order
            or coverage.table_ir_sha256 != physical.table_ir_sha256
            or coverage.table_node_raw_text_sha256
            != _text_sha256(physical.node.raw_text)
            or coverage.table_notes_sha256 != _canonical_sha256(physical.table.notes)
            or coverage.canonical_cell_ids
            != tuple(cell.cell_id for cell in physical.cells)
        ):
            raise ValueError("physical table coverage drifted from SourceIR evidence")

    scope_by_id = {item.logical_scope_id: item for item in artifact.logical_scopes}
    object_by_id = {item.logical_object_id: item for item in artifact.logical_objects}
    verbatim_by_region: dict[str, tuple[D2TableRegionVerbatimCellV1, ...]] = {}
    for region in artifact.regions:
        physical = tables_by_id.get(region.physical_table_id)
        if physical is None:
            raise ValueError(
                "table-region artifact references an unknown SourceIR table"
            )
        verbatim_by_region[region.region_id] = _resolve_region_cells(
            source=source,
            source_canonical_sha256=source_canonical_sha256,
            physical=physical,
            region=region,
        )

    source_segments: list[D2TableRegionSourceSegmentV1] = []
    for region in artifact.regions:
        if region.disposition not in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }:
            continue
        scope = (
            scope_by_id[region.logical_scope_id]
            if region.logical_scope_id is not None
            else None
        )
        logical_object = (
            object_by_id[region.logical_object_id]
            if region.logical_object_id is not None
            else None
        )
        for cell in verbatim_by_region[region.region_id]:
            common = _content_values((cell,))
            values = {
                "source_ir_id": source.source_ir_id,
                "source_sha256": source.source_sha256,
                "source_ir_canonical_sha256": source_canonical_sha256,
                "kit_bundle_sha256": expected_kit_bundle_sha256,
                "region_artifact_id": artifact.artifact_id,
                "region_artifact_sha256": artifact.artifact_sha256,
                "region_id": region.region_id,
                "region_sha256": region.region_sha256,
                "candidate_region_key": region.candidate_region_key,
                "disposition": region.disposition,
                "physical_table_id": region.physical_table_id,
                "table_source_node_id": region.table_source_node_id,
                "physical_table_source_order": region.physical_table_source_order,
                "source_position": (
                    region.physical_table_source_order,
                    cell.canonical_cell_ordinal,
                ),
                "logical_scope_id": region.logical_scope_id,
                "candidate_logical_scope_key": (
                    scope.candidate_scope_key if scope is not None else None
                ),
                "logical_object_id": region.logical_object_id,
                "candidate_logical_object_key": (
                    logical_object.candidate_object_key
                    if logical_object is not None
                    else None
                ),
                "page_numbers": (
                    (cell.page_number,) if cell.page_number is not None else ()
                ),
                **common,
            }
            segment_id = stable_d2_table_region_source_segment_id(
                source_sha256=source.source_sha256,
                physical_table_id=region.physical_table_id,
                canonical_cell_ordinal=cell.canonical_cell_ordinal,
                cell_id=cell.cell_id,
                raw_text_sha256=cell.raw_text_sha256,
            )
            segment_values = {"segment_id": segment_id, **values}
            segment_draft = D2TableRegionSourceSegmentV1.model_construct(
                **segment_values,
                segment_sha256="0" * 64,
            )
            source_segments.append(
                D2TableRegionSourceSegmentV1(
                    **segment_values,
                    segment_sha256=stable_d2_table_region_source_segment_sha256(
                        **segment_draft.model_dump(
                            mode="json",
                            exclude={"segment_sha256"},
                        )
                    ),
                )
            )
    compiled_segments = tuple(
        sorted(
            source_segments,
            key=lambda item: (item.source_position, item.segment_id),
        )
    )
    segments_by_region_id = {
        region.region_id: tuple(
            item for item in compiled_segments if item.region_id == region.region_id
        )
        for region in artifact.regions
        if region.disposition
        in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }
    }

    regions_by_id = {item.region_id: item for item in artifact.regions}

    def nested_descendants(owner_region_ids: tuple[str, ...]) -> tuple[str, ...]:
        owner_set = set(owner_region_ids)

        def belongs_to_owner(region: D2CompiledTableRegionV1) -> bool:
            owner_id = region.nested_owner_region_id
            seen: set[str] = set()
            while owner_id is not None:
                if owner_id in owner_set:
                    return True
                if owner_id in seen:
                    return False
                seen.add(owner_id)
                owner = regions_by_id.get(owner_id)
                owner_id = owner.nested_owner_region_id if owner is not None else None
            return False

        return tuple(
            region.region_id
            for region in artifact.regions
            if region.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION
            and belongs_to_owner(region)
        )

    logical_scopes = tuple(
        _build_parent_scope(
            item,
            segments_by_region_id=segments_by_region_id,
            nested_region_ids=nested_descendants(item.ordered_region_ids),
        )
        for item in artifact.logical_scopes
    )
    logical_objects = tuple(
        _build_parent_object(
            item,
            segments_by_region_id=segments_by_region_id,
            nested_region_ids=nested_descendants(item.ordered_region_ids),
        )
        for item in artifact.logical_objects
    )

    nested_evidence: list[D2TableRegionExcludedEvidenceV1] = []
    non_business_evidence: list[D2TableRegionExcludedEvidenceV1] = []
    unresolved_scopes: list[D2TableRegionUnresolvedScopeV1] = []
    for region in artifact.regions:
        if region.disposition in {
            D2TableRegionDispositionKind.LOGICAL_SCOPE_PART,
            D2TableRegionDispositionKind.LOGICAL_OBJECT_PART,
        }:
            continue
        cells = verbatim_by_region[region.region_id]
        common = _content_values(cells)
        common_values = {
            "region_id": region.region_id,
            "region_sha256": region.region_sha256,
            "candidate_region_key": region.candidate_region_key,
            "physical_table_id": region.physical_table_id,
            "table_source_node_id": region.table_source_node_id,
            "physical_table_source_order": region.physical_table_source_order,
            "reason_codes": region.reason_codes,
            **common,
        }
        if region.disposition is D2TableRegionDispositionKind.UNRESOLVED:
            unresolved_id = stable_d2_table_region_unresolved_scope_id(
                region_id=region.region_id,
                region_sha256=region.region_sha256,
                cell_ids=common["cell_ids"],
            )
            unresolved_values = {
                "unresolved_scope_id": unresolved_id,
                **common_values,
                "host_auto_filled": region.host_auto_filled,
            }
            unresolved_draft = D2TableRegionUnresolvedScopeV1.model_construct(
                **unresolved_values,
                unresolved_scope_sha256="0" * 64,
            )
            unresolved_scopes.append(
                D2TableRegionUnresolvedScopeV1(
                    **unresolved_values,
                    unresolved_scope_sha256=(
                        stable_d2_table_region_unresolved_scope_sha256(
                            **unresolved_draft.model_dump(
                                mode="json",
                                exclude={"unresolved_scope_sha256"},
                            )
                        )
                    ),
                )
            )
            continue
        owner_segments = (
            segments_by_region_id.get(region.nested_owner_region_id, ())
            if region.nested_owner_region_id is not None
            else ()
        )
        evidence_id = stable_d2_table_region_excluded_evidence_id(
            region_id=region.region_id,
            region_sha256=region.region_sha256,
            disposition=region.disposition,
            cell_ids=common["cell_ids"],
        )
        excluded_values = {
            "evidence_id": evidence_id,
            **common_values,
            "disposition": region.disposition,
            "nested_owner_region_id": region.nested_owner_region_id,
            "owner_source_segment_ids": tuple(
                item.segment_id for item in owner_segments
            ),
        }
        excluded_draft = D2TableRegionExcludedEvidenceV1.model_construct(
            **excluded_values,
            evidence_sha256="0" * 64,
        )
        evidence = D2TableRegionExcludedEvidenceV1(
            **excluded_values,
            evidence_sha256=stable_d2_table_region_excluded_evidence_sha256(
                **excluded_draft.model_dump(
                    mode="json",
                    exclude={"evidence_sha256"},
                )
            ),
        )
        if region.disposition is D2TableRegionDispositionKind.NESTED_VALUE_REGION:
            nested_evidence.append(evidence)
        else:
            non_business_evidence.append(evidence)

    physical_partitions = tuple(
        _build_physical_partition(item)
        for item in artifact.physical_table_cell_coverage
    )
    supplemental_table_evidence = tuple(
        _build_supplemental_evidence(physical, coverage)
        for physical, coverage in zip(
            tables,
            artifact.physical_table_cell_coverage,
            strict=True,
        )
    )
    status = (
        D2TableRegionParentInputStatus.PARTIAL
        if source.status is SourceIRStatus.PARTIAL
        or artifact.status is D2TableRegionArtifactStatus.PARTIAL
        or unresolved_scopes
        or any(
            item.empty_table_disposition
            is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
            for item in physical_partitions
        )
        else D2TableRegionParentInputStatus.COMPLETE
    )
    stage_input_id = stable_d2_table_region_parent_input_id(
        source_ir_id=source.source_ir_id,
        source_ir_canonical_sha256=source_canonical_sha256,
        kit_bundle_sha256=expected_kit_bundle_sha256,
        d1_table_map_artifact_id=expected_d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=expected_d1_table_map_artifact_sha256,
        region_artifact_id=artifact.artifact_id,
        region_artifact_sha256=artifact.artifact_sha256,
    )
    stage_values = {
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_ir_canonical_sha256": source_canonical_sha256,
        "source_ir_status": source.status,
        "kit_bundle_sha256": expected_kit_bundle_sha256,
        "d1_table_map_artifact_id": expected_d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": expected_d1_table_map_artifact_sha256,
        "region_artifact_id": artifact.artifact_id,
        "region_artifact_sha256": artifact.artifact_sha256,
        "region_artifact_status": artifact.status,
        "status": status,
        "physical_partitions": physical_partitions,
        "supplemental_table_evidence": supplemental_table_evidence,
        "ordered_region_ids": tuple(item.region_id for item in artifact.regions),
        "ordered_region_sha256s": tuple(
            item.region_sha256 for item in artifact.regions
        ),
        "source_segments": compiled_segments,
        "logical_scopes": logical_scopes,
        "logical_objects": logical_objects,
        "nested_region_evidence": tuple(nested_evidence),
        "non_business_region_evidence": tuple(non_business_evidence),
        "unresolved_scopes": tuple(unresolved_scopes),
        "stage_input_id": stage_input_id,
    }
    stage_draft = D2TableRegionParentInputV1.model_construct(
        **stage_values,
        stage_input_sha256="0" * 64,
    )
    stage_input = D2TableRegionParentInputV1(
        **stage_values,
        stage_input_sha256=stable_d2_table_region_parent_input_sha256(
            **stage_draft.model_dump(
                mode="json",
                exclude={"stage_input_sha256"},
            )
        ),
    )
    receipt_values = {
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_ir_canonical_sha256": source_canonical_sha256,
        "kit_bundle_sha256": expected_kit_bundle_sha256,
        "d1_table_map_artifact_id": expected_d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": expected_d1_table_map_artifact_sha256,
        "region_artifact_id": artifact.artifact_id,
        "region_artifact_sha256": artifact.artifact_sha256,
        "stage_input_id": stage_input.stage_input_id,
        "stage_input_sha256": stage_input.stage_input_sha256,
        "source_segment_ids": tuple(
            item.segment_id for item in stage_input.source_segments
        ),
        "source_segment_sha256s": tuple(
            item.segment_sha256 for item in stage_input.source_segments
        ),
        "logical_scope_sha256s": tuple(
            item.parent_scope_sha256 for item in stage_input.logical_scopes
        ),
        "logical_object_sha256s": tuple(
            item.parent_object_sha256 for item in stage_input.logical_objects
        ),
        "excluded_evidence_sha256s": tuple(
            item.evidence_sha256
            for item in (
                *stage_input.nested_region_evidence,
                *stage_input.non_business_region_evidence,
            )
        ),
        "unresolved_scope_sha256s": tuple(
            item.unresolved_scope_sha256 for item in stage_input.unresolved_scopes
        ),
        "physical_partition_sha256s": tuple(
            item.partition_sha256 for item in stage_input.physical_partitions
        ),
        "supplemental_evidence_sha256s": tuple(
            item.residual_evidence_sha256
            for item in stage_input.supplemental_table_evidence
        ),
    }
    receipt_draft = D2TableRegionParentInputReceiptV1.model_construct(
        **receipt_values,
        receipt_sha256="0" * 64,
    )
    receipt = D2TableRegionParentInputReceiptV1(
        **receipt_values,
        receipt_sha256=stable_d2_table_region_parent_input_receipt_sha256(
            **receipt_draft.model_dump(
                mode="json",
                exclude={"receipt_sha256"},
            )
        ),
    )
    return D2TableRegionParentInputCompilationV1(
        stage_input=stage_input,
        receipt=receipt,
    )


class _PhysicalTable:
    def __init__(self, node: SourceNode, table: TableIR) -> None:
        self.node = node
        self.table = table
        self.table_ir_sha256 = _canonical_sha256(table.model_dump(mode="json"))
        self.cells = tuple(
            sorted(table.cells, key=lambda item: (item.row, item.column, item.cell_id))
        )


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> tuple[_PhysicalTable, ...]:
    result: list[_PhysicalTable] = []
    seen: set[str] = set()
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE:
            continue
        table = node.table_payload
        if table is None:
            raise ValueError("SourceIR table node lost its TableIR payload")
        if table.table_id in seen:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        seen.add(table.table_id)
        result.append(_PhysicalTable(node, table))
    return tuple(result)


def _resolve_region_cells(
    *,
    source: SourceIRSnapshot,
    source_canonical_sha256: str,
    physical: _PhysicalTable,
    region: D2CompiledTableRegionV1,
) -> tuple[D2TableRegionVerbatimCellV1, ...]:
    if (
        region.source_ir_id != source.source_ir_id
        or region.source_sha256 != source.source_sha256
        or region.source_ir_canonical_sha256 != source_canonical_sha256
        or region.physical_table_id != physical.table.table_id
        or region.table_source_node_id != physical.node.source_node_id
        or region.physical_table_source_order != physical.node.source_order
    ):
        raise ValueError("compiled table region SourceIR binding drifted")
    values: list[D2TableRegionVerbatimCellV1] = []
    for ref in region.cell_refs:
        if ref.canonical_cell_ordinal >= len(physical.cells):
            raise ValueError("compiled region cell ordinal exceeds SourceIR table")
        cell = physical.cells[ref.canonical_cell_ordinal]
        _validate_cell_ref(
            source=source,
            source_canonical_sha256=source_canonical_sha256,
            physical=physical,
            ref=ref,
            cell=cell,
        )
        cell_values = {
            "canonical_cell_ordinal": ref.canonical_cell_ordinal,
            "cell_id": cell.cell_id,
            "row": cell.row,
            "column": cell.column,
            "row_span": cell.row_span,
            "column_span": cell.column_span,
            "page_number": ref.page_number,
            "bbox": ref.bbox,
            "raw_text": cell.raw_text,
            "raw_text_sha256": ref.raw_text_sha256,
            "cell_evidence_sha256": ref.cell_evidence_sha256,
        }
        values.append(
            D2TableRegionVerbatimCellV1(
                **cell_values,
                verbatim_cell_sha256=(
                    stable_d2_table_region_verbatim_cell_sha256(**cell_values)
                ),
            )
        )
    return tuple(values)


def _validate_cell_ref(
    *,
    source: SourceIRSnapshot,
    source_canonical_sha256: str,
    physical: _PhysicalTable,
    ref: D2TableCellSourceRefV1,
    cell: TableCellIR,
) -> None:
    expected_page = (
        cell.native_locator.page_number or physical.node.native_locator.page_number
    )
    expected_locator_sha = _canonical_sha256(
        cell.native_locator.model_dump(mode="json", exclude_none=True)
    )
    expected = {
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_ir_canonical_sha256": source_canonical_sha256,
        "table_source_node_id": physical.node.source_node_id,
        "physical_table_id": physical.table.table_id,
        "table_ir_sha256": physical.table_ir_sha256,
        "cell_id": cell.cell_id,
        "row": cell.row,
        "column": cell.column,
        "row_span": cell.row_span,
        "column_span": cell.column_span,
        "page_number": expected_page,
        "bbox": cell.native_locator.bbox,
        "native_locator_sha256": expected_locator_sha,
        "raw_text_sha256": _text_sha256(cell.raw_text),
        "formula_sha256": _optional_text_sha256(cell.formula),
        "display_value_sha256": _optional_text_sha256(cell.display_value),
    }
    actual = ref.model_dump(
        mode="python",
        exclude={"canonical_cell_ordinal", "cell_evidence_sha256"},
    )
    if actual != expected:
        raise ValueError("compiled table cell evidence drifted from SourceIR")


def _content_values(
    cells: tuple[D2TableRegionVerbatimCellV1, ...],
) -> dict[str, object]:
    source_text = "".join(cell.raw_text for cell in cells)
    return {
        "cell_ids": tuple(cell.cell_id for cell in cells),
        "verbatim_cells": cells,
        "source_text": source_text,
        "source_text_sha256": _text_sha256(source_text),
        "evidence_refs": tuple(cell.cell_evidence_sha256 for cell in cells),
    }


def _build_parent_scope(
    scope: object,
    *,
    segments_by_region_id: dict[str, tuple[D2TableRegionSourceSegmentV1, ...]],
    nested_region_ids: tuple[str, ...],
) -> D2TableRegionParentScopeV1:
    ordered_region_ids = tuple(scope.ordered_region_ids)
    values = {
        "logical_scope_id": scope.logical_scope_id,
        "candidate_logical_scope_key": scope.candidate_scope_key,
        "compiled_scope_sha256": scope.scope_sha256,
        "ordered_region_ids": ordered_region_ids,
        "ordered_segment_ids": tuple(
            segment.segment_id
            for region_id in ordered_region_ids
            for segment in segments_by_region_id[region_id]
        ),
        "nested_region_ids": nested_region_ids,
    }
    return D2TableRegionParentScopeV1(
        **values,
        parent_scope_sha256=stable_d2_table_region_parent_scope_sha256(**values),
    )


def _build_parent_object(
    logical_object: object,
    *,
    segments_by_region_id: dict[str, tuple[D2TableRegionSourceSegmentV1, ...]],
    nested_region_ids: tuple[str, ...],
) -> D2TableRegionParentObjectV1:
    ordered_region_ids = tuple(logical_object.ordered_region_ids)
    values = {
        "logical_object_id": logical_object.logical_object_id,
        "candidate_logical_object_key": logical_object.candidate_object_key,
        "compiled_object_sha256": logical_object.object_sha256,
        "ordered_region_ids": ordered_region_ids,
        "ordered_segment_ids": tuple(
            segment.segment_id
            for region_id in ordered_region_ids
            for segment in segments_by_region_id[region_id]
        ),
        "nested_region_ids": nested_region_ids,
    }
    return D2TableRegionParentObjectV1(
        **values,
        parent_object_sha256=stable_d2_table_region_parent_object_sha256(**values),
    )


def _build_physical_partition(
    coverage: D2PhysicalTableCellCoverageV1,
) -> D2TableRegionPhysicalCellPartitionV1:
    values = {
        "physical_table_id": coverage.physical_table_id,
        "table_source_node_id": coverage.table_source_node_id,
        "physical_table_source_order": coverage.physical_table_source_order,
        "table_ir_sha256": coverage.table_ir_sha256,
        "canonical_cell_ids": tuple(coverage.canonical_cell_ids),
        "ordered_region_ids": tuple(coverage.ordered_region_ids),
        "empty_table_disposition": coverage.empty_table_disposition,
        "empty_table_reason_codes": coverage.empty_table_reason_codes,
        "cell_coverage_sha256": coverage.cell_coverage_sha256,
    }
    return D2TableRegionPhysicalCellPartitionV1(
        **values,
        partition_sha256=stable_d2_table_region_physical_partition_sha256(**values),
    )


def _build_supplemental_evidence(
    physical: _PhysicalTable,
    coverage: D2PhysicalTableCellCoverageV1,
) -> D2TableRegionSupplementalEvidenceV1:
    values = {
        "physical_table_id": coverage.physical_table_id,
        "table_source_node_id": coverage.table_source_node_id,
        "physical_table_source_order": coverage.physical_table_source_order,
        "table_ir_sha256": coverage.table_ir_sha256,
        "table_node_raw_text": physical.node.raw_text,
        "table_node_raw_text_sha256": coverage.table_node_raw_text_sha256,
        "table_notes": physical.table.notes,
        "table_notes_sha256": coverage.table_notes_sha256,
        "supplemental_visual_evidence": coverage.supplemental_visual_evidence,
        "semantic_scope": "SUPPLEMENTAL_NOT_CELL_DISPOSITIONED",
        "empty_table_disposition": coverage.empty_table_disposition,
        "empty_table_reason_codes": coverage.empty_table_reason_codes,
    }
    return D2TableRegionSupplementalEvidenceV1(
        **values,
        residual_evidence_sha256=(
            stable_d2_table_region_supplemental_evidence_sha256(**values)
        ),
    )


def _validate_content_evidence(
    *,
    cell_ids: tuple[str, ...],
    verbatim_cells: tuple[D2TableRegionVerbatimCellV1, ...],
    source_text: str,
    source_text_sha256: str,
    evidence_refs: tuple[str, ...],
    label: str,
) -> None:
    ordinals = tuple(item.canonical_cell_ordinal for item in verbatim_cells)
    if ordinals != tuple(sorted(set(ordinals))):
        raise ValueError(f"{label} cells must be unique and canonical")
    if cell_ids != tuple(item.cell_id for item in verbatim_cells):
        raise ValueError(f"{label} cell IDs differ from verbatim evidence")
    if len(cell_ids) != len(set(cell_ids)):
        raise ValueError(f"{label} cannot repeat a SourceIR cell")
    if source_text != "".join(item.raw_text for item in verbatim_cells):
        raise ValueError(f"{label} text must concatenate exact cells without edits")
    if _text_sha256(source_text) != source_text_sha256:
        raise ValueError(f"{label} source text SHA mismatch")
    if evidence_refs != tuple(item.cell_evidence_sha256 for item in verbatim_cells):
        raise ValueError(f"{label} evidence refs must follow exact cell order")


def _ordered_unique(values: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def stable_d2_table_region_verbatim_cell_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-verbatim-cell/v1", **values}
    )


def stable_d2_table_region_source_segment_id(**values: object) -> str:
    return _stable_id("d2-table-region-source-segment", values)


def stable_d2_table_region_source_segment_sha256(**values: object) -> str:
    return _canonical_sha256(values)


def stable_d2_table_region_parent_scope_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-parent-scope/v1", **values}
    )


def stable_d2_table_region_parent_object_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-parent-object/v1", **values}
    )


def stable_d2_table_region_excluded_evidence_id(**values: object) -> str:
    return _stable_id("d2-table-region-excluded-evidence", values)


def stable_d2_table_region_excluded_evidence_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-excluded-evidence/v1", **values}
    )


def stable_d2_table_region_unresolved_scope_id(**values: object) -> str:
    return _stable_id("d2-table-region-unresolved-scope", values)


def stable_d2_table_region_unresolved_scope_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-unresolved-scope/v1", **values}
    )


def stable_d2_table_region_physical_partition_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-physical-partition/v1", **values}
    )


def stable_d2_table_region_supplemental_evidence_sha256(**values: object) -> str:
    return _canonical_sha256(
        {"schema_version": "d2-table-region-supplemental-evidence/v1", **values}
    )


def stable_d2_table_region_parent_input_id(**values: object) -> str:
    return _stable_id("d2-table-region-parent-input", values)


def stable_d2_table_region_parent_input_sha256(**values: object) -> str:
    return _canonical_sha256(values)


def stable_d2_table_region_parent_input_receipt_sha256(**values: object) -> str:
    return _canonical_sha256(values)


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}-{_canonical_sha256(value)[:32]}"


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_text_sha256(value: str | None) -> str | None:
    return None if value is None else _text_sha256(value)


def _require_sha256(value: str, field_name: str) -> None:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


__all__ = [
    "D2_TABLE_REGION_PARENT_INPUT_ADAPTER_ID",
    "D2_TABLE_REGION_PARENT_INPUT_ADAPTER_VERSION",
    "D2_TABLE_REGION_PARENT_INPUT_RECEIPT_SCHEMA_VERSION",
    "D2_TABLE_REGION_PARENT_INPUT_SCHEMA_VERSION",
    "D2_TABLE_REGION_SOURCE_SEGMENT_SCHEMA_VERSION",
    "D2TableRegionExcludedEvidenceV1",
    "D2TableRegionParentInputCompilationV1",
    "D2TableRegionParentInputReceiptV1",
    "D2TableRegionParentInputStatus",
    "D2TableRegionParentInputV1",
    "D2TableRegionParentObjectV1",
    "D2TableRegionParentScopeV1",
    "D2TableRegionPhysicalCellPartitionV1",
    "D2TableRegionSourceSegmentV1",
    "D2TableRegionSupplementalEvidenceV1",
    "D2TableRegionUnresolvedScopeV1",
    "D2TableRegionVerbatimCellV1",
    "build_d2_table_region_parent_input",
    "stable_d2_table_region_excluded_evidence_id",
    "stable_d2_table_region_excluded_evidence_sha256",
    "stable_d2_table_region_parent_input_id",
    "stable_d2_table_region_parent_input_receipt_sha256",
    "stable_d2_table_region_parent_input_sha256",
    "stable_d2_table_region_parent_object_sha256",
    "stable_d2_table_region_parent_scope_sha256",
    "stable_d2_table_region_physical_partition_sha256",
    "stable_d2_table_region_source_segment_id",
    "stable_d2_table_region_source_segment_sha256",
    "stable_d2_table_region_supplemental_evidence_sha256",
    "stable_d2_table_region_unresolved_scope_id",
    "stable_d2_table_region_unresolved_scope_sha256",
    "stable_d2_table_region_verbatim_cell_sha256",
]
