from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.application.agent_workflow.d2_decision_candidate import (
    D2CandidateSourceRangeV1,
    D2CandidateSourceSpanV2,
    D2LogicalObjectDecisionCandidateV2,
    D2LogicalScopePartTableDispositionDecisionCandidateV1,
    D2LogicalStructureCompilationV1,
    D2LogicalStructureDecisionCandidateV2,
    D2LogicalTableScopeDecisionCandidateV1,
    D2UnboundSourceDispositionDecisionCandidateV1,
    _cell_catalog,
    _expand_candidate_source_spans,
    _resolve_source_segment,
    compile_and_validate_d2_logical_structure_candidate,
)
from app.application.agent_workflow.d2_logical_structure import (
    D2LogicalObjectKind,
    D2PhysicalTableDispositionKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_derived_container_id,
    stable_d2_logical_object_id,
)
from app.application.agent_workflow.d2_logical_structure_v2 import (
    D2BaseTableReplacementCandidateV1,
    D2BaseTableReplacementDisposition,
    D2LogicalStructureCompilationV2,
    D2RegionLogicalObjectPlacementMode,
    D2RegionLogicalObjectPlacementV1,
    D2RegionLogicalScopePlacementV1,
    D2RegionPlacementPlanV1,
    D2RetainedBaseObjectPlacementV1,
    D2SupplementalObjectOwnerReferenceKindV1,
    D2TableRawTextCellDerivationV1,
    D2TableSupplementalDispositionKindV1,
    D2TableSupplementalEvidenceKindV1,
    D2TableSupplementalEvidencePlacementV1,
    build_d2_region_placement_plan,
    compile_d2_logical_structure_v2,
    stable_d2_v2_region_logical_object_id,
)
from app.application.agent_workflow.d2_table_region_parent_input import (
    D2TableRegionParentInputCompilationV1,
    build_d2_table_region_parent_input,
)
from app.application.agent_workflow.d2_table_regions import (
    D2EmptyPhysicalTableDispositionCandidateV1,
    D2LogicalScopePartTableRegionCandidateV1,
    D2LogicalObjectPartTableRegionCandidateV1,
    D2NestedValueTableRegionCandidateV1,
    D2NonBusinessFalseTableRegionCandidateV1,
    D2TableLogicalScopeCandidateV1,
    D2TableRegionArtifactV1,
    D2TableRegionCandidateArtifactV1,
    D2TableRegionDispositionKind,
    D2TableRegionGapPolicy,
    D2UnresolvedTableRegionCandidateV1,
    compile_d2_table_regions,
    d2_table_region_source_ir_canonical_sha256,
)
from app.application.agent_workflow.extraction_runtime import AgentKitIdentityV1
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableIR,
)


D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION = (
    "d2-logical-structure-decision-candidate/v3"
)
D2_LOGICAL_STRUCTURE_COMPILATION_V3_SCHEMA_VERSION = (
    "d2-logical-structure-compilation/v3"
)

_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,159}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


# App Server structured output rejects JSON Schema ``oneOf``. Keep this wire
# union non-discriminated so Pydantic emits the already-supported ``anyOf``;
# every branch still has a distinct Literal disposition and strict fields, and
# the Host recompiles it through the fully validated region artifact contract.
D2TableRegionWireCandidateV3 = (
    D2LogicalScopePartTableRegionCandidateV1
    | D2LogicalObjectPartTableRegionCandidateV1
    | D2NestedValueTableRegionCandidateV1
    | D2NonBusinessFalseTableRegionCandidateV1
    | D2UnresolvedTableRegionCandidateV1
)


class D2UnifiedLogicalObjectDecisionCandidateV3(_StrictFrozenModel):
    """Runner-owned non-table object boundary in the unified object graph."""

    object_key: str = Field(pattern=_KEY_PATTERN.pattern)
    object_kind: D2LogicalObjectKind
    parent_object_key: str | None = Field(
        default=None,
        pattern=_KEY_PATTERN.pattern,
    )
    source_ranges: tuple[D2CandidateSourceRangeV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    source_spans: tuple[D2CandidateSourceSpanV2, ...] = Field(
        default=(),
        max_length=4096,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("source_ranges")
    @classmethod
    def reject_duplicate_ranges(
        cls,
        value: tuple[D2CandidateSourceRangeV1, ...],
    ) -> tuple[D2CandidateSourceRangeV1, ...]:
        keys = tuple(
            (
                item.handle_kind,
                item.source_handle,
                item.start_offset,
                item.end_offset,
            )
            for item in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("a V3 object cannot repeat a source range")
        return value

    @field_validator("source_spans")
    @classmethod
    def reject_duplicate_spans(
        cls,
        value: tuple[D2CandidateSourceSpanV2, ...],
    ) -> tuple[D2CandidateSourceSpanV2, ...]:
        if len(value) != len(set(value)):
            raise ValueError("a V3 object cannot repeat a source span")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reasons(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_object(self) -> D2UnifiedLogicalObjectDecisionCandidateV3:
        if self.object_kind is D2LogicalObjectKind.TABLE:
            raise ValueError("V3 runner objects cannot define whole-table objects")
        if self.parent_object_key == self.object_key:
            raise ValueError("a V3 object cannot own itself")
        return self


class D2TableRegionDecisionCandidateV3(_StrictFrozenModel):
    """Region semantics only; Host adds SourceIR, D1, Kit, IDs, and hashes."""

    regions: tuple[D2TableRegionWireCandidateV3, ...] = Field(
        default=(),
        max_length=16384,
    )
    logical_scopes: tuple[D2TableLogicalScopeCandidateV1, ...] = Field(
        default=(),
        max_length=16384,
    )
    empty_table_dispositions: tuple[D2EmptyPhysicalTableDispositionCandidateV1, ...] = (
        Field(default=(), max_length=4096)
    )


class D2RegionLogicalScopePlacementDecisionCandidateV3(_StrictFrozenModel):
    logical_scope_key: str = Field(pattern=_KEY_PATTERN.pattern)
    parent_object_key: str | None = Field(
        default=None,
        pattern=_KEY_PATTERN.pattern,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _validate_reason_codes(value)
    )


class D2RegionLogicalObjectPlacementDecisionCandidateV3(_StrictFrozenModel):
    """Merge region content into one unified object key."""

    target_object_key: str = Field(pattern=_KEY_PATTERN.pattern)
    source_region_logical_object_keys: tuple[str, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("source_region_logical_object_keys")
    @classmethod
    def validate_source_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            _KEY_PATTERN.fullmatch(item) is None for item in value
        ):
            raise ValueError("region logical-object keys must be unique valid keys")
        return value

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _validate_reason_codes(value)
    )


class D2TableSupplementalEvidencePlacementDecisionCandidateV3(_StrictFrozenModel):
    """Key-based semantic placement; Host supplies evidence refs and hashes."""

    physical_table_id: str = Field(min_length=1, max_length=160)
    evidence_kind: D2TableSupplementalEvidenceKindV1
    disposition: Literal[
        D2TableSupplementalDispositionKindV1.CELL_DERIVED,
        D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT,
        D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE,
        D2TableSupplementalDispositionKindV1.NESTED,
        D2TableSupplementalDispositionKindV1.NON_BUSINESS,
        D2TableSupplementalDispositionKindV1.UNRESOLVED,
    ]
    owner_object_key: str | None = Field(
        default=None,
        pattern=_KEY_PATTERN.pattern,
    )
    owner_logical_scope_key: str | None = Field(
        default=None,
        pattern=_KEY_PATTERN.pattern,
    )
    nested_owner_region_key: str | None = Field(
        default=None,
        pattern=_KEY_PATTERN.pattern,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    _validate_reasons = field_validator("reason_codes")(
        lambda value: _validate_reason_codes(value)
    )

    @model_validator(mode="after")
    def validate_owner_shape(
        self,
    ) -> D2TableSupplementalEvidencePlacementDecisionCandidateV3:
        owner_shape = (
            self.owner_object_key is not None,
            self.owner_logical_scope_key is not None,
            self.nested_owner_region_key is not None,
        )
        expected = {
            D2TableSupplementalDispositionKindV1.CELL_DERIVED: (
                False,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT: (
                True,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE: (
                False,
                True,
                False,
            ),
            D2TableSupplementalDispositionKindV1.NESTED: (
                False,
                False,
                True,
            ),
            D2TableSupplementalDispositionKindV1.NON_BUSINESS: (
                False,
                False,
                False,
            ),
            D2TableSupplementalDispositionKindV1.UNRESOLVED: (
                False,
                False,
                False,
            ),
        }[self.disposition]
        if owner_shape != expected:
            raise ValueError("supplemental placement owner fields are inconsistent")
        if (
            self.disposition is D2TableSupplementalDispositionKindV1.CELL_DERIVED
            and self.evidence_kind
            is not D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT
        ):
            raise ValueError("CELL_DERIVED applies only to table-node raw text")
        return self


class D2LogicalStructureDecisionCandidateV3(_StrictFrozenModel):
    """Runner schema: semantic keys and source handles, never durable output IDs."""

    schema_version: Literal["d2-logical-structure-decision-candidate/v3"] = (
        D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION
    )
    logical_objects: tuple[D2UnifiedLogicalObjectDecisionCandidateV3, ...] = Field(
        default=(),
        max_length=8192,
    )
    table_regions: D2TableRegionDecisionCandidateV3
    region_logical_scope_placements: tuple[
        D2RegionLogicalScopePlacementDecisionCandidateV3, ...
    ] = Field(default=(), max_length=16384)
    region_logical_object_placements: tuple[
        D2RegionLogicalObjectPlacementDecisionCandidateV3, ...
    ] = Field(default=(), max_length=16384)
    supplemental_evidence_placements: tuple[
        D2TableSupplementalEvidencePlacementDecisionCandidateV3, ...
    ] = Field(default=(), max_length=12288)
    source_evidence_dispositions: tuple[
        D2UnboundSourceDispositionDecisionCandidateV1, ...
    ] = Field(default=(), max_length=65536)
    unresolved_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("unresolved_codes")
    @classmethod
    def validate_unresolved_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_candidate(self) -> D2LogicalStructureDecisionCandidateV3:
        objects_by_key = {item.object_key: item for item in self.logical_objects}
        if len(objects_by_key) != len(self.logical_objects):
            raise ValueError("V3 logical object keys must be unique")
        for item in self.logical_objects:
            if (
                item.parent_object_key is not None
                and item.parent_object_key not in objects_by_key
            ):
                raise ValueError("V3 object references an unknown parent key")
        _reject_key_cycles(objects_by_key)

        scope_keys = tuple(
            item.logical_scope_key for item in self.table_regions.logical_scopes
        )
        placement_scope_keys = tuple(
            item.logical_scope_key for item in self.region_logical_scope_placements
        )
        if (
            len(scope_keys) != len(set(scope_keys))
            or len(placement_scope_keys) != len(set(placement_scope_keys))
            or set(scope_keys) != set(placement_scope_keys)
        ):
            raise ValueError("every region logical scope requires one key placement")
        if any(
            item.parent_object_key is not None
            and item.parent_object_key not in objects_by_key
            for item in self.region_logical_scope_placements
        ):
            raise ValueError("region scope placement references an unknown parent key")

        region_object_keys = {
            item.logical_object_key
            for item in self.table_regions.regions
            if isinstance(item, D2LogicalObjectPartTableRegionCandidateV1)
        }
        placed_region_object_keys = tuple(
            key
            for item in self.region_logical_object_placements
            for key in item.source_region_logical_object_keys
        )
        target_keys = tuple(
            item.target_object_key for item in self.region_logical_object_placements
        )
        if len(target_keys) != len(set(target_keys)):
            raise ValueError("each unified target object allows one region placement")
        if (
            len(placed_region_object_keys) != len(set(placed_region_object_keys))
            or set(placed_region_object_keys) != region_object_keys
        ):
            raise ValueError(
                "every LOGICAL_OBJECT_PART key requires one explicit placement"
            )
        if any(key not in objects_by_key for key in target_keys):
            raise ValueError("region object placement references an unknown target key")

        region_keys = {item.region_key for item in self.table_regions.regions}
        supplemental_keys = tuple(
            (item.physical_table_id, item.evidence_kind)
            for item in self.supplemental_evidence_placements
        )
        if len(supplemental_keys) != len(set(supplemental_keys)):
            raise ValueError("supplemental evidence placements must be unique")
        for item in self.supplemental_evidence_placements:
            if (
                item.owner_object_key is not None
                and item.owner_object_key not in objects_by_key
            ):
                raise ValueError("supplemental placement has an unknown object owner")
            if (
                item.owner_logical_scope_key is not None
                and item.owner_logical_scope_key not in set(scope_keys)
            ):
                raise ValueError("supplemental placement has an unknown scope owner")
            if (
                item.nested_owner_region_key is not None
                and item.nested_owner_region_key not in region_keys
            ):
                raise ValueError("supplemental placement has an unknown region owner")
        return self


class D2LogicalStructureCompilationV3(_StrictFrozenModel):
    schema_version: Literal["d2-logical-structure-compilation/v3"] = (
        D2_LOGICAL_STRUCTURE_COMPILATION_V3_SCHEMA_VERSION
    )
    private_stage_input_only: Literal[True] = True
    base_compilation: D2LogicalStructureCompilationV1
    table_region_artifact: D2TableRegionArtifactV1
    region_parent_input: D2TableRegionParentInputCompilationV1
    placement_plan: D2RegionPlacementPlanV1
    final_compilation: D2LogicalStructureCompilationV2

    @model_validator(mode="after")
    def validate_compilation(self) -> D2LogicalStructureCompilationV3:
        base = self.base_compilation
        final = self.final_compilation
        if (
            self.region_parent_input.stage_input.region_artifact_id
            != self.table_region_artifact.artifact_id
            or self.region_parent_input.stage_input.region_artifact_sha256
            != self.table_region_artifact.artifact_sha256
            or self.placement_plan.base_proposal_sha256 != base.proposal.proposal_sha256
            or self.placement_plan.base_validation_receipt_sha256
            != base.validation_receipt.receipt_sha256
            or self.placement_plan.table_region_artifact_id
            != self.table_region_artifact.artifact_id
            or self.placement_plan.table_region_artifact_sha256
            != self.table_region_artifact.artifact_sha256
            or final.proposal.region_placement_plan_sha256
            != self.placement_plan.plan_sha256
            or final.validation_receipt.proposal_sha256
            != final.proposal.proposal_sha256
        ):
            raise ValueError("V3 compiler stages do not share immutable lineage")
        return self


def compile_and_validate_d2_logical_structure_candidate_v3(
    candidate: D2LogicalStructureDecisionCandidateV3,
    *,
    source_ir: SourceIRSnapshot,
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureCompilationV3:
    """Compile the V3 runner wire through the existing Host validators."""

    candidate = D2LogicalStructureDecisionCandidateV3.model_validate(
        candidate.model_dump(mode="json")
    )
    source_ir = SourceIRSnapshot.model_validate(source_ir.model_dump(mode="json"))
    source_tables = _source_table_catalog(source_ir)
    base_object_candidates, base_keys = _build_base_object_candidates(candidate)
    placeholder_objects, placeholder_scopes, placeholder_dispositions = (
        _build_host_table_placeholders(source_tables)
    )
    if not base_object_candidates and not placeholder_objects:
        raise ValueError("V3 candidate has no bindable base or table evidence")
    base_wire = D2LogicalStructureDecisionCandidateV2(
        logical_objects=(*base_object_candidates, *placeholder_objects),
        logical_table_scopes=placeholder_scopes,
        source_evidence_dispositions=candidate.source_evidence_dispositions,
        physical_table_dispositions=placeholder_dispositions,
        unresolved_codes=candidate.unresolved_codes,
    )
    base_compilation = compile_and_validate_d2_logical_structure_candidate(
        base_wire,
        source_ir=source_ir,
        expected_d1_artifact=expected_d1_artifact,
        expected_d1_table_map_artifact=expected_d1_table_map_artifact,
        expected_kit_identity=expected_kit_identity,
    )
    base_ids_by_key = _resolve_base_object_ids(
        source_ir,
        base_object_candidates,
    )
    base_non_table_ids = {
        item.logical_object_id
        for item in base_compilation.proposal.logical_objects
        if item.object_kind is not D2LogicalObjectKind.TABLE
    }
    if set(base_ids_by_key.values()) != base_non_table_ids:
        raise ValueError("Host base object-key lineage failed to close")
    if set(base_ids_by_key) != base_keys:
        raise ValueError("Host base object-key mapping is incomplete")

    region_wire = D2TableRegionCandidateArtifactV1(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=(
            d2_table_region_source_ir_canonical_sha256(source_ir)
        ),
        kit_bundle_sha256=expected_kit_identity.bundle_sha256,
        d1_table_map_artifact_id=expected_d1_table_map_artifact.artifact_id,
        d1_table_map_artifact_sha256=(expected_d1_table_map_artifact.artifact_sha256),
        regions=candidate.table_regions.regions,
        logical_scopes=candidate.table_regions.logical_scopes,
        empty_table_dispositions=(candidate.table_regions.empty_table_dispositions),
    )
    table_region_artifact = compile_d2_table_regions(
        source_ir,
        region_wire,
        expected_kit_bundle_sha256=expected_kit_identity.bundle_sha256,
        expected_d1_table_map_artifact_id=(expected_d1_table_map_artifact.artifact_id),
        expected_d1_table_map_artifact_sha256=(
            expected_d1_table_map_artifact.artifact_sha256
        ),
        gap_policy=D2TableRegionGapPolicy.REJECT,
    )
    region_parent_input = build_d2_table_region_parent_input(
        source_ir,
        table_region_artifact,
        expected_kit_bundle_sha256=expected_kit_identity.bundle_sha256,
        expected_d1_table_map_artifact_id=(expected_d1_table_map_artifact.artifact_id),
        expected_d1_table_map_artifact_sha256=(
            expected_d1_table_map_artifact.artifact_sha256
        ),
        expected_region_artifact_id=table_region_artifact.artifact_id,
        expected_region_artifact_sha256=table_region_artifact.artifact_sha256,
    )

    placement_plan = _build_host_placement_plan(
        candidate,
        source_ir=source_ir,
        source_tables=source_tables,
        base_compilation=base_compilation,
        base_ids_by_key=base_ids_by_key,
        table_region_artifact=table_region_artifact,
        region_parent_input=region_parent_input,
    )
    final_compilation = compile_d2_logical_structure_v2(
        source_ir,
        base_proposal=base_compilation.proposal,
        base_validation_receipt=base_compilation.validation_receipt,
        table_region_artifact=table_region_artifact,
        placement_plan=placement_plan,
        expected_kit_identity=expected_kit_identity,
    )
    return D2LogicalStructureCompilationV3(
        base_compilation=base_compilation,
        table_region_artifact=table_region_artifact,
        region_parent_input=region_parent_input,
        placement_plan=placement_plan,
        final_compilation=final_compilation,
    )


def _build_base_object_candidates(
    candidate: D2LogicalStructureDecisionCandidateV3,
) -> tuple[tuple[D2LogicalObjectDecisionCandidateV2, ...], set[str]]:
    objects_by_key = {item.object_key: item for item in candidate.logical_objects}
    direct_keys = {
        item.object_key
        for item in candidate.logical_objects
        if item.source_ranges or item.source_spans
    }
    base_keys = set(direct_keys)
    for key in tuple(direct_keys):
        parent_key = objects_by_key[key].parent_object_key
        while parent_key is not None:
            base_keys.add(parent_key)
            parent_key = objects_by_key[parent_key].parent_object_key
    for key in base_keys:
        parent_key = objects_by_key[key].parent_object_key
        if parent_key is not None and parent_key not in base_keys:
            raise ValueError("base object cannot depend on a region-only parent")
    result = tuple(
        D2LogicalObjectDecisionCandidateV2(
            object_key=item.object_key,
            object_kind=item.object_kind,
            parent_object_key=item.parent_object_key,
            source_ranges=item.source_ranges,
            source_spans=item.source_spans,
            logical_table_scope_key=None,
            reason_codes=item.reason_codes,
        )
        for item in candidate.logical_objects
        if item.object_key in base_keys
    )
    return result, base_keys


def _build_host_table_placeholders(
    source_tables: dict[str, tuple[SourceNode, TableIR]],
) -> tuple[
    tuple[D2LogicalObjectDecisionCandidateV2, ...],
    tuple[D2LogicalTableScopeDecisionCandidateV1, ...],
    tuple[D2LogicalScopePartTableDispositionDecisionCandidateV1, ...],
]:
    objects: list[D2LogicalObjectDecisionCandidateV2] = []
    scopes: list[D2LogicalTableScopeDecisionCandidateV1] = []
    dispositions: list[D2LogicalScopePartTableDispositionDecisionCandidateV1] = []
    for index, table_id in enumerate(source_tables):
        scope_key = f"host-placeholder-scope-{index:05d}"
        object_key = f"host-placeholder-table-{index:05d}"
        scopes.append(
            D2LogicalTableScopeDecisionCandidateV1(
                scope_key=scope_key,
                physical_table_ids=(table_id,),
                reason_codes=("host_lineage_placeholder",),
            )
        )
        objects.append(
            D2LogicalObjectDecisionCandidateV2(
                object_key=object_key,
                object_kind=D2LogicalObjectKind.TABLE,
                logical_table_scope_key=scope_key,
                reason_codes=("host_lineage_placeholder",),
            )
        )
        dispositions.append(
            D2LogicalScopePartTableDispositionDecisionCandidateV1(
                disposition=D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART,
                table_id=table_id,
                logical_table_scope_key=scope_key,
                reason_codes=("host_lineage_placeholder",),
            )
        )
    return tuple(objects), tuple(scopes), tuple(dispositions)


def _resolve_base_object_ids(
    source_ir: SourceIRSnapshot,
    candidates: tuple[D2LogicalObjectDecisionCandidateV2, ...],
) -> dict[str, str]:
    candidates_by_key = {item.object_key: item for item in candidates}
    node_catalog = {item.source_node_id: item for item in source_ir.nodes}
    cell_catalog = _cell_catalog(source_ir)
    segments_by_key: dict[str, tuple[D2SourceSegmentV1, ...]] = {}
    for item in candidates:
        expanded = _expand_candidate_source_spans(
            item.source_spans,
            source_ir=source_ir,
        )
        selections = {
            (
                selected.handle_kind,
                selected.source_handle,
                selected.start_offset,
                selected.end_offset,
            ): selected
            for selected in (*item.source_ranges, *expanded)
        }
        segments_by_key[item.object_key] = tuple(
            sorted(
                (
                    _resolve_source_segment(
                        selected,
                        source_ir=source_ir,
                        node_catalog=node_catalog,
                        cell_catalog=cell_catalog,
                    )
                    for selected in selections.values()
                ),
                key=lambda segment: (
                    segment.source_order,
                    segment.source_handle,
                    -1 if segment.start_offset is None else segment.start_offset,
                    segment.segment_id,
                ),
            )
        )

    def descendants(key: str) -> tuple[D2SourceSegmentV1, ...]:
        values = tuple(
            segment
            for child_key, child in candidates_by_key.items()
            if child.parent_object_key == key
            for segment in (*segments_by_key[child_key], *descendants(child_key))
        )
        return tuple(
            sorted(
                values,
                key=lambda segment: (
                    segment.source_order,
                    segment.source_handle,
                    -1 if segment.start_offset is None else segment.start_offset,
                    segment.segment_id,
                ),
            )
        )

    result: dict[str, str] = {}

    def resolve(key: str) -> str:
        if key in result:
            return result[key]
        item = candidates_by_key[key]
        segments = segments_by_key[key]
        if segments:
            object_id = stable_d2_logical_object_id(
                source_sha256=source_ir.source_sha256,
                object_kind=item.object_kind,
                source_segment_ids=tuple(segment.segment_id for segment in segments),
            )
        else:
            descendant_segments = descendants(key)
            if not descendant_segments:
                raise ValueError("base container has no descendant source evidence")
            object_id = stable_d2_derived_container_id(
                source_sha256=source_ir.source_sha256,
                object_kind=item.object_kind,
                descendant_source_segment_ids=tuple(
                    segment.segment_id for segment in descendant_segments
                ),
                descendant_logical_table_scope_ids=(),
                parent_logical_object_id=(
                    resolve(item.parent_object_key)
                    if item.parent_object_key is not None
                    else None
                ),
            )
        result[key] = object_id
        return object_id

    for key in candidates_by_key:
        resolve(key)
    if any(count > 1 for count in Counter(result.values()).values()):
        raise ValueError("V3 base object keys collapse to duplicate stable identities")
    return result


def _build_host_placement_plan(
    candidate: D2LogicalStructureDecisionCandidateV3,
    *,
    source_ir: SourceIRSnapshot,
    source_tables: dict[str, tuple[SourceNode, TableIR]],
    base_compilation: D2LogicalStructureCompilationV1,
    base_ids_by_key: dict[str, str],
    table_region_artifact: D2TableRegionArtifactV1,
    region_parent_input: D2TableRegionParentInputCompilationV1,
) -> D2RegionPlacementPlanV1:
    objects_by_key = {item.object_key: item for item in candidate.logical_objects}
    scope_by_key = {
        item.candidate_scope_key: item for item in table_region_artifact.logical_scopes
    }
    region_object_by_key = {
        item.candidate_object_key: item
        for item in table_region_artifact.logical_objects
    }
    region_by_key = {
        item.candidate_region_key: item for item in table_region_artifact.regions
    }
    scope_placement_by_key = {
        item.logical_scope_key: item
        for item in candidate.region_logical_scope_placements
    }
    object_placement_by_target = {
        item.target_object_key: item
        for item in candidate.region_logical_object_placements
    }
    if set(scope_placement_by_key) != set(scope_by_key):
        raise ValueError("compiled region scopes differ from V3 key placements")
    placed_region_keys = {
        key
        for item in candidate.region_logical_object_placements
        for key in item.source_region_logical_object_keys
    }
    if placed_region_keys != set(region_object_by_key):
        raise ValueError("compiled region objects differ from V3 key placements")

    new_ids_by_key: dict[str, str] = {}
    for target_key, placement in object_placement_by_target.items():
        if target_key in base_ids_by_key:
            continue
        target = objects_by_key[target_key]
        source_object_ids = {
            region_object_by_key[key].logical_object_id
            for key in placement.source_region_logical_object_keys
        }
        ordered_region_ids = tuple(
            item.region_id
            for item in table_region_artifact.regions
            if item.logical_object_id in source_object_ids
        )
        new_ids_by_key[target_key] = stable_d2_v2_region_logical_object_id(
            source_ir_canonical_sha256=(
                table_region_artifact.source_ir_canonical_sha256
            ),
            object_kind=target.object_kind,
            ordered_region_ids=ordered_region_ids,
        )
    final_ids_by_key = {**base_ids_by_key, **new_ids_by_key}
    missing_object_keys = set(objects_by_key) - set(final_ids_by_key)
    if missing_object_keys:
        raise ValueError(
            "every V3 object requires base evidence or a region-object placement"
        )

    def parent_id(object_key: str) -> str | None:
        parent_key = objects_by_key[object_key].parent_object_key
        return None if parent_key is None else final_ids_by_key[parent_key]

    retained = tuple(
        D2RetainedBaseObjectPlacementV1(
            base_logical_object_id=object_id,
            parent_logical_object_id=parent_id(object_key),
            reason_codes=("host_retained_unified_object",),
        )
        for object_key, object_id in base_ids_by_key.items()
    )
    base_objects_by_id = {
        item.logical_object_id: item
        for item in base_compilation.proposal.logical_objects
    }
    for object_key, object_id in base_ids_by_key.items():
        if base_objects_by_id[object_id].parent_logical_object_id != parent_id(
            object_key
        ):
            raise ValueError("V3 retained base hierarchy drifted")

    region_scope_placements = tuple(
        D2RegionLogicalScopePlacementV1(
            logical_scope_id=scope_by_key[key].logical_scope_id,
            parent_logical_object_id=(
                None
                if item.parent_object_key is None
                else final_ids_by_key[item.parent_object_key]
            ),
            reason_codes=item.reason_codes,
        )
        for key, item in scope_placement_by_key.items()
    )
    region_object_placements: list[D2RegionLogicalObjectPlacementV1] = []
    for target_key, item in object_placement_by_target.items():
        source_ids = tuple(
            region_object_by_key[key].logical_object_id
            for key in item.source_region_logical_object_keys
        )
        if target_key in base_ids_by_key:
            mode = D2RegionLogicalObjectPlacementMode.MERGE_RETAINED_BASE
            target_base_id = base_ids_by_key[target_key]
            object_kind = None
        else:
            mode = D2RegionLogicalObjectPlacementMode.NEW_REGION_OBJECT
            target_base_id = None
            object_kind = objects_by_key[target_key].object_kind
        region_object_placements.append(
            D2RegionLogicalObjectPlacementV1(
                placement_key=target_key,
                source_region_logical_object_ids=source_ids,
                mode=mode,
                target_base_logical_object_id=target_base_id,
                object_kind=object_kind,
                parent_logical_object_id=parent_id(target_key),
                reason_codes=item.reason_codes,
            )
        )

    placeholder_table_by_physical_id = _placeholder_table_object_by_physical_id(
        base_compilation
    )
    replacements = tuple(
        D2BaseTableReplacementCandidateV1(
            base_logical_object_id=placeholder_table_by_physical_id[table_id],
            disposition=(
                D2BaseTableReplacementDisposition.PRESERVED_AS_UNRESOLVED_LINEAGE
                if _physical_table_is_unresolved(
                    table_id,
                    table_region_artifact=table_region_artifact,
                )
                else D2BaseTableReplacementDisposition.REPLACED_BY_REGION_TOPOLOGY
            ),
            reason_codes=("host_replaced_lineage_placeholder",),
        )
        for table_id in source_tables
    )
    supplemental = _build_supplemental_placements(
        candidate,
        source_ir=source_ir,
        source_tables=source_tables,
        table_region_artifact=table_region_artifact,
        region_parent_input=region_parent_input,
        base_ids_by_key=base_ids_by_key,
        new_ids_by_key=new_ids_by_key,
        scope_by_key=scope_by_key,
        region_by_key=region_by_key,
    )
    return build_d2_region_placement_plan(
        base_proposal=base_compilation.proposal,
        base_validation_receipt=base_compilation.validation_receipt,
        table_region_artifact=table_region_artifact,
        retained_base_objects=retained,
        base_table_replacements=replacements,
        region_logical_scope_placements=region_scope_placements,
        region_logical_object_placements=tuple(region_object_placements),
        supplemental_evidence_placements=supplemental,
    )


def _placeholder_table_object_by_physical_id(
    base_compilation: D2LogicalStructureCompilationV1,
) -> dict[str, str]:
    scopes_by_id = {
        item.logical_object_id: item
        for item in base_compilation.proposal.logical_table_scopes
    }
    result: dict[str, str] = {}
    for item in base_compilation.proposal.logical_objects:
        if item.object_kind is not D2LogicalObjectKind.TABLE:
            continue
        scope = scopes_by_id[item.logical_table_scope_ids[0]]
        if len(scope.physical_table_ids) != 1:
            raise ValueError(
                "Host table placeholder must bind exactly one physical table"
            )
        table_id = scope.physical_table_ids[0]
        if table_id in result:
            raise ValueError("physical table has multiple Host placeholders")
        result[table_id] = item.logical_object_id
    return result


def _physical_table_is_unresolved(
    table_id: str,
    *,
    table_region_artifact: D2TableRegionArtifactV1,
) -> bool:
    return table_id in set(table_region_artifact.unresolved_empty_table_ids) or any(
        item.physical_table_id == table_id
        and item.disposition is D2TableRegionDispositionKind.UNRESOLVED
        for item in table_region_artifact.regions
    )


def _build_supplemental_placements(
    candidate: D2LogicalStructureDecisionCandidateV3,
    *,
    source_ir: SourceIRSnapshot,
    source_tables: dict[str, tuple[SourceNode, TableIR]],
    table_region_artifact: D2TableRegionArtifactV1,
    region_parent_input: D2TableRegionParentInputCompilationV1,
    base_ids_by_key: dict[str, str],
    new_ids_by_key: dict[str, str],
    scope_by_key: dict[str, object],
    region_by_key: dict[str, object],
) -> tuple[D2TableSupplementalEvidencePlacementV1, ...]:
    coverage_by_id = {
        item.physical_table_id: item
        for item in table_region_artifact.physical_table_cell_coverage
    }
    residual_by_id = {
        item.physical_table_id: item
        for item in region_parent_input.stage_input.supplemental_table_evidence
    }
    result: list[D2TableSupplementalEvidencePlacementV1] = []
    for item in candidate.supplemental_evidence_placements:
        source_table = source_tables.get(item.physical_table_id)
        if source_table is None:
            raise ValueError("supplemental placement references an unknown table")
        node, table = source_table
        coverage = coverage_by_id[item.physical_table_id]
        residual = residual_by_id[item.physical_table_id]
        if item.evidence_kind is D2TableSupplementalEvidenceKindV1.TABLE_NODE_RAW_TEXT:
            evidence_ref = f"table-node-raw-text:{item.physical_table_id}"
            evidence_sha256 = residual.table_node_raw_text_sha256
            present = bool(node.raw_text)
        elif item.evidence_kind is D2TableSupplementalEvidenceKindV1.TABLE_NOTES:
            evidence_ref = f"table-notes:{item.physical_table_id}"
            evidence_sha256 = residual.table_notes_sha256
            present = bool(table.notes)
        else:
            evidence_ref = f"table-visual:{item.physical_table_id}"
            evidence_sha256 = _canonical_sha256(
                [
                    value.model_dump(mode="json")
                    for value in coverage.supplemental_visual_evidence
                ]
            )
            present = bool(coverage.supplemental_visual_evidence)
        if not present:
            raise ValueError("runner cannot disposition absent supplemental evidence")

        owner_reference_kind = None
        owner_reference = None
        owner_scope_id = None
        nested_owner_id = None
        cell_derivation = None
        derived_cell_ids: tuple[str, ...] = ()
        if item.disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_OBJECT:
            owner_key = item.owner_object_key or ""
            if owner_key in base_ids_by_key:
                owner_reference_kind = (
                    D2SupplementalObjectOwnerReferenceKindV1.RETAINED_BASE_OBJECT
                )
                owner_reference = base_ids_by_key[owner_key]
            elif owner_key in new_ids_by_key:
                owner_reference_kind = D2SupplementalObjectOwnerReferenceKindV1.REGION_LOGICAL_OBJECT_PLACEMENT
                owner_reference = owner_key
            else:
                raise ValueError("supplemental object owner has no final object")
        elif item.disposition is D2TableSupplementalDispositionKindV1.OWNED_BY_SCOPE:
            owner_scope_id = scope_by_key[
                item.owner_logical_scope_key or ""
            ].logical_scope_id
        elif item.disposition is D2TableSupplementalDispositionKindV1.NESTED:
            nested_owner_id = region_by_key[
                item.nested_owner_region_key or ""
            ].region_id
        elif item.disposition is D2TableSupplementalDispositionKindV1.CELL_DERIVED:
            cell_derivation = (
                D2TableRawTextCellDerivationV1.ORDERED_CELL_RAW_TEXT_JOIN_LF_V1
            )
            derived_cell_ids = tuple(
                cell.cell_id
                for cell in sorted(
                    table.cells,
                    key=lambda cell: (cell.row, cell.column, cell.cell_id),
                )
            )
        result.append(
            D2TableSupplementalEvidencePlacementV1(
                physical_table_id=item.physical_table_id,
                evidence_kind=item.evidence_kind,
                evidence_ref=evidence_ref,
                evidence_sha256=evidence_sha256,
                disposition=item.disposition,
                owner_reference_kind=owner_reference_kind,
                owner_reference=owner_reference,
                owner_region_logical_scope_id=owner_scope_id,
                nested_owner_region_id=nested_owner_id,
                cell_derivation=cell_derivation,
                derived_cell_ids=derived_cell_ids,
                reason_codes=item.reason_codes,
            )
        )
    return tuple(result)


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


def _reject_key_cycles(
    objects_by_key: dict[str, D2UnifiedLogicalObjectDecisionCandidateV3],
) -> None:
    for key in objects_by_key:
        seen: set[str] = set()
        current: str | None = key
        while current is not None:
            if current in seen:
                raise ValueError("V3 logical object parentage contains a cycle")
            seen.add(current)
            current = objects_by_key[current].parent_object_key


def _validate_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    if value != tuple(sorted(set(value))) or any(
        _REASON_CODE_PATTERN.fullmatch(item) is None for item in value
    ):
        raise ValueError("V3 reason codes must be unique canonical lowercase tokens")
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


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if hasattr(value, "value"):
        return value.value
    return value


__all__ = [
    "D2_LOGICAL_STRUCTURE_COMPILATION_V3_SCHEMA_VERSION",
    "D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_V3_SCHEMA_VERSION",
    "D2LogicalStructureCompilationV3",
    "D2LogicalStructureDecisionCandidateV3",
    "D2RegionLogicalObjectPlacementDecisionCandidateV3",
    "D2RegionLogicalScopePlacementDecisionCandidateV3",
    "D2TableRegionDecisionCandidateV3",
    "D2TableRegionWireCandidateV3",
    "D2TableSupplementalEvidencePlacementDecisionCandidateV3",
    "D2UnifiedLogicalObjectDecisionCandidateV3",
    "compile_and_validate_d2_logical_structure_candidate_v3",
]
