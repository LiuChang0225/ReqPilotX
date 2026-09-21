from __future__ import annotations

import hashlib
import json
import re
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

from reqpilotx_server.application.agent_workflow.d2_logical_structure import (
    D2LogicalStructureStatus,
    D2SourceHandleKind,
    stable_d2_source_segment_sha256,
)
from reqpilotx_server.application.agent_workflow.d2_decision_candidate import (
    d2_source_node_content_sha256,
)
from reqpilotx_server.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementDisposition,
    D2LogicalStructureProposalV2,
    D2LogicalStructureValidationReceiptV2,
    D2TableSupplementalDispositionKindV1,
    D2TableSupplementalEvidenceKindV1,
    D2V2BaseSourceSegmentRefV1,
    D2V2TableRegionSourceSegmentRefV1,
)
from reqpilotx_server.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionExcludedEvidenceV1,
    D2TableRegionParentInputCompilationV1,
    D2TableRegionParentInputReceiptV1,
    D2TableRegionParentInputStatus,
    D2TableRegionParentInputV1,
    D2TableRegionSourceSegmentV1,
    D2TableRegionUnresolvedScopeV1,
)
from reqpilotx_server.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionKind,
    D2TableSupplementalEvidenceRefV1,
)
from reqpilotx_server.application.extraction.agent_logical_table_scope import (
    source_ir_canonical_sha256,
)
from reqpilotx_server.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
)


D3_REGION_STAGE_INPUT_LINEAGE_SCHEMA_VERSION = (
    "reqpilotx-d3-region-stage-input-lineage/v1"
)
D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION = (
    "reqpilotx-d3-unified-exact-source-segment/v3"
)
D3_UNIFIED_EXACT_SOURCE_SEGMENT_SET_SCHEMA_VERSION = (
    "reqpilotx-d3-unified-exact-source-segment-set/v3"
)
D3_REGION_STAGE_INPUT_MANIFEST_SCHEMA_VERSION = (
    "reqpilotx-d3-region-stage-input-manifest/v1"
)

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class D3RegionStageInputError(ValueError):
    """Fail-closed error raised at the region-capable parent input boundary."""


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D3RegionStageInputStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


class D3UnifiedEvidenceKindV3(StrEnum):
    BASE_TEXT = "BASE_TEXT"
    TABLE_CELL = "TABLE_CELL"
    TABLE_NODE_RAW_TEXT = "TABLE_NODE_RAW_TEXT"
    TABLE_NOTE = "TABLE_NOTE"


class D3UnifiedSegmentRoleV3(StrEnum):
    PRIMARY_EXACT = "PRIMARY_EXACT"
    OWNED_CONTEXT = "OWNED_CONTEXT"


class D3EvidenceLedgerKindV1(StrEnum):
    NESTED_REGION = "NESTED_REGION"
    NON_BUSINESS_REGION = "NON_BUSINESS_REGION"
    UNRESOLVED_REGION = "UNRESOLVED_REGION"
    TABLE_SUPPLEMENTAL = "TABLE_SUPPLEMENTAL"
    EMPTY_TABLE = "EMPTY_TABLE"


class D3UnresolvedScopeKindV1(StrEnum):
    REGION = "REGION"
    SUPPLEMENTAL = "SUPPLEMENTAL"
    EMPTY_TABLE = "EMPTY_TABLE"
    PREDECESSOR = "PREDECESSOR"


_EVIDENCE_KIND_RANK = {
    D3UnifiedEvidenceKindV3.BASE_TEXT: 0,
    D3UnifiedEvidenceKindV3.TABLE_CELL: 10,
    D3UnifiedEvidenceKindV3.TABLE_NODE_RAW_TEXT: 20,
    D3UnifiedEvidenceKindV3.TABLE_NOTE: 30,
}


class D3RegionStageInputLineageV1(_StrictFrozenModel):
    """Out-of-band, content-free pins for one validated V2 materialization."""

    schema_version: Literal["reqpilotx-d3-region-stage-input-lineage/v1"] = (
        D3_REGION_STAGE_INPUT_LINEAGE_SCHEMA_VERSION
    )
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    base_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    base_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    region_artifact_id: str = Field(pattern=r"^d2-table-region-artifact-[0-9a-f]{32}$")
    region_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    v2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    v2_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    v2_validation_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    parent_input_id: str = Field(pattern=r"^d2-table-region-parent-input-[0-9a-f]{32}$")
    parent_input_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    parent_input_receipt_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    lineage_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_lineage(self) -> D3RegionStageInputLineageV1:
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"lineage_sha256"})
        )
        if self.lineage_sha256 != expected:
            raise ValueError("region stage-input lineage SHA drifted")
        return self


class D3UnifiedExactSourceSegmentV3(_StrictFrozenModel):
    """One Host-resolved exact text fragment in global native source order."""

    schema_version: Literal["reqpilotx-d3-unified-exact-source-segment/v3"] = (
        D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION
    )
    segment_id: str = Field(min_length=1, max_length=240)
    logical_object_id: str = Field(min_length=1, max_length=200)
    segment_role: D3UnifiedSegmentRoleV3
    evidence_kind: D3UnifiedEvidenceKindV3
    source_position: tuple[StrictInt, StrictInt, StrictInt, StrictInt]
    source_node_id: str = Field(min_length=1, max_length=160)
    source_node_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = ()
    intra_node_ordinal: StrictInt = Field(ge=0)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=0)
    source_text: str = Field(max_length=1_000_000)
    source_text_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_content_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    d2_v2_source_segment_id: str | None = Field(default=None, max_length=200)
    d2_v2_source_segment_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN.pattern,
    )
    region_parent_segment_id: str | None = Field(default=None, max_length=200)
    region_parent_segment_sha256: str | None = Field(
        default=None,
        pattern=_SHA256_PATTERN.pattern,
    )
    supplemental_evidence_id: str | None = Field(default=None, max_length=200)
    evidence_refs: tuple[str, ...] = ()
    segment_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("page_numbers")
    @classmethod
    def validate_pages(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if value != tuple(sorted(set(value))) or any(page < 1 for page in value):
            raise ValueError("unified source-segment pages must be canonical")
        return value

    @field_validator("evidence_refs")
    @classmethod
    def validate_refs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("unified source-segment evidence refs must be unique")
        return value

    @model_validator(mode="after")
    def validate_segment(self) -> D3UnifiedExactSourceSegmentV3:
        expected_position = (
            self.source_node_order,
            _EVIDENCE_KIND_RANK[self.evidence_kind],
            self.intra_node_ordinal,
            self.start_offset or 0,
        )
        if self.source_position != expected_position:
            raise ValueError("unified source position is not Host canonical")
        if _text_sha256(self.source_text) != self.source_text_sha256:
            raise ValueError("unified exact source text SHA mismatch")
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("unified source offsets must be supplied together")
        if self.start_offset is not None:
            assert self.end_offset is not None
            if self.end_offset < self.start_offset:
                raise ValueError("unified source offsets are reversed")
            if self.end_offset - self.start_offset != len(self.source_text):
                raise ValueError("unified source range length differs from exact text")
        base_bound = (
            self.d2_v2_source_segment_id is not None
            and self.d2_v2_source_segment_sha256 is not None
        )
        region_bound = (
            self.region_parent_segment_id is not None
            and self.region_parent_segment_sha256 is not None
        )
        supplemental_bound = self.supplemental_evidence_id is not None
        if (self.d2_v2_source_segment_id is None) != (
            self.d2_v2_source_segment_sha256 is None
        ) or (self.region_parent_segment_id is None) != (
            self.region_parent_segment_sha256 is None
        ):
            raise ValueError("unified source lineage fields must be paired")
        if self.segment_role is D3UnifiedSegmentRoleV3.PRIMARY_EXACT:
            if not base_bound or supplemental_bound:
                raise ValueError("primary exact segments require V2 source lineage")
            if self.evidence_kind is D3UnifiedEvidenceKindV3.TABLE_CELL:
                if not region_bound:
                    raise ValueError("table cells require exact region-parent lineage")
            elif region_bound:
                raise ValueError("base text cannot claim region-parent lineage")
        else:
            if base_bound or region_bound or not supplemental_bound:
                raise ValueError(
                    "owned context requires only supplemental evidence lineage"
                )
            if self.evidence_kind not in {
                D3UnifiedEvidenceKindV3.TABLE_NODE_RAW_TEXT,
                D3UnifiedEvidenceKindV3.TABLE_NOTE,
            }:
                raise ValueError("owned context must be table raw text or a note")
        expected_sha = _canonical_sha256(
            self.model_dump(mode="json", exclude={"segment_sha256"})
        )
        if self.segment_sha256 != expected_sha:
            raise ValueError("unified source-segment SHA drifted")
        return self


class D3LogicalObjectSegmentScopeV3(_StrictFrozenModel):
    logical_object_id: str = Field(min_length=1, max_length=200)
    parent_logical_object_id: str | None = Field(default=None, max_length=200)
    object_kind: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")
    object_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_order: StrictInt = Field(ge=0)
    ordered_segment_ids: tuple[str, ...] = ()
    primary_segment_ids: tuple[str, ...] = ()
    context_segment_ids: tuple[str, ...] = ()
    nested_evidence_ids: tuple[str, ...] = ()
    supplemental_evidence_ids: tuple[str, ...] = ()
    scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_scope(self) -> D3LogicalObjectSegmentScopeV3:
        for values in (
            self.ordered_segment_ids,
            self.primary_segment_ids,
            self.context_segment_ids,
            self.nested_evidence_ids,
            self.supplemental_evidence_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("logical-object scope ledgers must be unique")
        if set(self.primary_segment_ids).intersection(self.context_segment_ids):
            raise ValueError("primary and context segment ledgers must be disjoint")
        if set(self.ordered_segment_ids) != {
            *self.primary_segment_ids,
            *self.context_segment_ids,
        }:
            raise ValueError("ordered segments must exactly cover primary and context")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"scope_sha256"})
        )
        if self.scope_sha256 != expected:
            raise ValueError("logical-object segment scope SHA drifted")
        return self


class D3EvidenceLedgerEntryV1(_StrictFrozenModel):
    evidence_id: str = Field(min_length=1, max_length=240)
    evidence_kind: D3EvidenceLedgerKindV1
    disposition: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    physical_table_id: str = Field(min_length=1, max_length=160)
    table_source_node_id: str = Field(min_length=1, max_length=160)
    source_node_order: StrictInt = Field(ge=0)
    region_id: str | None = Field(default=None, max_length=200)
    ultimate_owner_logical_object_id: str | None = Field(
        default=None,
        max_length=200,
    )
    owner_segment_ids: tuple[str, ...] = ()
    text_fragments: tuple[str, ...] = ()
    text_fragment_sha256s: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    derived_cell_ids: tuple[str, ...] = ()
    selected_parent_assignment_eligible: bool = False
    source_evidence_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ledger_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator(
        "owner_segment_ids",
        "evidence_refs",
        "derived_cell_ids",
    )
    @classmethod
    def validate_unique_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("evidence ledgers require unique non-empty values")
        return value

    @model_validator(mode="after")
    def validate_ledger(self) -> D3EvidenceLedgerEntryV1:
        if len(self.text_fragments) != len(self.text_fragment_sha256s) or any(
            _text_sha256(text) != digest
            for text, digest in zip(
                self.text_fragments,
                self.text_fragment_sha256s,
                strict=True,
            )
        ):
            raise ValueError("evidence text fragments are not exactly hash-bound")
        if (self.ultimate_owner_logical_object_id is None) != (
            not self.owner_segment_ids
        ):
            raise ValueError("owned evidence must bind its ultimate owner segments")
        if self.selected_parent_assignment_eligible and (
            self.evidence_kind is not D3EvidenceLedgerKindV1.NESTED_REGION
            or self.ultimate_owner_logical_object_id is None
        ):
            raise ValueError(
                "selected-parent assignment is reserved for owned nested evidence"
            )
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"ledger_sha256"})
        )
        if self.ledger_sha256 != expected:
            raise ValueError("evidence ledger SHA drifted")
        return self


class D3UnresolvedEvidenceScopeV1(_StrictFrozenModel):
    unresolved_scope_id: str = Field(
        pattern=r"^d3-region-unresolved-scope-[0-9a-f]{32}$"
    )
    scope_kind: D3UnresolvedScopeKindV1
    source_node_order: StrictInt = Field(ge=0)
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=32)
    scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("unresolved evidence IDs must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("unresolved reason codes must be canonical")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> D3UnresolvedEvidenceScopeV1:
        expected = _canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"unresolved_scope_id", "scope_sha256"},
            )
        )
        if self.scope_sha256 != expected:
            raise ValueError("unresolved evidence scope SHA drifted")
        expected_id = f"d3-region-unresolved-scope-{expected[:32]}"
        if self.unresolved_scope_id != expected_id:
            raise ValueError("unresolved evidence scope ID drifted")
        return self


class D3UnifiedExactSourceSegmentSetV3(_StrictFrozenModel):
    """Private content-bearing parent-stage input compiled from V2 plus regions."""

    schema_version: Literal["reqpilotx-d3-unified-exact-source-segment-set/v3"] = (
        D3_UNIFIED_EXACT_SOURCE_SEGMENT_SET_SCHEMA_VERSION
    )
    content_bearing: Literal[True] = True
    private_stage_input_only: Literal[True] = True
    run_id: str = Field(pattern=r"^agent-run-[0-9a-f]{32}$")
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    source_ir_canonical_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    kit_bundle_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    predecessor_v2_artifact_id: str = Field(pattern=r"^agent-artifact-[0-9a-f]{32}$")
    predecessor_v2_artifact_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    parent_input_id: str = Field(pattern=r"^d2-table-region-parent-input-[0-9a-f]{32}$")
    parent_input_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    status: D3RegionStageInputStatus
    source_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    segments: tuple[D3UnifiedExactSourceSegmentV3, ...] = ()
    logical_objects: tuple[D3LogicalObjectSegmentScopeV3, ...] = ()
    evidence_ledger: tuple[D3EvidenceLedgerEntryV1, ...] = ()
    unresolved_scopes: tuple[D3UnresolvedEvidenceScopeV1, ...] = ()
    segment_set_id: str = Field(pattern=r"^d3-region-segment-set-[0-9a-f]{32}$")
    segment_set_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_segment_set(self) -> D3UnifiedExactSourceSegmentSetV3:
        segment_ids = tuple(item.segment_id for item in self.segments)
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("unified source segment IDs must be globally unique")
        positions = tuple(item.source_position for item in self.segments)
        if positions != tuple(sorted(positions)) or len(positions) != len(
            set(positions)
        ):
            raise ValueError("unified source positions must be globally strict")
        object_ids = tuple(item.logical_object_id for item in self.logical_objects)
        if len(object_ids) != len(set(object_ids)):
            raise ValueError("unified logical-object scopes must be unique")
        segment_by_id = {item.segment_id: item for item in self.segments}
        assigned = tuple(
            segment_id
            for logical_object in self.logical_objects
            for segment_id in logical_object.ordered_segment_ids
        )
        if len(assigned) != len(set(assigned)) or set(assigned) != set(segment_ids):
            raise ValueError("every unified source segment requires one object owner")
        for logical_object in self.logical_objects:
            members = tuple(
                segment_by_id[item] for item in logical_object.ordered_segment_ids
            )
            if any(
                item.logical_object_id != logical_object.logical_object_id
                for item in members
            ) or tuple(item.source_position for item in members) != tuple(
                sorted(item.source_position for item in members)
            ):
                raise ValueError("logical-object segments lost Host source order")
            if logical_object.primary_segment_ids != tuple(
                item.segment_id
                for item in members
                if item.segment_role is D3UnifiedSegmentRoleV3.PRIMARY_EXACT
            ) or logical_object.context_segment_ids != tuple(
                item.segment_id
                for item in members
                if item.segment_role is D3UnifiedSegmentRoleV3.OWNED_CONTEXT
            ):
                raise ValueError("logical-object role ledgers differ from segments")
        ledger_ids = tuple(item.evidence_id for item in self.evidence_ledger)
        if len(ledger_ids) != len(set(ledger_ids)):
            raise ValueError("evidence ledger IDs must be globally unique")
        for item in self.evidence_ledger:
            if any(
                segment_id not in segment_by_id for segment_id in item.owner_segment_ids
            ):
                raise ValueError("evidence ledger references an unknown owner segment")
            if item.ultimate_owner_logical_object_id is not None and any(
                segment_by_id[segment_id].logical_object_id
                != item.ultimate_owner_logical_object_id
                for segment_id in item.owner_segment_ids
            ):
                raise ValueError("evidence ledger owner segment belongs elsewhere")
        unresolved_ids = tuple(
            item.unresolved_scope_id for item in self.unresolved_scopes
        )
        if len(unresolved_ids) != len(set(unresolved_ids)):
            raise ValueError("unresolved scopes must be unique")
        expected_status = (
            D3RegionStageInputStatus.PARTIAL
            if self.unresolved_scopes
            else D3RegionStageInputStatus.COMPLETE
        )
        if self.status is not expected_status:
            raise ValueError("region stage-input status hides unresolved evidence")
        expected_scope_sha = _source_scope_sha256(
            self.segments,
            self.logical_objects,
            self.evidence_ledger,
            self.unresolved_scopes,
        )
        if self.source_scope_sha256 != expected_scope_sha:
            raise ValueError("unified source scope SHA drifted")
        expected_sha = _canonical_sha256(
            self.model_dump(
                mode="json",
                exclude={"segment_set_id", "segment_set_sha256"},
            )
        )
        if self.segment_set_sha256 != expected_sha:
            raise ValueError("unified source-segment set SHA drifted")
        if self.segment_set_id != f"d3-region-segment-set-{expected_sha[:32]}":
            raise ValueError("unified source-segment set ID drifted")
        return self


class D3RegionStageInputManifestV1(_StrictFrozenModel):
    """Source-text-free publication identity for one in-memory input bundle."""

    schema_version: Literal["reqpilotx-d3-region-stage-input-manifest/v1"] = (
        D3_REGION_STAGE_INPUT_MANIFEST_SCHEMA_VERSION
    )
    content_free: Literal[True] = True
    manifest_id: str = Field(pattern=r"^d3-region-stage-input-[0-9a-f]{32}$")
    lineage: D3RegionStageInputLineageV1
    status: D3RegionStageInputStatus
    source_scope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    segment_set_id: str = Field(pattern=r"^d3-region-segment-set-[0-9a-f]{32}$")
    segment_set_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    ordered_logical_object_ids: tuple[str, ...] = ()
    ordered_segment_ids: tuple[str, ...] = ()
    logical_object_scope_sha256s: tuple[str, ...] = ()
    evidence_ledger_sha256s: tuple[str, ...] = ()
    unresolved_scope_sha256s: tuple[str, ...] = ()
    manifest_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="after")
    def validate_manifest(self) -> D3RegionStageInputManifestV1:
        for values in (
            self.ordered_logical_object_ids,
            self.ordered_segment_ids,
            self.logical_object_scope_sha256s,
            self.evidence_ledger_sha256s,
            self.unresolved_scope_sha256s,
        ):
            if len(values) != len(set(values)):
                raise ValueError("region stage-input manifest ledgers must be unique")
        expected = _canonical_sha256(
            self.model_dump(mode="json", exclude={"manifest_id", "manifest_sha256"})
        )
        if self.manifest_sha256 != expected:
            raise ValueError("region stage-input manifest SHA drifted")
        if self.manifest_id != f"d3-region-stage-input-{expected[:32]}":
            raise ValueError("region stage-input manifest ID drifted")
        return self


class D3RegionStageInputCompilationV1(_StrictFrozenModel):
    segment_set: D3UnifiedExactSourceSegmentSetV3
    manifest: D3RegionStageInputManifestV1

    @model_validator(mode="after")
    def validate_compilation(self) -> D3RegionStageInputCompilationV1:
        source = self.segment_set
        manifest = self.manifest
        if (
            manifest.status is not source.status
            or manifest.source_scope_sha256 != source.source_scope_sha256
            or manifest.segment_set_id != source.segment_set_id
            or manifest.segment_set_sha256 != source.segment_set_sha256
            or manifest.ordered_logical_object_ids
            != tuple(item.logical_object_id for item in source.logical_objects)
            or manifest.ordered_segment_ids
            != tuple(item.segment_id for item in source.segments)
            or manifest.logical_object_scope_sha256s
            != tuple(item.scope_sha256 for item in source.logical_objects)
            or manifest.evidence_ledger_sha256s
            != tuple(item.ledger_sha256 for item in source.evidence_ledger)
            or manifest.unresolved_scope_sha256s
            != tuple(item.scope_sha256 for item in source.unresolved_scopes)
        ):
            raise ValueError("region stage-input manifest differs from source bundle")
        return self


def build_d3_region_stage_input_lineage(
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    parent_input: D2TableRegionParentInputV1,
    parent_input_receipt: D2TableRegionParentInputReceiptV1,
) -> D3RegionStageInputLineageV1:
    values = _lineage_values(
        proposal=proposal,
        validation_receipt=validation_receipt,
        parent_input=parent_input,
        parent_input_receipt=parent_input_receipt,
    )
    return D3RegionStageInputLineageV1(
        **values,
        lineage_sha256=_canonical_sha256(
            {
                "schema_version": D3_REGION_STAGE_INPUT_LINEAGE_SCHEMA_VERSION,
                **_json_value(values),
            }
        ),
    )


def materialize_d3_region_stage_input(
    source_ir: SourceIRSnapshot,
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    parent_input: D2TableRegionParentInputV1,
    parent_input_receipt: D2TableRegionParentInputReceiptV1,
    expected_lineage: D3RegionStageInputLineageV1,
) -> D3RegionStageInputCompilationV1:
    """Compile the region-aware, exact parent-stage input without filesystem I/O."""

    try:
        source = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
        v2 = D2LogicalStructureProposalV2.model_validate(
            proposal.model_dump(mode="json")
        )
        v2_receipt = D2LogicalStructureValidationReceiptV2.model_validate(
            validation_receipt.model_dump(mode="json")
        )
        region_input = D2TableRegionParentInputV1.model_validate(
            parent_input.model_dump(mode="json")
        )
        region_receipt = D2TableRegionParentInputReceiptV1.model_validate(
            parent_input_receipt.model_dump(mode="json")
        )
        lineage = D3RegionStageInputLineageV1.model_validate(
            expected_lineage.model_dump(mode="json")
        )
        D2TableRegionParentInputCompilationV1(
            stage_input=region_input,
            receipt=region_receipt,
        )
    except (TypeError, ValueError) as exc:
        raise D3RegionStageInputError(
            "D3_REGION_STAGE_INPUT_PREDECESSOR_INVALID"
        ) from exc

    _validate_closure(
        source,
        proposal=v2,
        validation_receipt=v2_receipt,
        parent_input=region_input,
        parent_input_receipt=region_receipt,
        expected_lineage=lineage,
    )

    segments, evidence_ledger, unresolved_scopes = _materialize_content(
        source,
        proposal=v2,
        parent_input=region_input,
    )
    segments_by_object: dict[str, list[D3UnifiedExactSourceSegmentV3]] = {
        item.logical_object_id: [] for item in v2.logical_objects
    }
    for segment in segments:
        segments_by_object[segment.logical_object_id].append(segment)
    evidence_by_owner: dict[str, list[D3EvidenceLedgerEntryV1]] = {}
    for evidence in evidence_ledger:
        if evidence.ultimate_owner_logical_object_id is not None:
            evidence_by_owner.setdefault(
                evidence.ultimate_owner_logical_object_id,
                [],
            ).append(evidence)

    logical_objects: list[D3LogicalObjectSegmentScopeV3] = []
    for item in v2.logical_objects:
        members = tuple(
            sorted(
                segments_by_object[item.logical_object_id],
                key=lambda segment: segment.source_position,
            )
        )
        evidence = tuple(evidence_by_owner.get(item.logical_object_id, ()))
        values = {
            "logical_object_id": item.logical_object_id,
            "parent_logical_object_id": item.parent_logical_object_id,
            "object_kind": item.object_kind.value,
            "object_sha256": item.object_sha256,
            "source_order": item.source_order,
            "ordered_segment_ids": tuple(segment.segment_id for segment in members),
            "primary_segment_ids": tuple(
                segment.segment_id
                for segment in members
                if segment.segment_role is D3UnifiedSegmentRoleV3.PRIMARY_EXACT
            ),
            "context_segment_ids": tuple(
                segment.segment_id
                for segment in members
                if segment.segment_role is D3UnifiedSegmentRoleV3.OWNED_CONTEXT
            ),
            "nested_evidence_ids": tuple(
                value.evidence_id
                for value in evidence
                if value.evidence_kind is D3EvidenceLedgerKindV1.NESTED_REGION
            ),
            "supplemental_evidence_ids": tuple(
                value.evidence_id
                for value in evidence
                if value.evidence_kind is D3EvidenceLedgerKindV1.TABLE_SUPPLEMENTAL
            ),
        }
        logical_objects.append(
            D3LogicalObjectSegmentScopeV3(
                **values,
                scope_sha256=_canonical_sha256(values),
            )
        )

    compiled_segments = tuple(sorted(segments, key=lambda item: item.source_position))
    compiled_objects = tuple(logical_objects)
    compiled_evidence = tuple(
        sorted(
            evidence_ledger,
            key=lambda item: (
                item.source_node_order,
                item.evidence_kind.value,
                item.evidence_id,
            ),
        )
    )
    compiled_unresolved = tuple(
        sorted(
            unresolved_scopes,
            key=lambda item: (
                item.source_node_order,
                item.scope_kind.value,
                item.unresolved_scope_id,
            ),
        )
    )
    status = (
        D3RegionStageInputStatus.PARTIAL
        if compiled_unresolved
        else D3RegionStageInputStatus.COMPLETE
    )
    source_scope_sha = _source_scope_sha256(
        compiled_segments,
        compiled_objects,
        compiled_evidence,
        compiled_unresolved,
    )
    set_values = {
        "content_bearing": True,
        "private_stage_input_only": True,
        "run_id": v2.run_id,
        "source_ir_id": source.source_ir_id,
        "source_sha256": source.source_sha256,
        "source_ir_canonical_sha256": source_ir_canonical_sha256(source),
        "kit_bundle_sha256": v2.kit_identity.bundle_sha256,
        "predecessor_v2_artifact_id": v2_receipt.artifact_id,
        "predecessor_v2_artifact_sha256": v2_receipt.artifact_sha256,
        "parent_input_id": region_input.stage_input_id,
        "parent_input_sha256": region_input.stage_input_sha256,
        "status": status,
        "source_scope_sha256": source_scope_sha,
        "segments": compiled_segments,
        "logical_objects": compiled_objects,
        "evidence_ledger": compiled_evidence,
        "unresolved_scopes": compiled_unresolved,
    }
    set_draft = D3UnifiedExactSourceSegmentSetV3.model_construct(
        **set_values,
        segment_set_id="d3-region-segment-set-" + "0" * 32,
        segment_set_sha256="0" * 64,
    )
    segment_set_sha = _canonical_sha256(
        set_draft.model_dump(
            mode="json",
            exclude={"segment_set_id", "segment_set_sha256"},
        )
    )
    segment_set = D3UnifiedExactSourceSegmentSetV3(
        **set_values,
        segment_set_id=f"d3-region-segment-set-{segment_set_sha[:32]}",
        segment_set_sha256=segment_set_sha,
    )
    manifest_values = {
        "content_free": True,
        "lineage": lineage,
        "status": segment_set.status,
        "source_scope_sha256": segment_set.source_scope_sha256,
        "segment_set_id": segment_set.segment_set_id,
        "segment_set_sha256": segment_set.segment_set_sha256,
        "ordered_logical_object_ids": tuple(
            item.logical_object_id for item in segment_set.logical_objects
        ),
        "ordered_segment_ids": tuple(item.segment_id for item in segment_set.segments),
        "logical_object_scope_sha256s": tuple(
            item.scope_sha256 for item in segment_set.logical_objects
        ),
        "evidence_ledger_sha256s": tuple(
            item.ledger_sha256 for item in segment_set.evidence_ledger
        ),
        "unresolved_scope_sha256s": tuple(
            item.scope_sha256 for item in segment_set.unresolved_scopes
        ),
    }
    manifest_draft = D3RegionStageInputManifestV1.model_construct(
        **manifest_values,
        manifest_id="d3-region-stage-input-" + "0" * 32,
        manifest_sha256="0" * 64,
    )
    manifest_sha = _canonical_sha256(
        manifest_draft.model_dump(
            mode="json",
            exclude={"manifest_id", "manifest_sha256"},
        )
    )
    manifest = D3RegionStageInputManifestV1(
        **manifest_values,
        manifest_id=f"d3-region-stage-input-{manifest_sha[:32]}",
        manifest_sha256=manifest_sha,
    )
    return D3RegionStageInputCompilationV1(
        segment_set=segment_set,
        manifest=manifest,
    )


def _validate_closure(
    source: SourceIRSnapshot,
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    parent_input: D2TableRegionParentInputV1,
    parent_input_receipt: D2TableRegionParentInputReceiptV1,
    expected_lineage: D3RegionStageInputLineageV1,
) -> None:
    if source.status is SourceIRStatus.FAILED:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_SOURCE_FAILED")
    source_canonical_sha = source_ir_canonical_sha256(source)
    if (
        proposal.source_ir_id != source.source_ir_id
        or proposal.source_sha256 != source.source_sha256
        or proposal.source_ir_canonical_sha256 != source_canonical_sha
        or parent_input.source_ir_id != source.source_ir_id
        or parent_input.source_sha256 != source.source_sha256
        or parent_input.source_ir_canonical_sha256 != source_canonical_sha
        or parent_input.source_ir_status is not source.status
    ):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_SOURCE_DRIFT")

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
    receipt_expected = {
        "run_id": proposal.run_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_id": proposal.source_ir_id,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_identity": proposal.kit_identity,
        "base_artifact_id": proposal.base_logical_structure.artifact_id,
        "base_artifact_sha256": proposal.base_logical_structure.artifact_sha256,
        "base_validation_receipt_sha256": (
            proposal.base_logical_structure.validation_receipt_sha256
        ),
        "table_region_artifact_id": proposal.table_region_artifact.artifact_id,
        "table_region_artifact_sha256": proposal.table_region_artifact.artifact_sha256,
        "region_placement_plan_sha256": proposal.region_placement_plan_sha256,
        "status": proposal.status,
        "logical_object_ids": tuple(
            item.logical_object_id for item in proposal.logical_objects
        ),
        "source_segment_ids": tuple(
            segment.segment_id
            for item in proposal.logical_objects
            for segment in item.source_segments
        ),
        "retained_base_logical_object_ids": proposal.retained_base_logical_object_ids,
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
    }
    for field_name, expected in receipt_expected.items():
        if getattr(validation_receipt, field_name) != expected:
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_V2_RECEIPT_DRIFT")
    if validation_receipt.artifact_sha256 != proposal.proposal_sha256:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_V2_RECEIPT_DRIFT")

    input_receipt_expected = {
        "source_ir_id": parent_input.source_ir_id,
        "source_sha256": parent_input.source_sha256,
        "source_ir_canonical_sha256": parent_input.source_ir_canonical_sha256,
        "kit_bundle_sha256": parent_input.kit_bundle_sha256,
        "d1_table_map_artifact_id": parent_input.d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": parent_input.d1_table_map_artifact_sha256,
        "region_artifact_id": parent_input.region_artifact_id,
        "region_artifact_sha256": parent_input.region_artifact_sha256,
        "stage_input_id": parent_input.stage_input_id,
        "stage_input_sha256": parent_input.stage_input_sha256,
        "source_segment_ids": tuple(
            item.segment_id for item in parent_input.source_segments
        ),
        "source_segment_sha256s": tuple(
            item.segment_sha256 for item in parent_input.source_segments
        ),
        "logical_scope_sha256s": tuple(
            item.parent_scope_sha256 for item in parent_input.logical_scopes
        ),
        "logical_object_sha256s": tuple(
            item.parent_object_sha256 for item in parent_input.logical_objects
        ),
        "excluded_evidence_sha256s": tuple(
            item.evidence_sha256
            for item in (
                *parent_input.nested_region_evidence,
                *parent_input.non_business_region_evidence,
            )
        ),
        "unresolved_scope_sha256s": tuple(
            item.unresolved_scope_sha256 for item in parent_input.unresolved_scopes
        ),
        "physical_partition_sha256s": tuple(
            item.partition_sha256 for item in parent_input.physical_partitions
        ),
        "supplemental_evidence_sha256s": tuple(
            item.residual_evidence_sha256
            for item in parent_input.supplemental_table_evidence
        ),
    }
    for field_name, expected in input_receipt_expected.items():
        if getattr(parent_input_receipt, field_name) != expected:
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_PARENT_RECEIPT_DRIFT")

    if (
        proposal.kit_identity.bundle_sha256 != parent_input.kit_bundle_sha256
        or proposal.table_region_artifact.artifact_id != parent_input.region_artifact_id
        or proposal.table_region_artifact.artifact_sha256
        != parent_input.region_artifact_sha256
        or proposal.table_region_artifact.status
        is not parent_input.region_artifact_status
    ):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_REGION_DRIFT")
    actual_lineage = build_d3_region_stage_input_lineage(
        proposal=proposal,
        validation_receipt=validation_receipt,
        parent_input=parent_input,
        parent_input_receipt=parent_input_receipt,
    )
    if actual_lineage != expected_lineage:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_LINEAGE_DRIFT")

    nested = tuple(item.region_id for item in parent_input.nested_region_evidence)
    non_business = tuple(
        item.region_id for item in parent_input.non_business_region_evidence
    )
    unresolved = tuple(item.region_id for item in parent_input.unresolved_scopes)
    unresolved_empty = tuple(
        item.physical_table_id
        for item in parent_input.physical_partitions
        if item.empty_table_disposition
        is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
    )
    unresolved_supplemental = tuple(
        disposition.evidence_id
        for table in proposal.physical_table_evidence
        for disposition in table.supplemental_dispositions
        if disposition.disposition is D2TableSupplementalDispositionKindV1.UNRESOLVED
    )
    if (
        nested != proposal.nested_region_ids
        or non_business != proposal.false_region_ids
        or unresolved != proposal.unresolved_region_ids
        or unresolved_empty != proposal.unresolved_empty_table_ids
        or unresolved_supplemental != proposal.unresolved_supplemental_evidence_ids
    ):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_UNRESOLVED_MISMATCH")

    _validate_physical_table_lineage(source, proposal, parent_input)


def _validate_physical_table_lineage(
    source: SourceIRSnapshot,
    proposal: D2LogicalStructureProposalV2,
    parent_input: D2TableRegionParentInputV1,
) -> None:
    table_nodes = tuple(
        item
        for item in source.nodes
        if item.kind is SourceNodeKind.TABLE and item.table_payload is not None
    )
    table_by_id = {item.table_payload.table_id: item for item in table_nodes}
    if len(table_by_id) != len(table_nodes):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_DUPLICATE_TABLE")
    partitions = {
        item.physical_table_id: item for item in parent_input.physical_partitions
    }
    residuals = {
        item.physical_table_id: item
        for item in parent_input.supplemental_table_evidence
    }
    ledgers = {
        item.physical_table_id: item for item in proposal.physical_table_evidence
    }
    if set(table_by_id) != set(partitions) or set(partitions) != set(ledgers):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_TABLE_SCOPE_DRIFT")
    for table_id, node in table_by_id.items():
        assert node.table_payload is not None
        partition = partitions[table_id]
        residual = residuals[table_id]
        ledger = ledgers[table_id]
        visual_residual_sha = _canonical_sha256(
            {
                "source_visual_locator": (
                    None
                    if node.visual_locator is None
                    else node.visual_locator.model_dump(mode="json")
                ),
                "source_asset_ref": node.asset_ref,
                "table_visual_asset_ref": node.table_payload.visual_asset_ref,
            }
        )
        if (
            partition.table_source_node_id != node.source_node_id
            or partition.physical_table_source_order != node.source_order
            # Both ProposalV2 and the region parent-input adapter hash the full
            # JSON-mode TableIR payload (including explicit null/default fields).
            # ``canonical_model_sha256`` excludes nulls and is therefore a
            # different contract even when it describes the same table.
            or partition.table_ir_sha256
            != _canonical_sha256(node.table_payload.model_dump(mode="json"))
            or residual.table_source_node_id != node.source_node_id
            or ledger.table_source_node_id != node.source_node_id
            or ledger.physical_table_source_order != node.source_order
            or ledger.table_source_node_sha256
            != _canonical_sha256(node.model_dump(mode="json"))
            or ledger.table_ir_sha256 != partition.table_ir_sha256
            or ledger.raw_text_sha256 != residual.table_node_raw_text_sha256
            or ledger.notes_sha256 != residual.table_notes_sha256
            or ledger.visual_residual_sha256 != visual_residual_sha
            or ledger.physical_region_coverage_sha256 != partition.cell_coverage_sha256
            or ledger.ordered_region_ids != partition.ordered_region_ids
            or ledger.empty_table_disposition != partition.empty_table_disposition
        ):
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_TABLE_LINEAGE_DRIFT")
        disposition_by_kind = {
            item.evidence_kind: item for item in ledger.supplemental_dispositions
        }
        visual_sha = _canonical_sha256(
            [
                item.model_dump(mode="json")
                for item in residual.supplemental_visual_evidence
            ]
        )
        if (
            disposition_by_kind[
                D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
            ].evidence_sha256
            != residual.table_node_raw_text_sha256
            or disposition_by_kind[
                D2TableSupplementalEvidenceKindV1.TABLE_NOTES
            ].evidence_sha256
            != residual.table_notes_sha256
            or disposition_by_kind[
                D2TableSupplementalEvidenceKindV1.TABLE_VISUAL
            ].evidence_sha256
            != visual_sha
        ):
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_SUPPLEMENTAL_DRIFT")


def _materialize_content(
    source: SourceIRSnapshot,
    *,
    proposal: D2LogicalStructureProposalV2,
    parent_input: D2TableRegionParentInputV1,
) -> tuple[
    list[D3UnifiedExactSourceSegmentV3],
    list[D3EvidenceLedgerEntryV1],
    list[D3UnresolvedEvidenceScopeV1],
]:
    nodes = {item.source_node_id: item for item in source.nodes}
    parent_segments_by_region: dict[str, tuple[D2TableRegionSourceSegmentV1, ...]] = {
        region_id: tuple(
            item for item in parent_input.source_segments if item.region_id == region_id
        )
        for region_id in parent_input.ordered_region_ids
    }
    proposal_region_dispositions = {
        item.region_id: item for item in proposal.table_region_dispositions
    }
    if len(proposal_region_dispositions) != len(proposal.table_region_dispositions):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_DUPLICATE_REGION")

    base_refs_by_node: dict[str, list[D2V2BaseSourceSegmentRefV1]] = {}
    for logical_object in proposal.logical_objects:
        for segment in logical_object.source_segments:
            if isinstance(segment, D2V2BaseSourceSegmentRefV1):
                base_refs_by_node.setdefault(segment.owner_source_node_id, []).append(
                    segment
                )
    base_ordinal: dict[str, int] = {}
    for node_id, refs in base_refs_by_node.items():
        ordered = sorted(
            refs,
            key=lambda item: (
                -1 if item.start_offset is None else item.start_offset,
                -1 if item.end_offset is None else item.end_offset,
                item.base_source_segment_id,
            ),
        )
        previous_end = -1
        for ordinal, item in enumerate(ordered):
            if item.start_offset is not None:
                assert item.end_offset is not None
                if item.start_offset < previous_end:
                    raise D3RegionStageInputError(
                        "D3_REGION_STAGE_INPUT_BASE_RANGE_OVERLAP"
                    )
                previous_end = item.end_offset
            base_ordinal[item.segment_id] = ordinal

    segments: list[D3UnifiedExactSourceSegmentV3] = []
    used_parent_cell_ids: set[str] = set()
    for logical_object in proposal.logical_objects:
        for source_ref in logical_object.source_segments:
            if isinstance(source_ref, D2V2BaseSourceSegmentRefV1):
                segment = _resolve_base_segment(
                    source_ref,
                    logical_object_id=logical_object.logical_object_id,
                    source=source,
                    nodes=nodes,
                    parent_input=parent_input,
                    intra_node_ordinal=base_ordinal[source_ref.segment_id],
                )
                if segment.region_parent_segment_id is not None:
                    cell_id = next(
                        item.cell_ids[0]
                        for item in parent_input.source_segments
                        if item.segment_id == segment.region_parent_segment_id
                    )
                    if cell_id in used_parent_cell_ids:
                        raise D3RegionStageInputError(
                            "D3_REGION_STAGE_INPUT_DUPLICATE_CELL_EVIDENCE"
                        )
                    used_parent_cell_ids.add(cell_id)
                segments.append(segment)
                continue

            assert isinstance(source_ref, D2V2TableRegionSourceSegmentRefV1)
            disposition = proposal_region_dispositions.get(source_ref.table_region_id)
            if (
                disposition is None
                or disposition.logical_object_id != logical_object.logical_object_id
                or disposition.source_segment_id != source_ref.segment_id
                or disposition.region_sha256 != source_ref.table_region_sha256
            ):
                raise D3RegionStageInputError(
                    "D3_REGION_STAGE_INPUT_REGION_OWNER_DRIFT"
                )
            parent_segments = parent_segments_by_region.get(
                source_ref.table_region_id,
                (),
            )
            if not parent_segments:
                raise D3RegionStageInputError(
                    "D3_REGION_STAGE_INPUT_REGION_TEXT_MISSING"
                )
            for parent_segment in parent_segments:
                cell_id = parent_segment.cell_ids[0]
                if cell_id in used_parent_cell_ids:
                    raise D3RegionStageInputError(
                        "D3_REGION_STAGE_INPUT_DUPLICATE_CELL_EVIDENCE"
                    )
                used_parent_cell_ids.add(cell_id)
                segments.append(
                    _unified_table_cell_segment(
                        logical_object_id=logical_object.logical_object_id,
                        source_ref=source_ref,
                        parent_segment=parent_segment,
                    )
                )

    evidence_ledger, unresolved_scopes, context_segments = _evidence_ledgers(
        source,
        proposal=proposal,
        parent_input=parent_input,
        primary_segments=tuple(segments),
    )
    segments.extend(context_segments)

    segment_ids = tuple(item.segment_id for item in segments)
    if len(segment_ids) != len(set(segment_ids)):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_DUPLICATE_SEGMENT")
    positions = tuple(item.source_position for item in segments)
    if len(positions) != len(set(positions)):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_POSITION_COLLISION")
    return segments, evidence_ledger, unresolved_scopes


def _resolve_base_segment(
    item: D2V2BaseSourceSegmentRefV1,
    *,
    logical_object_id: str,
    source: SourceIRSnapshot,
    nodes: dict[str, SourceNode],
    parent_input: D2TableRegionParentInputV1,
    intra_node_ordinal: int,
) -> D3UnifiedExactSourceSegmentV3:
    node = nodes.get(item.owner_source_node_id)
    if node is None or item.source_ir_id != source.source_ir_id:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_OWNER_MISSING")
    if (
        item.source_sha256 != source.source_sha256
        or item.source_order != node.source_order
    ):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_SOURCE_DRIFT")

    parent_segment: D2TableRegionSourceSegmentV1 | None = None
    if item.handle_kind is D2SourceHandleKind.SOURCE_NODE:
        if (
            item.source_handle != node.source_node_id
            or node.kind is SourceNodeKind.TABLE
        ):
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_HANDLE_DRIFT")
        full_text = node.raw_text
        try:
            content_sha = d2_source_node_content_sha256(node)
        except ValueError as exc:
            raise D3RegionStageInputError(
                "D3_REGION_STAGE_INPUT_SOURCE_CONTENT_MISSING"
            ) from exc
        evidence_kind = D3UnifiedEvidenceKindV3.BASE_TEXT
        segment_id = item.segment_id
        evidence_refs = (node.source_node_id, item.base_source_segment_id)
    else:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_HANDLE_DRIFT")
        candidates = tuple(
            segment
            for segment in parent_input.source_segments
            if segment.table_source_node_id == node.source_node_id
            and segment.cell_ids == (item.source_handle,)
        )
        if len(candidates) != 1:
            raise D3RegionStageInputError(
                "D3_REGION_STAGE_INPUT_TABLE_CELL_PARENT_BINDING_MISSING"
            )
        parent_segment = candidates[0]
        full_text = parent_segment.source_text
        content_sha = parent_segment.source_text_sha256
        evidence_kind = D3UnifiedEvidenceKindV3.TABLE_CELL
        segment_id = parent_segment.segment_id
        evidence_refs = parent_segment.evidence_refs

    expected_base_sha = stable_d2_source_segment_sha256(
        segment_id=item.base_source_segment_id,
        source_sha256=item.source_sha256,
        handle_kind=item.handle_kind,
        source_handle=item.source_handle,
        owner_source_node_id=item.owner_source_node_id,
        source_order=item.source_order,
        page_numbers=item.page_numbers,
        start_offset=item.start_offset,
        end_offset=item.end_offset,
        source_content_sha256=content_sha,
    )
    if expected_base_sha != item.base_source_segment_sha256:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_CONTENT_DRIFT")
    if item.start_offset is None:
        if full_text:
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_RANGE_MISSING")
        exact_text = ""
    else:
        assert item.end_offset is not None
        if item.end_offset > len(full_text):
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_RANGE_DRIFT")
        exact_text = full_text[item.start_offset : item.end_offset]
    if parent_segment is not None and exact_text != parent_segment.source_text:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_TABLE_CELL_RANGE_DRIFT")
    native_page = node.native_locator.page_number
    expected_pages = () if native_page is None else (native_page,)
    if item.page_numbers != expected_pages:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_BASE_PAGE_DRIFT")
    values = {
        "segment_id": segment_id,
        "logical_object_id": logical_object_id,
        "segment_role": D3UnifiedSegmentRoleV3.PRIMARY_EXACT,
        "evidence_kind": evidence_kind,
        "source_position": (
            node.source_order,
            _EVIDENCE_KIND_RANK[evidence_kind],
            (
                parent_segment.verbatim_cells[0].canonical_cell_ordinal
                if parent_segment is not None
                else intra_node_ordinal
            ),
            item.start_offset or 0,
        ),
        "source_node_id": node.source_node_id,
        "source_node_order": node.source_order,
        "page_numbers": item.page_numbers,
        "intra_node_ordinal": (
            parent_segment.verbatim_cells[0].canonical_cell_ordinal
            if parent_segment is not None
            else intra_node_ordinal
        ),
        "start_offset": item.start_offset,
        "end_offset": item.end_offset,
        "source_text": exact_text,
        "source_text_sha256": _text_sha256(exact_text),
        "source_content_sha256": content_sha,
        "d2_v2_source_segment_id": item.segment_id,
        "d2_v2_source_segment_sha256": item.segment_sha256,
        "region_parent_segment_id": (
            None if parent_segment is None else parent_segment.segment_id
        ),
        "region_parent_segment_sha256": (
            None if parent_segment is None else parent_segment.segment_sha256
        ),
        "supplemental_evidence_id": None,
        "evidence_refs": evidence_refs,
    }
    return D3UnifiedExactSourceSegmentV3(
        **values,
        segment_sha256=_canonical_sha256(
            {
                "schema_version": D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION,
                **_json_value(values),
            }
        ),
    )


def _unified_table_cell_segment(
    *,
    logical_object_id: str,
    source_ref: D2V2TableRegionSourceSegmentRefV1,
    parent_segment: D2TableRegionSourceSegmentV1,
) -> D3UnifiedExactSourceSegmentV3:
    if (
        parent_segment.region_artifact_id != source_ref.table_region_artifact_id
        or parent_segment.region_artifact_sha256
        != source_ref.table_region_artifact_sha256
        or parent_segment.region_id != source_ref.table_region_id
        or parent_segment.region_sha256 != source_ref.table_region_sha256
        or parent_segment.physical_table_id != source_ref.physical_table_id
        or parent_segment.table_source_node_id != source_ref.table_source_node_id
        or parent_segment.physical_table_source_order != source_ref.source_order
    ):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_CELL_LINEAGE_DRIFT")
    cell = parent_segment.verbatim_cells[0]
    values = {
        "segment_id": parent_segment.segment_id,
        "logical_object_id": logical_object_id,
        "segment_role": D3UnifiedSegmentRoleV3.PRIMARY_EXACT,
        "evidence_kind": D3UnifiedEvidenceKindV3.TABLE_CELL,
        "source_position": (
            parent_segment.physical_table_source_order,
            _EVIDENCE_KIND_RANK[D3UnifiedEvidenceKindV3.TABLE_CELL],
            cell.canonical_cell_ordinal,
            0,
        ),
        "source_node_id": parent_segment.table_source_node_id,
        "source_node_order": parent_segment.physical_table_source_order,
        "page_numbers": parent_segment.page_numbers,
        "intra_node_ordinal": cell.canonical_cell_ordinal,
        "start_offset": 0,
        "end_offset": len(parent_segment.source_text),
        "source_text": parent_segment.source_text,
        "source_text_sha256": parent_segment.source_text_sha256,
        "source_content_sha256": cell.raw_text_sha256,
        "d2_v2_source_segment_id": source_ref.segment_id,
        "d2_v2_source_segment_sha256": source_ref.segment_sha256,
        "region_parent_segment_id": parent_segment.segment_id,
        "region_parent_segment_sha256": parent_segment.segment_sha256,
        "supplemental_evidence_id": None,
        "evidence_refs": parent_segment.evidence_refs,
    }
    return D3UnifiedExactSourceSegmentV3(
        **values,
        segment_sha256=_canonical_sha256(
            {
                "schema_version": D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION,
                **_json_value(values),
            }
        ),
    )


def _evidence_ledgers(
    source: SourceIRSnapshot,
    *,
    proposal: D2LogicalStructureProposalV2,
    parent_input: D2TableRegionParentInputV1,
    primary_segments: tuple[D3UnifiedExactSourceSegmentV3, ...],
) -> tuple[
    list[D3EvidenceLedgerEntryV1],
    list[D3UnresolvedEvidenceScopeV1],
    list[D3UnifiedExactSourceSegmentV3],
]:
    object_ids = {item.logical_object_id for item in proposal.logical_objects}
    primary_by_object = {
        object_id: tuple(
            item.segment_id
            for item in sorted(primary_segments, key=lambda item: item.source_position)
            if item.logical_object_id == object_id
        )
        for object_id in object_ids
    }
    primary_by_id = {item.segment_id: item for item in primary_segments}
    parent_segment_by_id = {
        item.segment_id: item for item in parent_input.source_segments
    }
    nested_owner: dict[str, str] = {}
    for logical_object in proposal.logical_objects:
        for region_id in logical_object.nested_evidence_region_ids:
            if region_id in nested_owner:
                raise D3RegionStageInputError(
                    "D3_REGION_STAGE_INPUT_DUPLICATE_NESTED_OWNER"
                )
            nested_owner[region_id] = logical_object.logical_object_id
    if set(nested_owner) != set(proposal.nested_region_ids):
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_NESTED_OWNER_DRIFT")

    ledgers: list[D3EvidenceLedgerEntryV1] = []
    unresolved: list[D3UnresolvedEvidenceScopeV1] = []
    context_segments: list[D3UnifiedExactSourceSegmentV3] = []
    for record in parent_input.nested_region_evidence:
        owner_id = nested_owner.get(record.region_id)
        owner_region_id = record.nested_owner_region_id
        owner_segment_ids = record.owner_source_segment_ids
        if (
            owner_id is None
            or owner_region_id is None
            or not owner_segment_ids
            or any(
                segment_id not in parent_segment_by_id
                or parent_segment_by_id[segment_id].region_id != owner_region_id
                or segment_id not in primary_by_id
                or primary_by_id[segment_id].logical_object_id != owner_id
                for segment_id in owner_segment_ids
            )
            or not set(owner_segment_ids).issubset(primary_by_object[owner_id])
        ):
            raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_NESTED_OWNER_DRIFT")
        ledger = _region_evidence_ledger(
            record,
            evidence_kind=D3EvidenceLedgerKindV1.NESTED_REGION,
            ultimate_owner_logical_object_id=owner_id,
            owner_segment_ids=owner_segment_ids,
            selected_parent_assignment_eligible=True,
        )
        ledgers.append(ledger)
    for record in parent_input.non_business_region_evidence:
        ledgers.append(
            _region_evidence_ledger(
                record,
                evidence_kind=D3EvidenceLedgerKindV1.NON_BUSINESS_REGION,
            )
        )
    for record in parent_input.unresolved_scopes:
        ledger = _unresolved_region_evidence_ledger(record)
        ledgers.append(ledger)
        unresolved.append(
            _unresolved_scope(
                scope_kind=D3UnresolvedScopeKindV1.REGION,
                source_node_order=record.physical_table_source_order,
                evidence_ids=(ledger.evidence_id,),
                reason_codes=record.reason_codes,
            )
        )

    residual_by_id = {
        item.physical_table_id: item
        for item in parent_input.supplemental_table_evidence
    }
    partition_by_id = {
        item.physical_table_id: item for item in parent_input.physical_partitions
    }
    all_cells_by_table = _all_parent_cells(parent_input)
    for table in proposal.physical_table_evidence:
        residual = residual_by_id[table.physical_table_id]
        partition = partition_by_id[table.physical_table_id]
        for disposition in table.supplemental_dispositions:
            owner_id = disposition.owner_logical_object_id
            if owner_id is not None and owner_id not in object_ids:
                raise D3RegionStageInputError(
                    "D3_REGION_STAGE_INPUT_SUPPLEMENTAL_OWNER_DRIFT"
                )
            text_fragments: tuple[str, ...] = ()
            visual_refs: tuple[str, ...] = ()
            if (
                disposition.evidence_kind
                is D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
            ):
                if residual.table_node_raw_text:
                    text_fragments = (residual.table_node_raw_text,)
            elif (
                disposition.evidence_kind
                is D2TableSupplementalEvidenceKindV1.TABLE_NOTES
            ):
                text_fragments = residual.table_notes
            else:
                visual_refs = _visual_evidence_refs(
                    residual.supplemental_visual_evidence
                )

            if disposition.disposition is D2TableSupplementalDispositionKindV1.ABSENT:
                if text_fragments or visual_refs:
                    raise D3RegionStageInputError(
                        "D3_REGION_STAGE_INPUT_ABSENT_EVIDENCE_PRESENT"
                    )
            if (
                disposition.disposition
                is D2TableSupplementalDispositionKindV1.CELL_DERIVED
            ):
                cells = all_cells_by_table[table.physical_table_id]
                if (
                    disposition.derived_cell_ids != partition.canonical_cell_ids
                    or tuple(item.cell_id for item in cells)
                    != partition.canonical_cell_ids
                    or residual.table_node_raw_text
                    != "\n".join(item.raw_text for item in cells)
                ):
                    raise D3RegionStageInputError(
                        "D3_REGION_STAGE_INPUT_CELL_DERIVATION_DRIFT"
                    )
                text_fragments = ()

            if (
                disposition.disposition
                in {
                    D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
                    D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
                }
                and text_fragments
            ):
                assert owner_id is not None
                built = tuple(
                    _supplemental_context_segment(
                        logical_object_id=owner_id,
                        table_source_node_id=table.table_source_node_id,
                        source_node_order=table.physical_table_source_order,
                        evidence_id=disposition.evidence_id,
                        evidence_kind=disposition.evidence_kind,
                        fragment=fragment,
                        fragment_ordinal=ordinal,
                        source_evidence_sha256=disposition.evidence_sha256,
                    )
                    for ordinal, fragment in enumerate(text_fragments)
                )
                context_segments.extend(built)

            owner_context_ids = (
                tuple(
                    item.segment_id
                    for item in context_segments
                    if item.logical_object_id == owner_id
                )
                if owner_id is not None
                else ()
            )
            owner_segment_ids = (
                tuple(
                    sorted(
                        (*primary_by_object[owner_id], *owner_context_ids),
                        key=lambda segment_id: next(
                            item.source_position
                            for item in (*primary_segments, *context_segments)
                            if item.segment_id == segment_id
                        ),
                    )
                )
                if owner_id is not None
                else ()
            )
            owned_disposition = disposition.disposition in {
                D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
                D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
            }
            owner_has_no_selectable_segments = (
                owned_disposition and not owner_segment_ids
            )
            effective_disposition = (
                D2TableSupplementalDispositionKindV1.UNRESOLVED
                if owner_has_no_selectable_segments
                else disposition.disposition
            )
            effective_owner_id = None if owner_has_no_selectable_segments else owner_id
            evidence_refs = tuple(
                dict.fromkeys(
                    (
                        disposition.evidence_ref,
                        *visual_refs,
                        *(
                            cell.cell_evidence_sha256
                            for cell in all_cells_by_table[table.physical_table_id]
                            if cell.cell_id in set(disposition.derived_cell_ids)
                        ),
                    )
                )
            )
            ledger = _evidence_ledger(
                evidence_id=disposition.evidence_id,
                evidence_kind=D3EvidenceLedgerKindV1.TABLE_SUPPLEMENTAL,
                disposition=effective_disposition.value,
                physical_table_id=table.physical_table_id,
                table_source_node_id=table.table_source_node_id,
                source_node_order=table.physical_table_source_order,
                region_id=disposition.nested_owner_region_id,
                ultimate_owner_logical_object_id=effective_owner_id,
                owner_segment_ids=(
                    () if owner_has_no_selectable_segments else owner_segment_ids
                ),
                text_fragments=(
                    ()
                    if disposition.disposition
                    in {
                        D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
                        D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
                        D2TableSupplementalDispositionKindV1.CELL_DERIVED,
                    }
                    else text_fragments
                ),
                evidence_refs=evidence_refs,
                derived_cell_ids=disposition.derived_cell_ids,
                source_evidence_sha256=disposition.disposition_sha256,
            )
            ledgers.append(ledger)
            if effective_disposition is D2TableSupplementalDispositionKindV1.UNRESOLVED:
                reason_codes = (
                    ("supplemental_owner_has_no_selectable_segments",)
                    if owner_has_no_selectable_segments
                    else disposition.reason_codes
                )
                unresolved.append(
                    _unresolved_scope(
                        scope_kind=D3UnresolvedScopeKindV1.SUPPLEMENTAL,
                        source_node_order=table.physical_table_source_order,
                        evidence_ids=(ledger.evidence_id,),
                        reason_codes=reason_codes,
                    )
                )

        if (
            partition.empty_table_disposition
            is D2EmptyPhysicalTableDispositionKind.UNRESOLVED
        ):
            evidence_id = f"empty-table:{table.physical_table_id}"
            ledger = _evidence_ledger(
                evidence_id=evidence_id,
                evidence_kind=D3EvidenceLedgerKindV1.EMPTY_TABLE,
                disposition=D2EmptyPhysicalTableDispositionKind.UNRESOLVED.value,
                physical_table_id=table.physical_table_id,
                table_source_node_id=table.table_source_node_id,
                source_node_order=table.physical_table_source_order,
                evidence_refs=(partition.partition_sha256,),
                source_evidence_sha256=partition.partition_sha256,
            )
            ledgers.append(ledger)
            unresolved.append(
                _unresolved_scope(
                    scope_kind=D3UnresolvedScopeKindV1.EMPTY_TABLE,
                    source_node_order=table.physical_table_source_order,
                    evidence_ids=(evidence_id,),
                    reason_codes=partition.empty_table_reason_codes,
                )
            )

    predecessor_reasons = tuple(
        sorted(
            {
                *proposal.unresolved_codes,
                *(
                    ("source_ir_partial",)
                    if source.status is SourceIRStatus.PARTIAL
                    else ()
                ),
                *(
                    ("region_parent_input_partial",)
                    if parent_input.status is D2TableRegionParentInputStatus.PARTIAL
                    and not unresolved
                    else ()
                ),
            }
        )
    )
    if proposal.status is D2LogicalStructureStatus.PARTIAL and not predecessor_reasons:
        raise D3RegionStageInputError("D3_REGION_STAGE_INPUT_PARTIAL_REASON_MISSING")
    if predecessor_reasons:
        unresolved.append(
            _unresolved_scope(
                scope_kind=D3UnresolvedScopeKindV1.PREDECESSOR,
                source_node_order=0,
                evidence_ids=(proposal.proposal_sha256,),
                reason_codes=predecessor_reasons,
            )
        )
    return ledgers, unresolved, context_segments


def _region_evidence_ledger(
    record: D2TableRegionExcludedEvidenceV1,
    *,
    evidence_kind: D3EvidenceLedgerKindV1,
    ultimate_owner_logical_object_id: str | None = None,
    owner_segment_ids: tuple[str, ...] = (),
    selected_parent_assignment_eligible: bool = False,
) -> D3EvidenceLedgerEntryV1:
    return _evidence_ledger(
        evidence_id=record.evidence_id,
        evidence_kind=evidence_kind,
        disposition=record.disposition.value,
        physical_table_id=record.physical_table_id,
        table_source_node_id=record.table_source_node_id,
        source_node_order=record.physical_table_source_order,
        region_id=record.region_id,
        ultimate_owner_logical_object_id=ultimate_owner_logical_object_id,
        owner_segment_ids=owner_segment_ids,
        text_fragments=(record.source_text,),
        evidence_refs=record.evidence_refs,
        selected_parent_assignment_eligible=selected_parent_assignment_eligible,
        source_evidence_sha256=record.evidence_sha256,
    )


def _unresolved_region_evidence_ledger(
    record: D2TableRegionUnresolvedScopeV1,
) -> D3EvidenceLedgerEntryV1:
    return _evidence_ledger(
        evidence_id=record.unresolved_scope_id,
        evidence_kind=D3EvidenceLedgerKindV1.UNRESOLVED_REGION,
        disposition=record.disposition.value,
        physical_table_id=record.physical_table_id,
        table_source_node_id=record.table_source_node_id,
        source_node_order=record.physical_table_source_order,
        region_id=record.region_id,
        text_fragments=(record.source_text,),
        evidence_refs=record.evidence_refs,
        source_evidence_sha256=record.unresolved_scope_sha256,
    )


def _evidence_ledger(
    *,
    evidence_id: str,
    evidence_kind: D3EvidenceLedgerKindV1,
    disposition: str,
    physical_table_id: str,
    table_source_node_id: str,
    source_node_order: int,
    region_id: str | None = None,
    ultimate_owner_logical_object_id: str | None = None,
    owner_segment_ids: tuple[str, ...] = (),
    text_fragments: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    derived_cell_ids: tuple[str, ...] = (),
    selected_parent_assignment_eligible: bool = False,
    source_evidence_sha256: str,
) -> D3EvidenceLedgerEntryV1:
    values = {
        "evidence_id": evidence_id,
        "evidence_kind": evidence_kind,
        "disposition": disposition,
        "physical_table_id": physical_table_id,
        "table_source_node_id": table_source_node_id,
        "source_node_order": source_node_order,
        "region_id": region_id,
        "ultimate_owner_logical_object_id": ultimate_owner_logical_object_id,
        "owner_segment_ids": owner_segment_ids,
        "text_fragments": text_fragments,
        "text_fragment_sha256s": tuple(_text_sha256(item) for item in text_fragments),
        "evidence_refs": evidence_refs,
        "derived_cell_ids": derived_cell_ids,
        "selected_parent_assignment_eligible": selected_parent_assignment_eligible,
        "source_evidence_sha256": source_evidence_sha256,
    }
    return D3EvidenceLedgerEntryV1(
        **values,
        ledger_sha256=_canonical_sha256(values),
    )


def _unresolved_scope(
    *,
    scope_kind: D3UnresolvedScopeKindV1,
    source_node_order: int,
    evidence_ids: tuple[str, ...],
    reason_codes: tuple[str, ...],
) -> D3UnresolvedEvidenceScopeV1:
    values = {
        "scope_kind": scope_kind,
        "source_node_order": source_node_order,
        "evidence_ids": evidence_ids,
        "reason_codes": tuple(sorted(set(reason_codes))),
    }
    draft = D3UnresolvedEvidenceScopeV1.model_construct(
        **values,
        unresolved_scope_id="d3-region-unresolved-scope-" + "0" * 32,
        scope_sha256="0" * 64,
    )
    scope_sha = _canonical_sha256(
        draft.model_dump(
            mode="json",
            exclude={"unresolved_scope_id", "scope_sha256"},
        )
    )
    return D3UnresolvedEvidenceScopeV1(
        **values,
        unresolved_scope_id=f"d3-region-unresolved-scope-{scope_sha[:32]}",
        scope_sha256=scope_sha,
    )


def _supplemental_context_segment(
    *,
    logical_object_id: str,
    table_source_node_id: str,
    source_node_order: int,
    evidence_id: str,
    evidence_kind: D2TableSupplementalEvidenceKindV1,
    fragment: str,
    fragment_ordinal: int,
    source_evidence_sha256: str,
) -> D3UnifiedExactSourceSegmentV3:
    unified_kind = (
        D3UnifiedEvidenceKindV3.TABLE_NODE_RAW_TEXT
        if evidence_kind is D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
        else D3UnifiedEvidenceKindV3.TABLE_NOTE
    )
    identity = {
        "logical_object_id": logical_object_id,
        "evidence_id": evidence_id,
        "fragment_ordinal": fragment_ordinal,
        "source_text_sha256": _text_sha256(fragment),
    }
    segment_id = f"d3-region-context-{_canonical_sha256(identity)[:32]}"
    values = {
        "segment_id": segment_id,
        "logical_object_id": logical_object_id,
        "segment_role": D3UnifiedSegmentRoleV3.OWNED_CONTEXT,
        "evidence_kind": unified_kind,
        "source_position": (
            source_node_order,
            _EVIDENCE_KIND_RANK[unified_kind],
            fragment_ordinal,
            0,
        ),
        "source_node_id": table_source_node_id,
        "source_node_order": source_node_order,
        "page_numbers": (),
        "intra_node_ordinal": fragment_ordinal,
        "start_offset": 0,
        "end_offset": len(fragment),
        "source_text": fragment,
        "source_text_sha256": _text_sha256(fragment),
        "source_content_sha256": source_evidence_sha256,
        "d2_v2_source_segment_id": None,
        "d2_v2_source_segment_sha256": None,
        "region_parent_segment_id": None,
        "region_parent_segment_sha256": None,
        "supplemental_evidence_id": evidence_id,
        "evidence_refs": (evidence_id,),
    }
    return D3UnifiedExactSourceSegmentV3(
        **values,
        segment_sha256=_canonical_sha256(
            {
                "schema_version": D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION,
                **_json_value(values),
            }
        ),
    )


def _all_parent_cells(
    parent_input: D2TableRegionParentInputV1,
) -> dict[str, tuple[object, ...]]:
    records = (
        *parent_input.source_segments,
        *parent_input.nested_region_evidence,
        *parent_input.non_business_region_evidence,
        *parent_input.unresolved_scopes,
    )
    result: dict[str, tuple[object, ...]] = {}
    for partition in parent_input.physical_partitions:
        cells = tuple(
            sorted(
                (
                    cell
                    for record in records
                    if record.physical_table_id == partition.physical_table_id
                    for cell in record.verbatim_cells
                ),
                key=lambda cell: cell.canonical_cell_ordinal,
            )
        )
        if tuple(cell.cell_id for cell in cells) != partition.canonical_cell_ids:
            raise D3RegionStageInputError(
                "D3_REGION_STAGE_INPUT_PARENT_CELL_PARTITION_DRIFT"
            )
        result[partition.physical_table_id] = cells
    return result


def _visual_evidence_refs(
    values: tuple[D2TableSupplementalEvidenceRefV1, ...],
) -> tuple[str, ...]:
    return tuple(
        item for value in values for item in (value.evidence_ref, value.evidence_sha256)
    )


def _lineage_values(
    *,
    proposal: D2LogicalStructureProposalV2,
    validation_receipt: D2LogicalStructureValidationReceiptV2,
    parent_input: D2TableRegionParentInputV1,
    parent_input_receipt: D2TableRegionParentInputReceiptV1,
) -> dict[str, object]:
    return {
        "run_id": proposal.run_id,
        "source_ir_id": proposal.source_ir_id,
        "source_sha256": proposal.source_sha256,
        "source_ir_canonical_sha256": proposal.source_ir_canonical_sha256,
        "kit_bundle_sha256": proposal.kit_identity.bundle_sha256,
        "base_artifact_id": proposal.base_logical_structure.artifact_id,
        "base_artifact_sha256": proposal.base_logical_structure.artifact_sha256,
        "base_validation_receipt_sha256": (
            proposal.base_logical_structure.validation_receipt_sha256
        ),
        "region_artifact_id": proposal.table_region_artifact.artifact_id,
        "region_artifact_sha256": proposal.table_region_artifact.artifact_sha256,
        "v2_artifact_id": validation_receipt.artifact_id,
        "v2_artifact_sha256": validation_receipt.artifact_sha256,
        "v2_validation_receipt_sha256": validation_receipt.receipt_sha256,
        "parent_input_id": parent_input.stage_input_id,
        "parent_input_sha256": parent_input.stage_input_sha256,
        "parent_input_receipt_sha256": parent_input_receipt.receipt_sha256,
        "d1_table_map_artifact_id": parent_input.d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": parent_input.d1_table_map_artifact_sha256,
    }


def _source_scope_sha256(
    segments: tuple[D3UnifiedExactSourceSegmentV3, ...],
    logical_objects: tuple[D3LogicalObjectSegmentScopeV3, ...],
    evidence_ledger: tuple[D3EvidenceLedgerEntryV1, ...],
    unresolved_scopes: tuple[D3UnresolvedEvidenceScopeV1, ...],
) -> str:
    return _canonical_sha256(
        {
            "segment_sha256s": [item.segment_sha256 for item in segments],
            "logical_object_scope_sha256s": [
                item.scope_sha256 for item in logical_objects
            ],
            "evidence_ledger_sha256s": [item.ledger_sha256 for item in evidence_ledger],
            "unresolved_scope_sha256s": [
                item.scope_sha256 for item in unresolved_scopes
            ],
        }
    )


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


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


__all__ = [
    "D3_REGION_STAGE_INPUT_LINEAGE_SCHEMA_VERSION",
    "D3_REGION_STAGE_INPUT_MANIFEST_SCHEMA_VERSION",
    "D3_UNIFIED_EXACT_SOURCE_SEGMENT_SCHEMA_VERSION",
    "D3_UNIFIED_EXACT_SOURCE_SEGMENT_SET_SCHEMA_VERSION",
    "D3EvidenceLedgerEntryV1",
    "D3EvidenceLedgerKindV1",
    "D3LogicalObjectSegmentScopeV3",
    "D3RegionStageInputCompilationV1",
    "D3RegionStageInputError",
    "D3RegionStageInputLineageV1",
    "D3RegionStageInputManifestV1",
    "D3RegionStageInputStatus",
    "D3UnifiedEvidenceKindV3",
    "D3UnifiedExactSourceSegmentSetV3",
    "D3UnifiedExactSourceSegmentV3",
    "D3UnifiedSegmentRoleV3",
    "D3UnresolvedEvidenceScopeV1",
    "D3UnresolvedScopeKindV1",
    "build_d3_region_stage_input_lineage",
    "materialize_d3_region_stage_input",
]
