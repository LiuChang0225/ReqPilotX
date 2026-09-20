from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.domain.extraction.structure import (
    BilingualCounterpartGroup,
    BilingualMismatchSignal,
    DocumentLanguagePolicy,
    StructureFinding,
    StructureNode,
    StructureOptionState,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    VisualTableCellTextBinding,
)


STRUCTURE_PATCH_PROPOSAL_SCHEMA_VERSION = "structure-patch-proposal/v1"
STRUCTURE_PATCH_SCHEMA_VERSION = "structure-patch/v1"
STRUCTURE_REVISION_SCHEMA_VERSION = "structure-revision/v1"
EFFECTIVE_STRUCTURE_SNAPSHOT_SCHEMA_VERSION = "effective-structure-snapshot/v1"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_STRUCTURE_RELATION_ID_PATTERN = re.compile(r"^structure-relation-[0-9a-f]{32}$")
_PROPOSAL_ID_PATTERN = re.compile(r"^structure-proposal-[0-9a-f]{32}$")
_PATCH_ID_PATTERN = re.compile(r"^structure-patch-[0-9a-f]{32}$")
_REVISION_ID_PATTERN = re.compile(r"^structure-revision-[0-9a-f]{32}$")
_EFFECTIVE_SNAPSHOT_ID_PATTERN = re.compile(r"^effective-structure-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class StructureRevisionRejectionCode(str, Enum):
    INVALID_PROPOSAL = "invalid_proposal"
    INVALID_PATCH = "invalid_patch"
    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    BASE_TREE_MISMATCH = "base_tree_mismatch"
    INVALID_SOURCE_REF = "invalid_source_ref"
    INVALID_SOURCE_SPAN = "invalid_source_span"
    INVALID_OPERATION = "invalid_operation"
    GRAPH_CYCLE = "graph_cycle"
    DANGLING_ENDPOINT = "dangling_endpoint"
    STALE_BASE_REVISION = "stale_base_revision"
    REVISION_CHAIN_INVALID = "revision_chain_invalid"
    NOTHING_TO_UNDO = "nothing_to_undo"


class StructureRevisionRejected(RuntimeError):
    def __init__(
        self,
        code: StructureRevisionRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class StructureSourceSpanV1(_StrictFrozenModel):
    """A content-free Unicode half-open interval over one immutable source ref."""

    source_ref: StructureSourceRef
    start_offset: StrictInt = Field(ge=0)
    end_offset: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def validate_interval(self) -> StructureSourceSpanV1:
        if self.end_offset < self.start_offset:
            raise ValueError("structure source spans use a half-open ordered interval")
        return self


class StructureNodeSourceSpansV1(_StrictFrozenModel):
    """Ordered effective grounding for one reused Structure node identity."""

    structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    source_spans: tuple[StructureSourceSpanV1, ...] = ()

    @model_validator(mode="after")
    def validate_spans(self) -> StructureNodeSourceSpansV1:
        keys = tuple(
            (
                span.source_ref.kind.value,
                span.source_ref.source_id,
                span.start_offset,
                span.end_offset,
            )
            for span in self.source_spans
        )
        if len(keys) != len(set(keys)):
            raise ValueError("effective node source spans must not contain duplicates")
        return self


class ReparentStructureOperationV1(_StrictFrozenModel):
    operation: Literal["re_parent"] = "re_parent"
    structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    new_parent_structure_node_id: str = Field(
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern
    )
    new_sibling_index: StrictInt = Field(ge=0)

    @model_validator(mode="after")
    def reject_self_parent(self) -> ReparentStructureOperationV1:
        if self.structure_node_id == self.new_parent_structure_node_id:
            raise ValueError("a re-parent operation cannot make a node its own parent")
        return self


class LogicalReorderStructureOperationV1(_StrictFrozenModel):
    operation: Literal["logical_reorder"] = "logical_reorder"
    parent_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    ordered_child_structure_node_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("ordered_child_structure_node_ids")
    @classmethod
    def validate_child_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            _STRUCTURE_NODE_ID_PATTERN.fullmatch(node_id) is None for node_id in value
        ):
            raise ValueError("logical reorder children must use Structure node IDs")
        if len(value) != len(set(value)):
            raise ValueError("logical reorder children must be unique")
        return value


class BoundarySplitPartV1(_StrictFrozenModel):
    source_spans: tuple[StructureSourceSpanV1, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_non_empty_intervals(self) -> BoundarySplitPartV1:
        if any(span.end_offset <= span.start_offset for span in self.source_spans):
            raise ValueError("boundary split parts require non-empty source intervals")
        return self


class BoundarySplitStructureOperationV1(_StrictFrozenModel):
    operation: Literal["boundary_split"] = "boundary_split"
    structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    parts: tuple[BoundarySplitPartV1, ...] = Field(min_length=2, max_length=64)


class BoundaryResegmentStructureOperationV1(_StrictFrozenModel):
    """Move one exact boundary between two adjacent, source-bound leaf nodes."""

    operation: Literal["boundary_resegment"] = "boundary_resegment"
    preceding_structure_node_id: str = Field(
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern
    )
    following_structure_node_id: str = Field(
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern
    )
    new_boundary_offset: StrictInt = Field(ge=1)

    @model_validator(mode="after")
    def require_distinct_nodes(self) -> BoundaryResegmentStructureOperationV1:
        if self.preceding_structure_node_id == self.following_structure_node_id:
            raise ValueError("boundary resegment requires two distinct nodes")
        return self


class ContinuationMergeStructureOperationV1(_StrictFrozenModel):
    operation: Literal["continuation_merge"] = "continuation_merge"
    primary_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    continuation_structure_node_id: str = Field(
        pattern=_STRUCTURE_NODE_ID_PATTERN.pattern
    )

    @model_validator(mode="after")
    def require_distinct_nodes(self) -> ContinuationMergeStructureOperationV1:
        if self.primary_structure_node_id == self.continuation_structure_node_id:
            raise ValueError("continuation merge requires two distinct nodes")
        return self


class AddStructureRelationOperationV1(_StrictFrozenModel):
    operation: Literal["add_relation"] = "add_relation"
    kind: StructureRelationKind
    source_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    target_structure_node_id: str = Field(pattern=_STRUCTURE_NODE_ID_PATTERN.pattern)
    source_refs: tuple[StructureSourceRef, ...] = ()
    option_state: StructureOptionState | None = None

    @model_validator(mode="after")
    def validate_relation_shape(self) -> AddStructureRelationOperationV1:
        if self.kind is StructureRelationKind.CONTAINS:
            raise ValueError("contains changes must use the re-parent operation")
        if self.source_structure_node_id == self.target_structure_node_id:
            raise ValueError("a structure relation cannot reference itself")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("relation source refs must not contain duplicates")
        if self.kind is StructureRelationKind.OPTION_STATE:
            if self.option_state is None:
                raise ValueError("option_state relations require an explicit state")
        elif self.option_state is not None:
            raise ValueError("option_state is valid only for option_state relations")
        return self


class RemoveStructureRelationOperationV1(_StrictFrozenModel):
    operation: Literal["remove_relation"] = "remove_relation"
    relation_id: str = Field(pattern=_STRUCTURE_RELATION_ID_PATTERN.pattern)


StructurePatchOperationV1 = Annotated[
    ReparentStructureOperationV1
    | LogicalReorderStructureOperationV1
    | BoundarySplitStructureOperationV1
    | BoundaryResegmentStructureOperationV1
    | ContinuationMergeStructureOperationV1
    | AddStructureRelationOperationV1
    | RemoveStructureRelationOperationV1,
    Field(discriminator="operation"),
]


class StructurePatchProposalV1(_StrictFrozenModel):
    """Untrusted, source-bound intent. This type is deliberately not applicable."""

    schema_version: Literal["structure-patch-proposal/v1"] = (
        STRUCTURE_PATCH_PROPOSAL_SCHEMA_VERSION
    )
    proposal_id: str = Field(pattern=_PROPOSAL_ID_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    source_ir_canonical_sha256: StrictStr
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    base_structure_canonical_sha256: StrictStr
    expected_revision_number: StrictInt = Field(ge=0)
    expected_effective_snapshot_id: str = Field(
        pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern
    )
    operations: tuple[StructurePatchOperationV1, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @field_validator(
        "source_sha256",
        "source_ir_canonical_sha256",
        "base_structure_canonical_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        _require_sha256(value, "proposal digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> StructurePatchProposalV1:
        operation_payloads = tuple(_canonical_payload(item) for item in self.operations)
        if len(operation_payloads) != len(set(operation_payloads)):
            raise ValueError("a proposal must not contain duplicate operations")
        expected = stable_structure_patch_proposal_id(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            base_structure_canonical_sha256=self.base_structure_canonical_sha256,
            expected_revision_number=self.expected_revision_number,
            expected_effective_snapshot_id=self.expected_effective_snapshot_id,
            operations=self.operations,
        )
        if self.proposal_id != expected:
            raise ValueError("proposal_id must bind the complete proposal")
        return self


class StructurePatchV1(_StrictFrozenModel):
    """Server-validated patch accepted from exactly one immutable proposal."""

    schema_version: Literal["structure-patch/v1"] = STRUCTURE_PATCH_SCHEMA_VERSION
    patch_id: str = Field(pattern=_PATCH_ID_PATTERN.pattern)
    accepted_proposal_id: str = Field(pattern=_PROPOSAL_ID_PATTERN.pattern)
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    source_ir_canonical_sha256: StrictStr
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    base_structure_canonical_sha256: StrictStr
    expected_revision_number: StrictInt = Field(ge=0)
    expected_effective_snapshot_id: str = Field(
        pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern
    )
    operations: tuple[StructurePatchOperationV1, ...] = Field(
        min_length=1,
        max_length=64,
    )

    @field_validator(
        "source_sha256",
        "source_ir_canonical_sha256",
        "base_structure_canonical_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        _require_sha256(value, "patch digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> StructurePatchV1:
        expected = stable_structure_patch_id(
            accepted_proposal_id=self.accepted_proposal_id,
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            base_structure_canonical_sha256=self.base_structure_canonical_sha256,
            expected_revision_number=self.expected_revision_number,
            expected_effective_snapshot_id=self.expected_effective_snapshot_id,
            operations=self.operations,
        )
        if self.patch_id != expected:
            raise ValueError("patch_id must bind the accepted proposal and patch")
        return self


class StructureImpactV1(_StrictFrozenModel):
    direct_structure_node_ids: tuple[str, ...] = ()
    affected_structure_node_ids: tuple[str, ...] = ()
    affected_section_node_ids: tuple[str, ...] = ()
    changed_relation_ids: tuple[str, ...] = ()

    @field_validator(
        "direct_structure_node_ids",
        "affected_structure_node_ids",
        "affected_section_node_ids",
    )
    @classmethod
    def validate_node_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            _STRUCTURE_NODE_ID_PATTERN.fullmatch(node_id) is None for node_id in value
        ):
            raise ValueError("impact nodes must use Structure node IDs")
        if value != tuple(sorted(set(value))):
            raise ValueError("impact node IDs must be unique and sorted")
        return value

    @field_validator("changed_relation_ids")
    @classmethod
    def validate_relation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            _STRUCTURE_RELATION_ID_PATTERN.fullmatch(relation_id) is None
            for relation_id in value
        ):
            raise ValueError("impact relations must use Structure relation IDs")
        if value != tuple(sorted(set(value))):
            raise ValueError("impact relation IDs must be unique and sorted")
        return value


class EffectiveStructureSnapshotV1(_StrictFrozenModel):
    """One revision-bound effective projection over an immutable base tree."""

    schema_version: Literal["effective-structure-snapshot/v1"] = (
        EFFECTIVE_STRUCTURE_SNAPSHOT_SCHEMA_VERSION
    )
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    effective_tree_sha256: StrictStr
    revision_chain_sha256: StrictStr
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    source_ir_canonical_sha256: StrictStr
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    base_structure_canonical_sha256: StrictStr
    resolver_id: str = Field(min_length=1, max_length=160)
    resolver_version: str = Field(min_length=1, max_length=160)
    revision_number: StrictInt = Field(ge=0)
    head_revision_id: str | None = Field(
        default=None,
        pattern=_REVISION_ID_PATTERN.pattern,
    )
    source_node_ids: tuple[str, ...] = ()
    source_cell_ids: tuple[str, ...] = ()
    visual_text_observation_ids: tuple[str, ...] = ()
    visual_table_cell_ids: tuple[str, ...] = ()
    nodes: tuple[StructureNode, ...]
    relations: tuple[StructureRelation, ...] = ()
    node_source_spans: tuple[StructureNodeSourceSpansV1, ...]
    document_language_policy: DocumentLanguagePolicy | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    bilingual_counterpart_groups: tuple[BilingualCounterpartGroup, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    bilingual_mismatch_signals: tuple[BilingualMismatchSignal, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    visual_table_cell_text_bindings: tuple[VisualTableCellTextBinding, ...] = ()
    findings: tuple[StructureFinding, ...] = ()

    @field_validator(
        "effective_tree_sha256",
        "revision_chain_sha256",
        "source_sha256",
        "source_ir_canonical_sha256",
        "base_structure_canonical_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        _require_sha256(value, "effective Structure digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> EffectiveStructureSnapshotV1:
        if (self.revision_number == 0) != (self.head_revision_id is None):
            raise ValueError(
                "only the base effective snapshot may omit head_revision_id"
            )
        node_ids = tuple(node.structure_node_id for node in self.nodes)
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError("effective Structure nodes must be non-empty and unique")
        span_node_ids = tuple(
            binding.structure_node_id for binding in self.node_source_spans
        )
        if span_node_ids != node_ids:
            raise ValueError(
                "effective source-span bindings must follow every node exactly once"
            )
        relation_ids = tuple(relation.relation_id for relation in self.relations)
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("effective Structure relations must be unique")

        expected_tree_sha256 = stable_effective_structure_tree_sha256(
            source_ir_id=self.source_ir_id,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            resolver_id=self.resolver_id,
            resolver_version=self.resolver_version,
            source_node_ids=self.source_node_ids,
            source_cell_ids=self.source_cell_ids,
            visual_text_observation_ids=self.visual_text_observation_ids,
            visual_table_cell_ids=self.visual_table_cell_ids,
            nodes=self.nodes,
            relations=self.relations,
            node_source_spans=self.node_source_spans,
            document_language_policy=self.document_language_policy,
            bilingual_counterpart_groups=self.bilingual_counterpart_groups,
            bilingual_mismatch_signals=self.bilingual_mismatch_signals,
            visual_table_cell_text_bindings=self.visual_table_cell_text_bindings,
            findings=self.findings,
        )
        if self.effective_tree_sha256 != expected_tree_sha256:
            raise ValueError(
                "effective_tree_sha256 must bind the complete effective tree"
            )
        expected_snapshot_id = stable_effective_structure_snapshot_id(
            source_ir_id=self.source_ir_id,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            revision_number=self.revision_number,
            head_revision_id=self.head_revision_id,
            revision_chain_sha256=self.revision_chain_sha256,
            effective_tree_sha256=self.effective_tree_sha256,
        )
        if self.effective_snapshot_id != expected_snapshot_id:
            raise ValueError(
                "effective_snapshot_id must bind revision lineage and tree content"
            )
        return self


class StructureRevisionAction(str, Enum):
    APPLY = "APPLY"
    UNDO = "UNDO"


class StructureRevisionV1(_StrictFrozenModel):
    """Append-only lineage record. Undo is another record, never deletion."""

    schema_version: Literal["structure-revision/v1"] = STRUCTURE_REVISION_SCHEMA_VERSION
    revision_id: str = Field(pattern=_REVISION_ID_PATTERN.pattern)
    revision_number: StrictInt = Field(ge=1)
    parent_revision_id: str | None = Field(
        default=None,
        pattern=_REVISION_ID_PATTERN.pattern,
    )
    action: StructureRevisionAction
    patch: StructurePatchV1 | None = None
    undo_target_revision_id: str | None = Field(
        default=None,
        pattern=_REVISION_ID_PATTERN.pattern,
    )
    source_ir_id: str = Field(min_length=1, max_length=160)
    source_sha256: StrictStr
    source_ir_canonical_sha256: StrictStr
    base_structure_snapshot_id: str = Field(min_length=1, max_length=160)
    base_structure_canonical_sha256: StrictStr
    previous_effective_snapshot_id: str = Field(
        pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern
    )
    previous_effective_tree_sha256: StrictStr
    previous_revision_chain_sha256: StrictStr
    effective_snapshot_id: str = Field(pattern=_EFFECTIVE_SNAPSHOT_ID_PATTERN.pattern)
    effective_tree_sha256: StrictStr
    revision_chain_sha256: StrictStr
    impact: StructureImpactV1

    @field_validator(
        "source_sha256",
        "source_ir_canonical_sha256",
        "base_structure_canonical_sha256",
        "previous_effective_tree_sha256",
        "previous_revision_chain_sha256",
        "effective_tree_sha256",
        "revision_chain_sha256",
    )
    @classmethod
    def validate_digest(cls, value: str) -> str:
        _require_sha256(value, "revision digest")
        return value

    @model_validator(mode="after")
    def validate_identity(self) -> StructureRevisionV1:
        if (self.revision_number == 1) != (self.parent_revision_id is None):
            raise ValueError("only revision one may omit parent_revision_id")
        if self.action is StructureRevisionAction.APPLY:
            if self.patch is None or self.undo_target_revision_id is not None:
                raise ValueError("APPLY revisions require only a patch")
            if (
                self.patch.source_ir_id != self.source_ir_id
                or self.patch.source_sha256 != self.source_sha256
                or self.patch.source_ir_canonical_sha256
                != self.source_ir_canonical_sha256
                or self.patch.base_structure_snapshot_id
                != self.base_structure_snapshot_id
                or self.patch.base_structure_canonical_sha256
                != self.base_structure_canonical_sha256
                or self.patch.expected_revision_number != self.revision_number - 1
                or self.patch.expected_effective_snapshot_id
                != self.previous_effective_snapshot_id
            ):
                raise ValueError("revision patch binding must match its lineage")
        elif self.patch is not None or self.undo_target_revision_id is None:
            raise ValueError("UNDO revisions require only an undo target")

        expected_revision_id = stable_structure_revision_id(
            revision_number=self.revision_number,
            parent_revision_id=self.parent_revision_id,
            action=self.action,
            patch=self.patch,
            undo_target_revision_id=self.undo_target_revision_id,
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            base_structure_canonical_sha256=self.base_structure_canonical_sha256,
            previous_effective_snapshot_id=self.previous_effective_snapshot_id,
            previous_effective_tree_sha256=self.previous_effective_tree_sha256,
            previous_revision_chain_sha256=self.previous_revision_chain_sha256,
            effective_tree_sha256=self.effective_tree_sha256,
            impact=self.impact,
        )
        if self.revision_id != expected_revision_id:
            raise ValueError(
                "revision_id must bind the complete append-only transition"
            )
        expected_chain_sha256 = stable_revision_chain_sha256(
            previous_revision_chain_sha256=self.previous_revision_chain_sha256,
            revision_id=self.revision_id,
        )
        if self.revision_chain_sha256 != expected_chain_sha256:
            raise ValueError("revision_chain_sha256 must extend the previous chain")
        expected_snapshot_id = stable_effective_structure_snapshot_id(
            source_ir_id=self.source_ir_id,
            base_structure_snapshot_id=self.base_structure_snapshot_id,
            revision_number=self.revision_number,
            head_revision_id=self.revision_id,
            revision_chain_sha256=self.revision_chain_sha256,
            effective_tree_sha256=self.effective_tree_sha256,
        )
        if self.effective_snapshot_id != expected_snapshot_id:
            raise ValueError("revision effective snapshot identity is inconsistent")
        return self


class StructureApplyResultV1(_StrictFrozenModel):
    appended: bool
    revision: StructureRevisionV1
    effective_snapshot: EffectiveStructureSnapshotV1
    impact: StructureImpactV1


def canonical_model_sha256(value: BaseModel) -> str:
    return hashlib.sha256(_canonical_payload(value).encode("utf-8")).hexdigest()


def stable_structure_patch_proposal_id(
    *,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    base_structure_snapshot_id: str,
    base_structure_canonical_sha256: str,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
    operations: tuple[StructurePatchOperationV1, ...],
) -> str:
    return _stable_id(
        "structure-proposal",
        {
            "schema_version": STRUCTURE_PATCH_PROPOSAL_SCHEMA_VERSION,
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "base_structure_snapshot_id": base_structure_snapshot_id,
            "base_structure_canonical_sha256": base_structure_canonical_sha256,
            "expected_revision_number": expected_revision_number,
            "expected_effective_snapshot_id": expected_effective_snapshot_id,
            "operations": [_json_value(item) for item in operations],
        },
    )


def stable_structure_patch_id(
    *,
    accepted_proposal_id: str,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    base_structure_snapshot_id: str,
    base_structure_canonical_sha256: str,
    expected_revision_number: int,
    expected_effective_snapshot_id: str,
    operations: tuple[StructurePatchOperationV1, ...],
) -> str:
    return _stable_id(
        "structure-patch",
        {
            "schema_version": STRUCTURE_PATCH_SCHEMA_VERSION,
            "accepted_proposal_id": accepted_proposal_id,
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "base_structure_snapshot_id": base_structure_snapshot_id,
            "base_structure_canonical_sha256": base_structure_canonical_sha256,
            "expected_revision_number": expected_revision_number,
            "expected_effective_snapshot_id": expected_effective_snapshot_id,
            "operations": [_json_value(item) for item in operations],
        },
    )


def stable_effective_structure_tree_sha256(
    *,
    source_ir_id: str,
    base_structure_snapshot_id: str,
    resolver_id: str,
    resolver_version: str,
    source_node_ids: tuple[str, ...],
    source_cell_ids: tuple[str, ...],
    visual_text_observation_ids: tuple[str, ...],
    visual_table_cell_ids: tuple[str, ...],
    nodes: tuple[StructureNode, ...],
    relations: tuple[StructureRelation, ...],
    node_source_spans: tuple[StructureNodeSourceSpansV1, ...],
    document_language_policy: DocumentLanguagePolicy | None,
    bilingual_counterpart_groups: tuple[BilingualCounterpartGroup, ...],
    bilingual_mismatch_signals: tuple[BilingualMismatchSignal, ...],
    visual_table_cell_text_bindings: tuple[VisualTableCellTextBinding, ...],
    findings: tuple[StructureFinding, ...],
) -> str:
    payload = {
        "source_ir_id": source_ir_id,
        "base_structure_snapshot_id": base_structure_snapshot_id,
        "resolver_id": resolver_id,
        "resolver_version": resolver_version,
        "source_node_ids": source_node_ids,
        "source_cell_ids": source_cell_ids,
        "visual_text_observation_ids": visual_text_observation_ids,
        "visual_table_cell_ids": visual_table_cell_ids,
        "nodes": [_json_value(node) for node in nodes],
        "relations": [_json_value(relation) for relation in relations],
        "node_source_spans": [_json_value(item) for item in node_source_spans],
        "document_language_policy": _json_value(document_language_policy),
        "bilingual_counterpart_groups": [
            _json_value(item) for item in bilingual_counterpart_groups
        ],
        "bilingual_mismatch_signals": [
            _json_value(item) for item in bilingual_mismatch_signals
        ],
        "visual_table_cell_text_bindings": [
            _json_value(item) for item in visual_table_cell_text_bindings
        ],
        "findings": [_json_value(item) for item in findings],
    }
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def stable_base_revision_chain_sha256(
    *,
    source_ir_id: str,
    source_ir_canonical_sha256: str,
    base_structure_snapshot_id: str,
    base_structure_canonical_sha256: str,
) -> str:
    return hashlib.sha256(
        _canonical_payload(
            {
                "schema_version": STRUCTURE_REVISION_SCHEMA_VERSION,
                "source_ir_id": source_ir_id,
                "source_ir_canonical_sha256": source_ir_canonical_sha256,
                "base_structure_snapshot_id": base_structure_snapshot_id,
                "base_structure_canonical_sha256": base_structure_canonical_sha256,
                "revisions": [],
            }
        ).encode("utf-8")
    ).hexdigest()


def stable_revision_chain_sha256(
    *,
    previous_revision_chain_sha256: str,
    revision_id: str,
) -> str:
    _require_sha256(previous_revision_chain_sha256, "previous revision-chain digest")
    if _REVISION_ID_PATTERN.fullmatch(revision_id) is None:
        raise ValueError("revision_id must use the Structure revision identity")
    return hashlib.sha256(
        _canonical_payload(
            {
                "previous_revision_chain_sha256": previous_revision_chain_sha256,
                "revision_id": revision_id,
            }
        ).encode("utf-8")
    ).hexdigest()


def stable_effective_structure_snapshot_id(
    *,
    source_ir_id: str,
    base_structure_snapshot_id: str,
    revision_number: int,
    head_revision_id: str | None,
    revision_chain_sha256: str,
    effective_tree_sha256: str,
) -> str:
    return _stable_id(
        "effective-structure",
        {
            "source_ir_id": source_ir_id,
            "base_structure_snapshot_id": base_structure_snapshot_id,
            "revision_number": revision_number,
            "head_revision_id": head_revision_id,
            "revision_chain_sha256": revision_chain_sha256,
            "effective_tree_sha256": effective_tree_sha256,
        },
    )


def stable_structure_revision_id(
    *,
    revision_number: int,
    parent_revision_id: str | None,
    action: StructureRevisionAction,
    patch: StructurePatchV1 | None,
    undo_target_revision_id: str | None,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    base_structure_snapshot_id: str,
    base_structure_canonical_sha256: str,
    previous_effective_snapshot_id: str,
    previous_effective_tree_sha256: str,
    previous_revision_chain_sha256: str,
    effective_tree_sha256: str,
    impact: StructureImpactV1,
) -> str:
    return _stable_id(
        "structure-revision",
        {
            "schema_version": STRUCTURE_REVISION_SCHEMA_VERSION,
            "revision_number": revision_number,
            "parent_revision_id": parent_revision_id,
            "action": action.value,
            "patch_id": None if patch is None else patch.patch_id,
            "undo_target_revision_id": undo_target_revision_id,
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "base_structure_snapshot_id": base_structure_snapshot_id,
            "base_structure_canonical_sha256": base_structure_canonical_sha256,
            "previous_effective_snapshot_id": previous_effective_snapshot_id,
            "previous_effective_tree_sha256": previous_effective_tree_sha256,
            "previous_revision_chain_sha256": previous_revision_chain_sha256,
            "effective_tree_sha256": effective_tree_sha256,
            "impact": _json_value(impact),
        },
    )


def _stable_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(_canonical_payload(value).encode("utf-8")).hexdigest()
    return f"{prefix}-{digest[:32]}"


def _canonical_payload(value: object) -> str:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _require_sha256(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")


__all__ = [
    "EFFECTIVE_STRUCTURE_SNAPSHOT_SCHEMA_VERSION",
    "STRUCTURE_PATCH_PROPOSAL_SCHEMA_VERSION",
    "STRUCTURE_PATCH_SCHEMA_VERSION",
    "STRUCTURE_REVISION_SCHEMA_VERSION",
    "AddStructureRelationOperationV1",
    "BoundaryResegmentStructureOperationV1",
    "BoundarySplitPartV1",
    "BoundarySplitStructureOperationV1",
    "ContinuationMergeStructureOperationV1",
    "EffectiveStructureSnapshotV1",
    "LogicalReorderStructureOperationV1",
    "RemoveStructureRelationOperationV1",
    "ReparentStructureOperationV1",
    "StructureApplyResultV1",
    "StructureImpactV1",
    "StructureNodeSourceSpansV1",
    "StructurePatchOperationV1",
    "StructurePatchProposalV1",
    "StructurePatchV1",
    "StructureRevisionAction",
    "StructureRevisionRejected",
    "StructureRevisionRejectionCode",
    "StructureRevisionV1",
    "StructureSourceSpanV1",
    "canonical_model_sha256",
    "stable_base_revision_chain_sha256",
    "stable_effective_structure_snapshot_id",
    "stable_effective_structure_tree_sha256",
    "stable_revision_chain_sha256",
    "stable_structure_patch_id",
    "stable_structure_patch_proposal_id",
    "stable_structure_revision_id",
]
