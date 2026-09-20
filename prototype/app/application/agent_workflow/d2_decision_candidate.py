from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import Literal

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
    D2LogicalObjectV1,
    D2LogicalStructureProposalV1,
    D2LogicalStructureStatus,
    D2LogicalStructureValidationReceiptV1,
    D2PhysicalTableDispositionKind,
    D2PhysicalTableDispositionV1,
    D2SourceEvidenceDispositionKind,
    D2SourceEvidenceDispositionV1,
    D2SourceHandleKind,
    D2SourceSegmentV1,
    D2ValidatedD1ArtifactIdentityV1,
    stable_d2_logical_object_id,
    stable_d2_derived_container_id,
    stable_d2_logical_object_sha256,
    stable_d2_logical_structure_proposal_sha256,
    stable_d2_source_disposition_id,
    stable_d2_source_disposition_sha256,
    stable_d2_source_segment_id,
    stable_d2_source_segment_sha256,
    stable_d2_table_disposition_sha256,
    validate_d2_logical_structure_proposal,
)
from app.application.agent_workflow.extraction_runtime import (
    AgentKitIdentityV1,
    AgentStageArtifactStatus,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentD1TableMapArtifactIdentityV1,
    AgentLogicalTableScopeV1,
    build_agent_logical_table_scope,
    source_ir_canonical_sha256,
)
from app.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
)
from app.domain.extraction.structure_revision import canonical_model_sha256


D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_SCHEMA_VERSION = (
    "d2-logical-structure-decision-candidate/v2"
)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class D2CandidateSourceRangeV1(_StrictFrozenModel):
    """Agent-selected SourceIR handle/range without any Host-derived binding."""

    handle_kind: D2SourceHandleKind
    source_handle: str = Field(min_length=1, max_length=160)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)

    @field_validator("source_handle")
    @classmethod
    def reject_blank_handle(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate source handle must not be blank")
        return value

    @model_validator(mode="after")
    def validate_range(self) -> D2CandidateSourceRangeV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("D2 candidate source offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("D2 candidate source range must be non-empty")
        return self


class D2CandidateSourceSpanV2(_StrictFrozenModel):
    """Compact half-open span between two Host-pinned SourceIR node handles."""

    start_source_handle: str = Field(min_length=1, max_length=200)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_source_handle: str = Field(min_length=1, max_length=200)
    end_offset: StrictInt | None = Field(default=None, ge=0)

    @field_validator("start_source_handle", "end_source_handle")
    @classmethod
    def reject_blank_span_handles(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate source span handles must not be blank")
        return value

    @model_validator(mode="after")
    def validate_span(self) -> D2CandidateSourceSpanV2:
        if (
            self.end_source_handle == self.start_source_handle
            and self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("single-node D2 candidate source span must be non-empty")
        return self


class D2LogicalObjectDecisionCandidateV2(_StrictFrozenModel):
    """One temporary object boundary decision; its durable identity is Host-owned."""

    object_key: str = Field(min_length=1, max_length=160)
    object_kind: D2LogicalObjectKind
    parent_object_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    source_ranges: tuple[D2CandidateSourceRangeV1, ...] = Field(
        default=(),
        max_length=4096,
    )
    source_spans: tuple[D2CandidateSourceSpanV2, ...] = Field(
        default=(),
        max_length=4096,
    )
    logical_table_scope_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator(
        "object_key",
        "parent_object_key",
        "logical_table_scope_key",
    )
    @classmethod
    def reject_blank_temporary_keys(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("D2 candidate temporary keys must not be blank")
        return value

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
            raise ValueError("a D2 candidate object cannot repeat a source range")
        return value

    @field_validator("source_spans")
    @classmethod
    def reject_duplicate_spans(
        cls,
        value: tuple[D2CandidateSourceSpanV2, ...],
    ) -> tuple[D2CandidateSourceSpanV2, ...]:
        if len(value) != len(set(value)):
            raise ValueError("a D2 candidate object cannot repeat a source span")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_object_shape(self) -> D2LogicalObjectDecisionCandidateV2:
        if self.parent_object_key == self.object_key:
            raise ValueError("a D2 candidate object cannot own itself")
        is_table = self.object_kind is D2LogicalObjectKind.TABLE
        if bool(self.logical_table_scope_key) != is_table:
            raise ValueError("only a D2 TABLE candidate binds one temporary scope key")
        if (
            not self.source_ranges
            and not self.source_spans
            and not self.logical_table_scope_key
            and self.object_kind
            not in {
                D2LogicalObjectKind.DOCUMENT_UNIT,
                D2LogicalObjectKind.SECTION,
                D2LogicalObjectKind.ATTACHMENT,
                D2LogicalObjectKind.CONTEXT,
            }
        ):
            raise ValueError(
                "only D2 container candidates may derive evidence from children"
            )
        return self


class D2LogicalTableScopeDecisionCandidateV1(_StrictFrozenModel):
    """Agent-owned logical-table/continuation grouping, before Host binding."""

    scope_key: str = Field(min_length=1, max_length=160)
    physical_table_ids: tuple[str, ...] = Field(min_length=1, max_length=512)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("physical_table_ids")
    @classmethod
    def validate_table_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError(
                "D2 candidate scope table IDs must be unique and non-empty"
            )
        return value

    @field_validator("scope_key")
    @classmethod
    def reject_blank_scope_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate scope key must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)


class _D2PhysicalTableDispositionDecisionBaseV1(_StrictFrozenModel):
    table_id: str = Field(min_length=1, max_length=160)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("table_id")
    @classmethod
    def reject_blank_table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate table IDs must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)


class D2LogicalScopePartTableDispositionDecisionCandidateV1(
    _D2PhysicalTableDispositionDecisionBaseV1
):
    disposition: Literal[D2PhysicalTableDispositionKind.LOGICAL_SCOPE_PART]
    logical_table_scope_key: str = Field(min_length=1, max_length=160)

    @field_validator("logical_table_scope_key")
    @classmethod
    def reject_blank_scope_key(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate scope key must not be blank")
        return value


class D2NestedValueTableDispositionDecisionCandidateV1(
    _D2PhysicalTableDispositionDecisionBaseV1
):
    disposition: Literal[D2PhysicalTableDispositionKind.NESTED_VALUE_TABLE]
    nested_owner_table_id: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def validate_owner(self) -> D2NestedValueTableDispositionDecisionCandidateV1:
        if not self.nested_owner_table_id.strip():
            raise ValueError("D2 candidate owner table must not be blank")
        if self.nested_owner_table_id == self.table_id:
            raise ValueError("a nested-value table cannot own itself")
        return self


class D2StandaloneTableDispositionDecisionCandidateV1(
    _D2PhysicalTableDispositionDecisionBaseV1
):
    disposition: Literal[
        D2PhysicalTableDispositionKind.NON_BUSINESS_FALSE_TABLE,
        D2PhysicalTableDispositionKind.UNRESOLVED,
    ]


D2PhysicalTableDispositionDecisionCandidateV1 = (
    D2LogicalScopePartTableDispositionDecisionCandidateV1
    | D2NestedValueTableDispositionDecisionCandidateV1
    | D2StandaloneTableDispositionDecisionCandidateV1
)


class D2UnboundSourceDispositionDecisionCandidateV1(_StrictFrozenModel):
    """Non-object disposition that names its exact SourceIR range directly."""

    disposition: Literal[
        D2SourceEvidenceDispositionKind.NON_BUSINESS_SOURCE,
        D2SourceEvidenceDispositionKind.PAGE_FURNITURE,
        D2SourceEvidenceDispositionKind.UNRESOLVED,
    ]
    source_handle: str = Field(min_length=1, max_length=160)
    start_offset: StrictInt | None = Field(default=None, ge=0)
    end_offset: StrictInt | None = Field(default=None, ge=1)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)

    @field_validator("source_handle")
    @classmethod
    def reject_blank_handle(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D2 candidate source disposition handle must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_disposition_shape(
        self,
    ) -> D2UnboundSourceDispositionDecisionCandidateV1:
        if (self.start_offset is None) != (self.end_offset is None):
            raise ValueError("D2 source disposition offsets must be supplied together")
        if (
            self.start_offset is not None
            and self.end_offset is not None
            and self.end_offset <= self.start_offset
        ):
            raise ValueError("D2 source disposition range must be non-empty")
        return self


class D2LogicalStructureDecisionCandidateV2(_StrictFrozenModel):
    """Minimal runner output: semantic decisions only, never durable lineage."""

    schema_version: Literal["d2-logical-structure-decision-candidate/v2"] = (
        D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_SCHEMA_VERSION
    )
    logical_objects: tuple[D2LogicalObjectDecisionCandidateV2, ...] = Field(
        min_length=1,
        max_length=8192,
    )
    logical_table_scopes: tuple[D2LogicalTableScopeDecisionCandidateV1, ...] = Field(
        default=(),
        max_length=2048,
    )
    source_evidence_dispositions: tuple[
        D2UnboundSourceDispositionDecisionCandidateV1, ...
    ] = Field(default=(), max_length=65536)
    physical_table_dispositions: tuple[
        D2PhysicalTableDispositionDecisionCandidateV1, ...
    ] = Field(default=(), max_length=8192)
    unresolved_codes: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("unresolved_codes")
    @classmethod
    def validate_unresolved_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_reason_codes(value)

    @model_validator(mode="after")
    def validate_unique_temporary_keys(
        self,
    ) -> D2LogicalStructureDecisionCandidateV2:
        object_keys = tuple(item.object_key for item in self.logical_objects)
        if len(object_keys) != len(set(object_keys)):
            raise ValueError("D2 candidate object keys must be unique")
        scope_keys = tuple(item.scope_key for item in self.logical_table_scopes)
        if len(scope_keys) != len(set(scope_keys)):
            raise ValueError("D2 candidate scope keys must be unique")
        table_ids = tuple(item.table_id for item in self.physical_table_dispositions)
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("D2 candidate physical tables require one decision")
        return self


class D2LogicalStructureCompilationV1(_StrictFrozenModel):
    proposal: D2LogicalStructureProposalV1
    validation_receipt: D2LogicalStructureValidationReceiptV1

    @model_validator(mode="after")
    def validate_compilation(self) -> D2LogicalStructureCompilationV1:
        if (
            self.validation_receipt.proposal_sha256 != self.proposal.proposal_sha256
            or self.validation_receipt.run_id != self.proposal.run_id
            or self.validation_receipt.source_sha256 != self.proposal.source_sha256
        ):
            raise ValueError("compiled D2 proposal and receipt lineage differ")
        return self


def compile_and_validate_d2_logical_structure_candidate(
    candidate: D2LogicalStructureDecisionCandidateV2,
    *,
    source_ir: SourceIRSnapshot,
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> D2LogicalStructureCompilationV1:
    """Host-compile a minimal wire decision into the canonical validated D2 artifact.

    The runner never supplies source identity, predecessor/Kit lineage, stable IDs,
    page/order facts, evidence digests, or artifact hashes. This function derives
    those values from the exact current inputs and delegates final closure to the
    existing D2 Host validator.
    """

    _validate_host_lineage(
        source_ir=source_ir,
        expected_d1_artifact=expected_d1_artifact,
        expected_d1_table_map_artifact=expected_d1_table_map_artifact,
        expected_kit_identity=expected_kit_identity,
    )
    node_catalog = {node.source_node_id: node for node in source_ir.nodes}
    if len(node_catalog) != len(source_ir.nodes):
        raise ValueError("SourceIR contains ambiguous source-node handles")
    cell_catalog = _cell_catalog(source_ir)
    table_catalog = _table_catalog(source_ir)

    scope_candidates = {item.scope_key: item for item in candidate.logical_table_scopes}
    scopes_by_key = {
        scope_key: build_agent_logical_table_scope(
            source_ir,
            d1_table_map_artifact=expected_d1_table_map_artifact,
            physical_table_ids=scope_candidate.physical_table_ids,
            reason_codes=_canonical_reason_codes(scope_candidate.reason_codes),
        )
        for scope_key, scope_candidate in scope_candidates.items()
    }

    object_candidates = {item.object_key: item for item in candidate.logical_objects}
    _validate_parent_keys_and_cycles(object_candidates)
    object_candidates = _prune_empty_leaf_containers(object_candidates)
    _validate_scope_ownership(object_candidates, scopes_by_key)

    segments_by_object_key: dict[str, tuple[D2SourceSegmentV1, ...]] = {}
    object_ids_by_key: dict[str, str] = {}
    for object_candidate in object_candidates.values():
        expanded_ranges = _expand_candidate_source_spans(
            object_candidate.source_spans,
            source_ir=source_ir,
        )
        source_ranges_by_key = {
            (
                source_range.handle_kind,
                source_range.source_handle,
                source_range.start_offset,
                source_range.end_offset,
            ): source_range
            for source_range in (*object_candidate.source_ranges, *expanded_ranges)
        }
        selected_segments = tuple(
            _resolve_source_segment(
                source_range,
                source_ir=source_ir,
                node_catalog=node_catalog,
                cell_catalog=cell_catalog,
            )
            for source_range in source_ranges_by_key.values()
        )
        scope = (
            scopes_by_key[object_candidate.logical_table_scope_key]
            if object_candidate.logical_table_scope_key is not None
            else None
        )
        if scope is not None:
            physical_source_node_ids = {
                part.source_node_id for part in scope.physical_parts
            }
            selected_segments = tuple(
                segment
                for segment in selected_segments
                if segment.owner_source_node_id not in physical_source_node_ids
            )
            selected_segments = (
                *selected_segments,
                *(
                    _resolve_source_segment(
                        D2CandidateSourceRangeV1(
                            handle_kind=D2SourceHandleKind.SOURCE_NODE,
                            source_handle=part.source_node_id,
                        ),
                        source_ir=source_ir,
                        node_catalog=node_catalog,
                        cell_catalog=cell_catalog,
                    )
                    for part in scope.physical_parts
                ),
            )
        segments = tuple(
            sorted(
                selected_segments,
                key=lambda item: (
                    item.source_order,
                    item.source_handle,
                    -1 if item.start_offset is None else item.start_offset,
                    item.segment_id,
                ),
            )
        )
        segments_by_object_key[object_candidate.object_key] = segments

    def resolve_object_id(object_key: str) -> str:
        existing = object_ids_by_key.get(object_key)
        if existing is not None:
            return existing
        object_candidate = object_candidates[object_key]
        segments = segments_by_object_key[object_key]
        if object_candidate.logical_table_scope_key is not None:
            logical_object_id = scopes_by_key[
                object_candidate.logical_table_scope_key
            ].logical_object_id
        elif segments:
            logical_object_id = stable_d2_logical_object_id(
                source_sha256=source_ir.source_sha256,
                object_kind=object_candidate.object_kind,
                source_segment_ids=tuple(item.segment_id for item in segments),
            )
        else:
            identity_segments = _descendant_source_segments(
                object_key,
                object_candidates=object_candidates,
                segments_by_object_key=segments_by_object_key,
            )
            identity_scopes = _descendant_logical_table_scopes(
                object_key,
                object_candidates=object_candidates,
                scopes_by_key=scopes_by_key,
            )
            parent_id = (
                resolve_object_id(object_candidate.parent_object_key)
                if object_candidate.parent_object_key is not None
                else None
            )
            logical_object_id = stable_d2_derived_container_id(
                source_sha256=source_ir.source_sha256,
                object_kind=object_candidate.object_kind,
                descendant_source_segment_ids=tuple(
                    item.segment_id for item in identity_segments
                ),
                descendant_logical_table_scope_ids=tuple(
                    item.logical_object_id for item in identity_scopes
                ),
                parent_logical_object_id=parent_id,
            )
        object_ids_by_key[object_key] = logical_object_id
        return logical_object_id

    for object_candidate in object_candidates.values():
        resolve_object_id(object_candidate.object_key)

    duplicate_object_ids = tuple(
        object_id
        for object_id, count in Counter(object_ids_by_key.values()).items()
        if count > 1
    )
    if duplicate_object_ids:
        raise ValueError("D2 candidate objects collapse to duplicate stable identities")

    logical_objects = tuple(
        sorted(
            (
                _compile_logical_object(
                    object_candidate,
                    source_segments=segments_by_object_key[object_candidate.object_key],
                    descendant_source_segments=_descendant_source_segments(
                        object_candidate.object_key,
                        object_candidates=object_candidates,
                        segments_by_object_key=segments_by_object_key,
                    ),
                    descendant_logical_table_scopes=(
                        _descendant_logical_table_scopes(
                            object_candidate.object_key,
                            object_candidates=object_candidates,
                            scopes_by_key=scopes_by_key,
                        )
                    ),
                    logical_object_id=object_ids_by_key[object_candidate.object_key],
                    parent_logical_object_id=(
                        object_ids_by_key[object_candidate.parent_object_key]
                        if object_candidate.parent_object_key is not None
                        else None
                    ),
                    scopes_by_key=scopes_by_key,
                )
                for object_candidate in object_candidates.values()
            ),
            key=lambda item: (item.source_order, item.logical_object_id),
        )
    )

    source_dispositions = _compile_source_dispositions(
        candidate.source_evidence_dispositions,
        source_ir=source_ir,
        node_catalog=node_catalog,
        logical_objects_by_key={
            item.object_key: logical_object
            for item in object_candidates.values()
            for logical_object in logical_objects
            if logical_object.logical_object_id == object_ids_by_key[item.object_key]
        },
        segments_by_object_key=segments_by_object_key,
    )
    table_dispositions = _compile_table_dispositions(
        candidate.physical_table_dispositions,
        table_catalog=table_catalog,
        scopes_by_key=scopes_by_key,
    )
    logical_table_scopes = tuple(
        sorted(
            scopes_by_key.values(),
            key=lambda item: (
                item.physical_parts[0].source_order,
                item.logical_object_id,
            ),
        )
    )
    partial = (
        source_ir.status is SourceIRStatus.PARTIAL
        or expected_d1_artifact.status is AgentStageArtifactStatus.PARTIAL
        or any(
            item.disposition is D2PhysicalTableDispositionKind.UNRESOLVED
            for item in table_dispositions
        )
        or any(
            item.disposition is D2SourceEvidenceDispositionKind.UNRESOLVED
            for item in source_dispositions
        )
        or bool(candidate.unresolved_codes)
    )
    proposal_values = {
        "run_id": expected_d1_artifact.run_id,
        "source_sha256": source_ir.source_sha256,
        "source_ir_id": source_ir.source_ir_id,
        "source_ir_canonical_sha256": source_ir_canonical_sha256(source_ir),
        "kit_identity": expected_kit_identity,
        "predecessor_d1_artifact": expected_d1_artifact,
        "d1_table_map_artifact": expected_d1_table_map_artifact,
        "status": (
            D2LogicalStructureStatus.PARTIAL
            if partial
            else D2LogicalStructureStatus.COMPLETE
        ),
        "logical_objects": logical_objects,
        "logical_table_scopes": logical_table_scopes,
        "source_evidence_dispositions": source_dispositions,
        "physical_table_dispositions": table_dispositions,
        "unresolved_codes": _canonical_reason_codes(candidate.unresolved_codes),
    }
    draft = D2LogicalStructureProposalV1.model_construct(
        **proposal_values,
        proposal_sha256="0" * 64,
    )
    proposal = D2LogicalStructureProposalV1(
        **proposal_values,
        proposal_sha256=stable_d2_logical_structure_proposal_sha256(draft),
    )
    receipt = validate_d2_logical_structure_proposal(
        source_ir,
        proposal,
        expected_d1_artifact=expected_d1_artifact,
        expected_d1_table_map_artifact=expected_d1_table_map_artifact,
        expected_kit_identity=expected_kit_identity,
    )
    return D2LogicalStructureCompilationV1(
        proposal=proposal,
        validation_receipt=receipt,
    )


def _validate_host_lineage(
    *,
    source_ir: SourceIRSnapshot,
    expected_d1_artifact: D2ValidatedD1ArtifactIdentityV1,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    expected_kit_identity: AgentKitIdentityV1,
) -> None:
    if source_ir.status is SourceIRStatus.FAILED:
        raise ValueError("D2 cannot compile a failed SourceIR snapshot")
    if (
        expected_d1_artifact.source_sha256 != source_ir.source_sha256
        or expected_d1_table_map_artifact.source_sha256 != source_ir.source_sha256
    ):
        raise ValueError("D2 Host inputs belong to another source")
    if expected_d1_artifact.kit_bundle_sha256 != expected_kit_identity.bundle_sha256:
        raise ValueError("D2 Host D1/Kit lineage is inconsistent")


def _validate_parent_keys_and_cycles(
    object_candidates: dict[str, D2LogicalObjectDecisionCandidateV2],
) -> None:
    for item in object_candidates.values():
        if (
            item.parent_object_key is not None
            and item.parent_object_key not in object_candidates
        ):
            raise ValueError("D2 candidate object references an unknown parent key")
    for object_key in object_candidates:
        seen: set[str] = set()
        current: str | None = object_key
        while current is not None:
            if current in seen:
                raise ValueError("D2 candidate object parentage contains a cycle")
            seen.add(current)
            current = object_candidates[current].parent_object_key


def _prune_empty_leaf_containers(
    object_candidates: dict[str, D2LogicalObjectDecisionCandidateV2],
) -> dict[str, D2LogicalObjectDecisionCandidateV2]:
    retained = dict(object_candidates)
    container_kinds = {
        D2LogicalObjectKind.DOCUMENT_UNIT,
        D2LogicalObjectKind.SECTION,
        D2LogicalObjectKind.ATTACHMENT,
        D2LogicalObjectKind.CONTEXT,
    }
    while True:
        parent_keys = {
            item.parent_object_key
            for item in retained.values()
            if item.parent_object_key is not None
        }
        removable = tuple(
            key
            for key, item in retained.items()
            if item.object_kind in container_kinds
            and not item.source_ranges
            and not item.source_spans
            and item.logical_table_scope_key is None
            and key not in parent_keys
        )
        if not removable:
            return retained
        for key in removable:
            retained.pop(key)


def _validate_scope_ownership(
    object_candidates: dict[str, D2LogicalObjectDecisionCandidateV2],
    scopes_by_key: dict[str, AgentLogicalTableScopeV1],
) -> None:
    referenced_scope_keys = tuple(
        item.logical_table_scope_key
        for item in object_candidates.values()
        if item.logical_table_scope_key is not None
    )
    unknown = set(referenced_scope_keys) - set(scopes_by_key)
    if unknown:
        raise ValueError("D2 candidate object references an unknown scope key")
    if Counter(referenced_scope_keys) != Counter({key: 1 for key in scopes_by_key}):
        raise ValueError("every D2 candidate scope requires exactly one TABLE owner")


def _resolve_source_segment(
    selection: D2CandidateSourceRangeV1,
    *,
    source_ir: SourceIRSnapshot,
    node_catalog: dict[str, SourceNode],
    cell_catalog: dict[str, tuple[SourceNode, TableCellIR]],
) -> D2SourceSegmentV1:
    if selection.handle_kind is D2SourceHandleKind.SOURCE_NODE:
        owner = node_catalog.get(selection.source_handle)
        if owner is None:
            raise ValueError("D2 candidate references an unknown source handle")
        source_text = owner.raw_text
        content_sha256 = d2_source_node_content_sha256(owner)
        page_number = owner.native_locator.page_number
    else:
        resolved = cell_catalog.get(selection.source_handle)
        if resolved is None:
            raise ValueError("D2 candidate references an unknown table-cell handle")
        owner, cell = resolved
        source_text = cell.raw_text
        content_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        page_number = cell.native_locator.page_number
    start_offset, end_offset = _resolve_selected_range(
        source_text=source_text,
        start_offset=selection.start_offset,
        end_offset=selection.end_offset,
    )
    if page_number is None:
        raise ValueError("D2 candidate source handle lacks validated page evidence")
    pages = (page_number,)
    segment_id = stable_d2_source_segment_id(
        source_sha256=source_ir.source_sha256,
        handle_kind=selection.handle_kind,
        source_handle=selection.source_handle,
        start_offset=start_offset,
        end_offset=end_offset,
    )
    values = {
        "segment_id": segment_id,
        "source_sha256": source_ir.source_sha256,
        "handle_kind": selection.handle_kind,
        "source_handle": selection.source_handle,
        "owner_source_node_id": owner.source_node_id,
        "source_order": owner.source_order,
        "page_numbers": pages,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "source_content_sha256": content_sha256,
    }
    return D2SourceSegmentV1(
        **values,
        segment_sha256=stable_d2_source_segment_sha256(**values),
    )


def _expand_candidate_source_spans(
    spans: tuple[D2CandidateSourceSpanV2, ...],
    *,
    source_ir: SourceIRSnapshot,
) -> tuple[D2CandidateSourceRangeV1, ...]:
    nodes = source_ir.nodes
    ordinals_by_handle = {
        node.source_node_id: ordinal for ordinal, node in enumerate(nodes)
    }
    expanded: list[D2CandidateSourceRangeV1] = []
    for span in spans:
        start_ordinal = ordinals_by_handle.get(span.start_source_handle)
        end_ordinal = ordinals_by_handle.get(span.end_source_handle)
        if start_ordinal is None or end_ordinal is None:
            raise ValueError("D2 candidate span references an unknown source handle")
        if end_ordinal < start_ordinal:
            raise ValueError("D2 candidate source span reverses SourceIR order")
        for ordinal in range(start_ordinal, end_ordinal + 1):
            node = nodes[ordinal]
            if node.kind is SourceNodeKind.TABLE:
                continue
            is_start = ordinal == start_ordinal
            is_end = ordinal == end_ordinal
            if node.raw_text:
                start_offset = (
                    span.start_offset
                    if is_start and span.start_offset is not None
                    else 0
                )
                end_offset = (
                    span.end_offset
                    if is_end and span.end_offset is not None
                    else len(node.raw_text)
                )
                if start_offset > len(node.raw_text) or end_offset > len(node.raw_text):
                    raise ValueError("D2 candidate span exceeds its source node")
                if end_offset <= start_offset:
                    if is_end and end_offset == 0 and ordinal > start_ordinal:
                        continue
                    if (
                        is_start
                        and start_offset == len(node.raw_text)
                        and (ordinal < end_ordinal)
                    ):
                        continue
                    raise ValueError(
                        "D2 candidate span resolves to an empty node range"
                    )
                expanded.append(
                    D2CandidateSourceRangeV1(
                        handle_kind=D2SourceHandleKind.SOURCE_NODE,
                        source_handle=node.source_node_id,
                        start_offset=start_offset,
                        end_offset=end_offset,
                    )
                )
                continue

            boundary_offsets = (
                span.start_offset if is_start else None,
                span.end_offset if is_end else None,
            )
            if any(value not in {None, 0} for value in boundary_offsets):
                raise ValueError("textless D2 source span cannot carry an offset")
            if is_end and span.end_offset == 0 and ordinal > start_ordinal:
                continue
            expanded.append(
                D2CandidateSourceRangeV1(
                    handle_kind=D2SourceHandleKind.SOURCE_NODE,
                    source_handle=node.source_node_id,
                )
            )
    return tuple(expanded)


def _descendant_source_segments(
    object_key: str,
    *,
    object_candidates: dict[str, D2LogicalObjectDecisionCandidateV2],
    segments_by_object_key: dict[str, tuple[D2SourceSegmentV1, ...]],
) -> tuple[D2SourceSegmentV1, ...]:
    child_keys = tuple(
        key
        for key, candidate in object_candidates.items()
        if candidate.parent_object_key == object_key
    )
    segments = tuple(
        segment
        for child_key in child_keys
        for segment in (
            *segments_by_object_key[child_key],
            *_descendant_source_segments(
                child_key,
                object_candidates=object_candidates,
                segments_by_object_key=segments_by_object_key,
            ),
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


def _descendant_logical_table_scopes(
    object_key: str,
    *,
    object_candidates: dict[str, D2LogicalObjectDecisionCandidateV2],
    scopes_by_key: dict[str, AgentLogicalTableScopeV1],
) -> tuple[AgentLogicalTableScopeV1, ...]:
    child_keys = tuple(
        key
        for key, candidate in object_candidates.items()
        if candidate.parent_object_key == object_key
    )
    scopes = tuple(
        scope
        for child_key in child_keys
        for scope in (
            *(
                (scopes_by_key[object_candidates[child_key].logical_table_scope_key],)
                if object_candidates[child_key].logical_table_scope_key is not None
                else ()
            ),
            *_descendant_logical_table_scopes(
                child_key,
                object_candidates=object_candidates,
                scopes_by_key=scopes_by_key,
            ),
        )
    )
    return tuple(
        sorted(
            {scope.logical_object_id: scope for scope in scopes}.values(),
            key=lambda item: (
                item.physical_parts[0].source_order,
                item.logical_object_id,
            ),
        )
    )


def _compile_logical_object(
    candidate: D2LogicalObjectDecisionCandidateV2,
    *,
    source_segments: tuple[D2SourceSegmentV1, ...],
    descendant_source_segments: tuple[D2SourceSegmentV1, ...],
    descendant_logical_table_scopes: tuple[AgentLogicalTableScopeV1, ...],
    logical_object_id: str,
    parent_logical_object_id: str | None,
    scopes_by_key: dict[str, AgentLogicalTableScopeV1],
) -> D2LogicalObjectV1:
    scope = (
        scopes_by_key[candidate.logical_table_scope_key]
        if candidate.logical_table_scope_key is not None
        else None
    )
    derives_from_descendants = not source_segments and scope is None
    effective_segments = (
        descendant_source_segments if derives_from_descendants else source_segments
    )
    effective_scopes = (
        descendant_logical_table_scopes
        if derives_from_descendants
        else (() if scope is None else (scope,))
    )
    evidence_orders = [item.source_order for item in effective_segments]
    evidence_pages = {page for item in effective_segments for page in item.page_numbers}
    for effective_scope in effective_scopes:
        evidence_orders.extend(
            item.source_order for item in effective_scope.physical_parts
        )
        evidence_pages.update(
            page
            for item in effective_scope.physical_parts
            for page in item.page_numbers
        )
    if not evidence_orders or not evidence_pages:
        raise ValueError("D2 candidate object lacks ordered page evidence")
    values = {
        "logical_object_id": logical_object_id,
        "object_kind": candidate.object_kind,
        "parent_logical_object_id": parent_logical_object_id,
        "source_order": min(evidence_orders),
        "page_numbers": tuple(sorted(evidence_pages)),
        "source_segments": source_segments,
        "logical_table_scope_ids": (
            () if scope is None else (scope.logical_object_id,)
        ),
        "reason_codes": _canonical_reason_codes(candidate.reason_codes),
    }
    return D2LogicalObjectV1(
        **values,
        object_sha256=stable_d2_logical_object_sha256(**values),
    )


def _compile_source_dispositions(
    decisions: tuple[D2UnboundSourceDispositionDecisionCandidateV1, ...],
    *,
    source_ir: SourceIRSnapshot,
    node_catalog: dict[str, SourceNode],
    logical_objects_by_key: dict[str, D2LogicalObjectV1],
    segments_by_object_key: dict[str, tuple[D2SourceSegmentV1, ...]],
) -> tuple[D2SourceEvidenceDispositionV1, ...]:
    compiled: list[D2SourceEvidenceDispositionV1] = []
    for object_key, logical_object in logical_objects_by_key.items():
        for segment in segments_by_object_key[object_key]:
            if segment.handle_kind is not D2SourceHandleKind.SOURCE_NODE:
                continue
            node = node_catalog.get(segment.owner_source_node_id)
            if node is None or node.kind is SourceNodeKind.TABLE:
                continue
            disposition_id = stable_d2_source_disposition_id(
                source_sha256=source_ir.source_sha256,
                source_node_id=node.source_node_id,
                start_offset=segment.start_offset,
                end_offset=segment.end_offset,
            )
            evidence_refs = tuple(
                sorted(
                    {
                        node.source_node_id,
                        *((node.asset_ref,) if node.asset_ref is not None else ()),
                    }
                )
            )
            values = {
                "disposition_id": disposition_id,
                "source_sha256": source_ir.source_sha256,
                "source_node_id": node.source_node_id,
                "source_order": node.source_order,
                "start_offset": segment.start_offset,
                "end_offset": segment.end_offset,
                "disposition": D2SourceEvidenceDispositionKind.LOGICAL_OBJECT_PART,
                "logical_object_id": logical_object.logical_object_id,
                "source_segment_id": segment.segment_id,
                "reason_codes": ("agent_selected_logical_object_part",),
                "evidence_refs": evidence_refs,
            }
            compiled.append(
                D2SourceEvidenceDispositionV1(
                    **values,
                    disposition_sha256=stable_d2_source_disposition_sha256(**values),
                )
            )

    for decision in decisions:
        node = node_catalog.get(decision.source_handle)
        if node is None:
            raise ValueError(
                "D2 source disposition references an unknown source handle"
            )
        if node.kind is SourceNodeKind.TABLE:
            raise ValueError("D2 source disposition must reference a non-table node")
        start_offset, end_offset = _resolve_selected_range(
            source_text=node.raw_text,
            start_offset=decision.start_offset,
            end_offset=decision.end_offset,
        )
        disposition_id = stable_d2_source_disposition_id(
            source_sha256=source_ir.source_sha256,
            source_node_id=node.source_node_id,
            start_offset=start_offset,
            end_offset=end_offset,
        )
        evidence_refs = tuple(
            sorted(
                {
                    node.source_node_id,
                    *((node.asset_ref,) if node.asset_ref is not None else ()),
                }
            )
        )
        values = {
            "disposition_id": disposition_id,
            "source_sha256": source_ir.source_sha256,
            "source_node_id": node.source_node_id,
            "source_order": node.source_order,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "disposition": decision.disposition,
            "logical_object_id": None,
            "source_segment_id": None,
            "reason_codes": _canonical_reason_codes(decision.reason_codes),
            "evidence_refs": evidence_refs,
        }
        compiled.append(
            D2SourceEvidenceDispositionV1(
                **values,
                disposition_sha256=stable_d2_source_disposition_sha256(**values),
            )
        )
    _append_unresolved_source_coverage_gaps(
        compiled,
        source_ir=source_ir,
        node_catalog=node_catalog,
    )
    return tuple(
        sorted(
            compiled,
            key=lambda item: (
                item.source_order,
                item.source_node_id,
                -1 if item.start_offset is None else item.start_offset,
                item.disposition_id,
            ),
        )
    )


def _append_unresolved_source_coverage_gaps(
    compiled: list[D2SourceEvidenceDispositionV1],
    *,
    source_ir: SourceIRSnapshot,
    node_catalog: dict[str, SourceNode],
) -> None:
    dispositions_by_node: dict[str, list[D2SourceEvidenceDispositionV1]] = {}
    for item in compiled:
        dispositions_by_node.setdefault(item.source_node_id, []).append(item)

    for node in node_catalog.values():
        if node.kind is SourceNodeKind.TABLE:
            continue
        observed = dispositions_by_node.get(node.source_node_id, [])
        if not node.raw_text:
            if not observed:
                compiled.append(
                    _unresolved_source_gap(
                        source_ir=source_ir,
                        node=node,
                        start_offset=None,
                        end_offset=None,
                    )
                )
            continue

        intervals: list[tuple[int, int]] = []
        for item in observed:
            if item.start_offset is None or item.end_offset is None:
                raise ValueError("text source disposition requires an exact range")
            if item.end_offset > len(node.raw_text):
                raise ValueError("D2 source disposition exceeds its source text")
            intervals.append((item.start_offset, item.end_offset))
        intervals.sort()

        cursor = 0
        for start_offset, end_offset in intervals:
            if start_offset < cursor:
                raise ValueError("D2 source disposition ranges cannot overlap")
            if cursor < start_offset:
                compiled.append(
                    _unresolved_source_gap(
                        source_ir=source_ir,
                        node=node,
                        start_offset=cursor,
                        end_offset=start_offset,
                    )
                )
            cursor = end_offset
        if cursor < len(node.raw_text):
            compiled.append(
                _unresolved_source_gap(
                    source_ir=source_ir,
                    node=node,
                    start_offset=cursor,
                    end_offset=len(node.raw_text),
                )
            )


def _unresolved_source_gap(
    *,
    source_ir: SourceIRSnapshot,
    node: SourceNode,
    start_offset: int | None,
    end_offset: int | None,
) -> D2SourceEvidenceDispositionV1:
    disposition_id = stable_d2_source_disposition_id(
        source_sha256=source_ir.source_sha256,
        source_node_id=node.source_node_id,
        start_offset=start_offset,
        end_offset=end_offset,
    )
    evidence_refs = tuple(
        sorted(
            {
                node.source_node_id,
                *((node.asset_ref,) if node.asset_ref is not None else ()),
            }
        )
    )
    values = {
        "disposition_id": disposition_id,
        "source_sha256": source_ir.source_sha256,
        "source_node_id": node.source_node_id,
        "source_order": node.source_order,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "disposition": D2SourceEvidenceDispositionKind.UNRESOLVED,
        "logical_object_id": None,
        "source_segment_id": None,
        "reason_codes": ("host_completed_uncovered_source",),
        "evidence_refs": evidence_refs,
    }
    return D2SourceEvidenceDispositionV1(
        **values,
        disposition_sha256=stable_d2_source_disposition_sha256(**values),
    )


def _compile_table_dispositions(
    decisions: tuple[D2PhysicalTableDispositionDecisionCandidateV1, ...],
    *,
    table_catalog: dict[str, SourceNode],
    scopes_by_key: dict[str, AgentLogicalTableScopeV1],
) -> tuple[D2PhysicalTableDispositionV1, ...]:
    decisions_by_table = {item.table_id: item for item in decisions}
    if set(decisions_by_table) != set(table_catalog):
        raise ValueError(
            "D2 candidate must disposition every current physical TableIR exactly once"
        )
    compiled: list[D2PhysicalTableDispositionV1] = []
    for table_id, node in table_catalog.items():
        decision = decisions_by_table[table_id]
        logical_table_scope_id: str | None = None
        logical_table_scope_key = getattr(
            decision,
            "logical_table_scope_key",
            None,
        )
        nested_owner_table_id = getattr(
            decision,
            "nested_owner_table_id",
            None,
        )
        if logical_table_scope_key is not None:
            scope = scopes_by_key.get(logical_table_scope_key)
            if scope is None:
                raise ValueError("D2 table disposition references an unknown scope key")
            logical_table_scope_id = scope.logical_object_id
        if (
            nested_owner_table_id is not None
            and nested_owner_table_id not in table_catalog
        ):
            raise ValueError(
                "D2 nested-value decision references an unknown owner table"
            )
        evidence_refs = tuple(
            sorted(
                {
                    node.source_node_id,
                    f"document-table-map:{table_id}",
                }
            )
        )
        values = {
            "table_id": table_id,
            "disposition": decision.disposition,
            "logical_table_scope_id": logical_table_scope_id,
            "nested_owner_table_id": nested_owner_table_id,
            "reason_codes": _canonical_reason_codes(decision.reason_codes),
            "evidence_refs": evidence_refs,
        }
        compiled.append(
            D2PhysicalTableDispositionV1(
                **values,
                disposition_sha256=stable_d2_table_disposition_sha256(**values),
            )
        )
    return tuple(compiled)


def _resolve_selected_range(
    *,
    source_text: str,
    start_offset: int | None,
    end_offset: int | None,
) -> tuple[int | None, int | None]:
    if source_text:
        if start_offset is None and end_offset is None:
            return 0, len(source_text)
        if start_offset is None or end_offset is None:
            raise ValueError("D2 source offsets must be supplied together")
        if end_offset > len(source_text):
            raise ValueError("D2 candidate source range exceeds its source text")
        return start_offset, end_offset
    elif start_offset is not None or end_offset is not None:
        raise ValueError("textless D2 source handles cannot carry a text range")
    return None, None


def _cell_catalog(
    source_ir: SourceIRSnapshot,
) -> dict[str, tuple[SourceNode, TableCellIR]]:
    catalog: dict[str, tuple[SourceNode, TableCellIR]] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        for cell in node.table_payload.cells:
            if cell.cell_id in catalog:
                raise ValueError("SourceIR contains an ambiguous table-cell handle")
            catalog[cell.cell_id] = (node, cell)
    return catalog


def _table_catalog(source_ir: SourceIRSnapshot) -> dict[str, SourceNode]:
    catalog: dict[str, SourceNode] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
            continue
        table_id = node.table_payload.table_id
        if table_id in catalog:
            raise ValueError("SourceIR contains an ambiguous physical TableIR ID")
        catalog[table_id] = node
    return catalog


def d2_source_node_content_sha256(node: SourceNode) -> str:
    """Authoritative D2 hash for one SourceNode's selectable content."""

    if node.raw_text:
        return hashlib.sha256(node.raw_text.encode("utf-8")).hexdigest()
    if node.table_payload is not None:
        return canonical_model_sha256(node.table_payload)
    if node.asset_ref is not None:
        return canonical_model_sha256(node)
    raise ValueError("D2 source node has no bindable content")


def _validate_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(not item.strip() or len(item) > 160 for item in value):
        raise ValueError("D2 candidate reason codes must be non-empty")
    return value


def _canonical_reason_codes(value: tuple[str, ...]) -> tuple[str, ...]:
    normalized = {
        re.sub(r"[^a-z0-9]+", "_", item.strip().lower()).strip("_")[:120].rstrip("_")
        for item in value
    }
    if "" in normalized:
        raise ValueError("D2 candidate reason code cannot normalize to empty")
    return tuple(sorted(normalized))


__all__ = [
    "D2_LOGICAL_STRUCTURE_DECISION_CANDIDATE_SCHEMA_VERSION",
    "D2CandidateSourceRangeV1",
    "D2CandidateSourceSpanV2",
    "D2LogicalObjectDecisionCandidateV2",
    "D2LogicalScopePartTableDispositionDecisionCandidateV1",
    "D2LogicalStructureCompilationV1",
    "D2LogicalStructureDecisionCandidateV2",
    "D2LogicalTableScopeDecisionCandidateV1",
    "D2NestedValueTableDispositionDecisionCandidateV1",
    "D2PhysicalTableDispositionDecisionCandidateV1",
    "D2StandaloneTableDispositionDecisionCandidateV1",
    "D2UnboundSourceDispositionDecisionCandidateV1",
    "compile_and_validate_d2_logical_structure_candidate",
    "d2_source_node_content_sha256",
]
