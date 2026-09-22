from __future__ import annotations

import hashlib
import json
import re
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx.domain.extraction.semantics import RequirementSubtype, SemanticClass
from reqpilotx.domain.extraction.structure import StructureSourceRef, StructureSourceRefKind


REQUIREMENT_VIEW_SCHEMA_VERSION = "requirement-view/v3"
_REQUIREMENT_VIEW_ID_PATTERN = re.compile(r"^requirement-view-[0-9a-f]{32}$")
_REQUIREMENT_VIEW_NODE_ID_PATTERN = re.compile(r"^requirement-view-node-[0-9a-f]{32}$")
_SOURCE_IR_ID_PATTERN = re.compile(r"^sir-[0-9a-f]{32}$")
_STRUCTURE_SNAPSHOT_ID_PATTERN = re.compile(
    r"^(?:structure|effective-structure)-[0-9a-f]{32}$"
)
_STRUCTURE_NODE_ID_PATTERN = re.compile(r"^structure-node-[0-9a-f]{32}$")
_STRUCTURE_RELATION_ID_PATTERN = re.compile(r"^structure-relation-[0-9a-f]{32}$")
_SEMANTIC_SNAPSHOT_ID_PATTERN = re.compile(r"^semantic-[0-9a-f]{32}$")
_SEMANTIC_ANNOTATION_ID_PATTERN = re.compile(r"^semantic-annotation-[0-9a-f]{32}$")
_SEMANTIC_SPAN_ID_PATTERN = re.compile(r"^semantic-span-[0-9a-f]{32}$")
_SOURCE_NODE_ID_PATTERN = re.compile(r"^node-[0-9a-f]{32}$")
_SOURCE_CELL_ID_PATTERN = re.compile(r"^cell-[0-9a-f]{32}$")
_VISUAL_TEXT_OBSERVATION_ID_PATTERN = re.compile(r"^vto-[0-9a-f]{32}$")


class _StrictFrozenModel(BaseModel):
    """Immutable, text-free value object for a requirement projection."""

    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class RequirementProjectionRole(str, Enum):
    PRIMARY = "PRIMARY"
    CHILD = "CHILD"
    CONTENT = "CONTENT"
    PARAMETER = "PARAMETER"
    REFERENCE = "REFERENCE"


class RequirementDecisionMode(str, Enum):
    DIRECT = "DIRECT"
    ROLLUP = "ROLLUP"
    NONE = "NONE"


class RequirementProjectionPolicy(str, Enum):
    """Selects review, formal, or trusted non-requirement projection behavior."""

    SHADOW_CANDIDATES = "SHADOW_CANDIDATES"
    NON_REQUIREMENT_ARTIFACT = "NON_REQUIREMENT_ARTIFACT"
    FORMAL_CONFIRMED = "FORMAL_CONFIRMED"


class RequirementViewNode(_StrictFrozenModel):
    """One source-grounded item in a reviewable requirement projection.

    Customer text is deliberately absent. ``source_span_ids`` identify the
    exact semantic grounding while ``full_context_source_refs`` retain the
    complete parent block for review and downstream traceability.
    """

    requirement_view_node_id: str = Field(min_length=1, max_length=160)
    role: RequirementProjectionRole
    parent_requirement_view_node_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=160,
    )
    decision_mode: RequirementDecisionMode
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None = None
    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    source_semantic_unit_ids: tuple[str, ...]
    owner_structure_node_id: str = Field(pattern=r"^structure-node-[0-9a-f]{32}$")
    semantic_annotation_ids: tuple[str, ...]
    ancestor_structure_node_ids: tuple[str, ...]
    source_span_ids: tuple[str, ...]
    source_refs: tuple[StructureSourceRef, ...]
    full_context_source_refs: tuple[StructureSourceRef, ...]
    relation_ids: tuple[str, ...] = ()
    source_order: int = Field(ge=0)
    unit_order: int = Field(ge=0)

    @field_validator("requirement_view_node_id")
    @classmethod
    def validate_view_node_id(cls, value: str) -> str:
        if not _REQUIREMENT_VIEW_NODE_ID_PATTERN.fullmatch(value):
            raise ValueError("requirement_view_node_id must use a stable identity")
        return value

    @field_validator("semantic_annotation_ids")
    @classmethod
    def validate_semantic_annotation_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not _SEMANTIC_ANNOTATION_ID_PATTERN.fullmatch(item) for item in value
        ):
            raise ValueError("semantic annotations must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("semantic annotations must be unique")
        return value

    @field_validator("source_semantic_unit_ids")
    @classmethod
    def validate_source_semantic_unit_ids(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not value or any(
            not re.fullmatch(r"semantic-unit-[0-9a-f]{32}", item) for item in value
        ):
            raise ValueError("source semantic units must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("source semantic units must be unique")
        return value

    @field_validator("source_span_ids")
    @classmethod
    def validate_source_span_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(
            not _SEMANTIC_SPAN_ID_PATTERN.fullmatch(item) for item in value
        ):
            raise ValueError("source spans must use stable semantic identities")
        if len(value) != len(set(value)):
            raise ValueError("source spans must be unique")
        return value

    @field_validator("ancestor_structure_node_ids")
    @classmethod
    def validate_ancestor_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STRUCTURE_NODE_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("ancestor structure IDs must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("ancestor structure IDs must be unique")
        return value

    @field_validator("relation_ids")
    @classmethod
    def validate_relation_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _STRUCTURE_RELATION_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("relation IDs must use stable identities")
        if len(value) != len(set(value)):
            raise ValueError("relation IDs must be unique")
        return value

    @model_validator(mode="after")
    def validate_projection_shape(self) -> RequirementViewNode:
        if self.source_semantic_unit_ids[0] != self.semantic_unit_id:
            raise ValueError("primary semantic unit must lead source unit order")
        if self.owner_structure_node_id in self.ancestor_structure_node_ids:
            raise ValueError("a target structure node cannot be its own ancestor")
        if not self.source_refs:
            raise ValueError("every RequirementView node requires exact source refs")
        if not self.full_context_source_refs:
            raise ValueError(
                "every RequirementView node requires full-context source refs"
            )
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("source_refs must not contain duplicates")
        if len(self.full_context_source_refs) != len(
            set(self.full_context_source_refs)
        ):
            raise ValueError("full_context_source_refs must not contain duplicates")
        if not set(self.source_refs).issubset(self.full_context_source_refs):
            raise ValueError("exact source refs must be contained by full context")

        if self.semantic_class is SemanticClass.REQUIREMENT:
            if self.role is RequirementProjectionRole.CONTENT:
                if self.decision_mode is not RequirementDecisionMode.NONE:
                    raise ValueError(
                        "requirement content projections must use decision_mode NONE"
                    )
            elif self.role in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.PARAMETER,
            }:
                if self.decision_mode is RequirementDecisionMode.NONE:
                    raise ValueError(
                        "requirement decision targets must be DIRECT or ROLLUP"
                    )
            else:
                raise ValueError(
                    "requirement semantics require a decision target or CONTENT role"
                )
        else:
            if self.requirement_subtype is not None:
                raise ValueError("non-requirement projections cannot have a subtype")
            if self.role not in {
                RequirementProjectionRole.CONTENT,
                RequirementProjectionRole.REFERENCE,
            }:
                raise ValueError("non-requirement semantics require a context role")
            if self.decision_mode is not RequirementDecisionMode.NONE:
                raise ValueError("context projections must use decision_mode NONE")

        if self.role is RequirementProjectionRole.PRIMARY:
            if self.parent_requirement_view_node_id is not None:
                raise ValueError("PRIMARY projections cannot have a view parent")
        elif self.parent_requirement_view_node_id is None:
            raise ValueError("non-PRIMARY projections require a view parent")
        return self

    @property
    def target_structure_node_id(self) -> str:
        return self.owner_structure_node_id


class RequirementViewSnapshot(_StrictFrozenModel):
    """Validated, immutable projection over one Structure/Semantic pair."""

    schema_version: Literal["requirement-view/v3"] = REQUIREMENT_VIEW_SCHEMA_VERSION
    requirement_view_snapshot_id: str = Field(min_length=1, max_length=160)
    source_ir_id: str = Field(min_length=1, max_length=160)
    structure_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_snapshot_id: str = Field(min_length=1, max_length=160)
    semantic_unit_plan_id: str = Field(pattern=r"^semantic-unit-plan-[0-9a-f]{32}$")
    projector_id: str = Field(min_length=1, max_length=160)
    projector_version: str = Field(min_length=1, max_length=160)
    projection_policy: RequirementProjectionPolicy
    requirement_set_id: str | None = Field(default=None, min_length=1, max_length=160)
    structure_node_ids: tuple[str, ...]
    structure_relation_ids: tuple[str, ...]
    semantic_unit_ids: tuple[str, ...]
    semantic_annotation_ids: tuple[str, ...]
    source_node_ids: tuple[str, ...]
    source_cell_ids: tuple[str, ...]
    visual_text_observation_ids: tuple[str, ...]
    nodes: tuple[RequirementViewNode, ...]

    @field_validator(
        "source_ir_id",
        "projector_id",
        "projector_version",
    )
    @classmethod
    def reject_blank_snapshot_value(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("snapshot identity values must not be blank")
        return value

    @field_validator("source_ir_id")
    @classmethod
    def validate_source_ir_id(cls, value: str) -> str:
        if not _SOURCE_IR_ID_PATTERN.fullmatch(value):
            raise ValueError("source_ir_id must use a stable SourceIR identity")
        return value

    @field_validator("structure_snapshot_id")
    @classmethod
    def validate_structure_snapshot_id(cls, value: str) -> str:
        if not _STRUCTURE_SNAPSHOT_ID_PATTERN.fullmatch(value):
            raise ValueError("structure_snapshot_id must use a stable identity")
        return value

    @field_validator("semantic_snapshot_id")
    @classmethod
    def validate_semantic_snapshot_id(cls, value: str) -> str:
        if not _SEMANTIC_SNAPSHOT_ID_PATTERN.fullmatch(value):
            raise ValueError("semantic_snapshot_id must use a stable identity")
        return value

    @field_validator("requirement_view_snapshot_id")
    @classmethod
    def validate_snapshot_id(cls, value: str) -> str:
        if not _REQUIREMENT_VIEW_ID_PATTERN.fullmatch(value):
            raise ValueError("requirement_view_snapshot_id must use a stable identity")
        return value

    @field_validator("structure_node_ids")
    @classmethod
    def validate_structure_node_catalog(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_STRUCTURE_NODE_ID_PATTERN,
            field_name="structure node catalog",
        )

    @field_validator("structure_relation_ids")
    @classmethod
    def validate_structure_relation_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_STRUCTURE_RELATION_ID_PATTERN,
            field_name="structure relation catalog",
        )

    @field_validator("semantic_annotation_ids")
    @classmethod
    def validate_semantic_annotation_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_SEMANTIC_ANNOTATION_ID_PATTERN,
            field_name="semantic annotation catalog",
        )

    @field_validator("semantic_unit_ids")
    @classmethod
    def validate_semantic_unit_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=re.compile(r"^semantic-unit-[0-9a-f]{32}$"),
            field_name="semantic unit catalog",
        )

    @field_validator("source_node_ids")
    @classmethod
    def validate_source_node_catalog(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_SOURCE_NODE_ID_PATTERN,
            field_name="source node catalog",
        )

    @field_validator("source_cell_ids")
    @classmethod
    def validate_source_cell_catalog(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_SOURCE_CELL_ID_PATTERN,
            field_name="source cell catalog",
        )

    @field_validator("visual_text_observation_ids")
    @classmethod
    def validate_visual_text_observation_catalog(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _validate_sorted_catalog(
            value,
            pattern=_VISUAL_TEXT_OBSERVATION_ID_PATTERN,
            field_name="visual text observation catalog",
        )

    @model_validator(mode="after")
    def validate_snapshot_graph(self) -> RequirementViewSnapshot:
        expected_snapshot_id = stable_requirement_view_snapshot_id(
            structure_snapshot_id=self.structure_snapshot_id,
            semantic_snapshot_id=self.semantic_snapshot_id,
            semantic_unit_plan_id=self.semantic_unit_plan_id,
            projector_id=self.projector_id,
            projector_version=self.projector_version,
            projection_policy=self.projection_policy,
            requirement_set_id=self.requirement_set_id,
        )
        if self.requirement_view_snapshot_id != expected_snapshot_id:
            raise ValueError("requirement view snapshot ID does not match its identity")

        if self.projection_policy in {
            RequirementProjectionPolicy.SHADOW_CANDIDATES,
            RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
        }:
            if self.requirement_set_id is not None:
                raise ValueError("non-formal projections cannot bind a Requirement Set")
        elif self.requirement_set_id is None:
            raise ValueError("formal projections require a target Requirement Set")

        structure_node_ids = set(self.structure_node_ids)
        structure_relation_ids = set(self.structure_relation_ids)
        semantic_unit_ids = set(self.semantic_unit_ids)
        semantic_annotation_ids = set(self.semantic_annotation_ids)
        source_node_ids = set(self.source_node_ids)
        source_cell_ids = set(self.source_cell_ids)
        visual_text_observation_ids = set(self.visual_text_observation_ids)

        node_ids: set[str] = set()
        semantic_units: set[str] = set()
        used_source_semantic_units: set[str] = set()
        used_annotations: set[str] = set()
        orders: list[tuple[int, int]] = []
        unit_orders: set[int] = set()
        nodes_by_id: dict[str, RequirementViewNode] = {}
        node_order_by_id: dict[str, int] = {}
        for node_index, node in enumerate(self.nodes):
            if node.requirement_view_node_id in node_ids:
                raise ValueError("requirement view node IDs must be unique")
            node_ids.add(node.requirement_view_node_id)
            nodes_by_id[node.requirement_view_node_id] = node
            node_order_by_id[node.requirement_view_node_id] = node_index
            if node.semantic_unit_id in semantic_units:
                raise ValueError("one semantic unit can produce at most one view node")
            semantic_units.add(node.semantic_unit_id)
            if used_source_semantic_units.intersection(node.source_semantic_unit_ids):
                raise ValueError("a semantic unit cannot feed multiple view nodes")
            used_source_semantic_units.update(node.source_semantic_unit_ids)
            if used_annotations.intersection(node.semantic_annotation_ids):
                raise ValueError(
                    "a semantic annotation cannot feed multiple view nodes"
                )
            used_annotations.update(node.semantic_annotation_ids)
            if node.unit_order in unit_orders:
                raise ValueError("view nodes require unique semantic unit_order values")
            unit_orders.add(node.unit_order)
            orders.append((node.source_order, node.unit_order))

            expected_node_id = stable_requirement_view_node_id(
                requirement_view_snapshot_id=self.requirement_view_snapshot_id,
                semantic_unit_id=node.semantic_unit_id,
                source_semantic_unit_ids=node.source_semantic_unit_ids,
                role=node.role,
                semantic_annotation_ids=node.semantic_annotation_ids,
            )
            if node.requirement_view_node_id != expected_node_id:
                raise ValueError("requirement view node ID does not match its identity")
            if node.semantic_unit_id not in semantic_unit_ids:
                raise ValueError("view unit must exist in the semantic unit catalog")
            if any(
                unit_id not in semantic_unit_ids
                for unit_id in node.source_semantic_unit_ids
            ):
                raise ValueError(
                    "view source units must exist in the semantic unit catalog"
                )
            if node.owner_structure_node_id not in structure_node_ids:
                raise ValueError("view target must exist in the StructureTree catalog")
            if any(
                ancestor_id not in structure_node_ids
                for ancestor_id in node.ancestor_structure_node_ids
            ):
                raise ValueError(
                    "view ancestors must exist in the StructureTree catalog"
                )
            if any(
                annotation_id not in semantic_annotation_ids
                for annotation_id in node.semantic_annotation_ids
            ):
                raise ValueError("view annotations must exist in the semantic catalog")
            if any(
                relation_id not in structure_relation_ids
                for relation_id in node.relation_ids
            ):
                raise ValueError(
                    "view relations must exist in the StructureTree catalog"
                )
            for source_ref in (*node.source_refs, *node.full_context_source_refs):
                if source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
                    if source_ref.source_id not in source_node_ids:
                        raise ValueError(
                            "view source ref is absent from the source catalog"
                        )
                elif source_ref.kind is StructureSourceRefKind.TABLE_CELL:
                    if source_ref.source_id not in source_cell_ids:
                        raise ValueError(
                            "view cell ref is absent from the source catalog"
                        )
                elif source_ref.source_id not in visual_text_observation_ids:
                    raise ValueError(
                        "view visual text ref is absent from the source catalog"
                    )

        if orders != sorted(orders):
            raise ValueError(
                "view nodes require ascending source_order and semantic unit_order"
            )

        children_by_parent: dict[str, list[RequirementViewNode]] = {}
        for node in self.nodes:
            parent_id = node.parent_requirement_view_node_id
            if parent_id is None:
                continue
            parent = nodes_by_id.get(parent_id)
            if parent is None:
                raise ValueError("view parent must reference a node in the snapshot")
            if (
                parent.source_order > node.source_order
                or parent.unit_order >= node.unit_order
                or node_order_by_id[parent_id]
                >= node_order_by_id[node.requirement_view_node_id]
            ):
                raise ValueError("a view parent must precede its children")
            if parent.role not in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.PARAMETER,
            }:
                raise ValueError("view children require a requirement parent")
            parent_is_structural_ancestor = (
                parent.target_structure_node_id in node.ancestor_structure_node_ids
            )
            shares_semantic_owner = (
                parent.owner_structure_node_id == node.owner_structure_node_id
                and parent.semantic_unit_id != node.semantic_unit_id
            )
            shares_reference_container = (
                node.role is RequirementProjectionRole.REFERENCE
                and bool(
                    set(parent.ancestor_structure_node_ids).intersection(
                        node.ancestor_structure_node_ids
                    )
                )
            )
            if (
                not parent_is_structural_ancestor
                and not shares_semantic_owner
                and not shares_reference_container
            ):
                raise ValueError(
                    "view parent must be a preceding unit from the same owner, "
                    "a structural ancestor, or share a container with reference context"
                )
            children_by_parent.setdefault(parent_id, []).append(node)

        for node in self.nodes:
            if node.role not in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.PARAMETER,
            }:
                continue
            has_determinable_child = any(
                child.role
                in {
                    RequirementProjectionRole.CHILD,
                    RequirementProjectionRole.PARAMETER,
                }
                for child in children_by_parent.get(node.requirement_view_node_id, ())
            )
            expected_mode = (
                RequirementDecisionMode.ROLLUP
                if has_determinable_child
                else RequirementDecisionMode.DIRECT
            )
            if node.decision_mode is not expected_mode:
                raise ValueError(
                    "requirement decision mode must reflect determinable child projections"
                )
        return self


def stable_requirement_view_snapshot_id(
    *,
    structure_snapshot_id: str,
    semantic_snapshot_id: str,
    semantic_unit_plan_id: str,
    projector_id: str,
    projector_version: str,
    projection_policy: RequirementProjectionPolicy,
    requirement_set_id: str | None,
) -> str:
    _require_non_blank(structure_snapshot_id, "structure_snapshot_id")
    _require_non_blank(semantic_snapshot_id, "semantic_snapshot_id")
    _require_non_blank(semantic_unit_plan_id, "semantic_unit_plan_id")
    _require_non_blank(projector_id, "projector_id")
    _require_non_blank(projector_version, "projector_version")
    if not isinstance(projection_policy, RequirementProjectionPolicy):
        raise TypeError("projection_policy must be a RequirementProjectionPolicy")
    if requirement_set_id is not None:
        _require_non_blank(requirement_set_id, "requirement_set_id")
    return _stable_id(
        "requirement-view",
        structure_snapshot_id,
        semantic_snapshot_id,
        semantic_unit_plan_id,
        projector_id,
        projector_version,
        projection_policy.value,
        requirement_set_id,
    )


def stable_requirement_view_node_id(
    *,
    requirement_view_snapshot_id: str,
    semantic_unit_id: str,
    source_semantic_unit_ids: tuple[str, ...],
    role: RequirementProjectionRole,
    semantic_annotation_ids: tuple[str, ...],
) -> str:
    _require_non_blank(requirement_view_snapshot_id, "requirement_view_snapshot_id")
    _require_non_blank(semantic_unit_id, "semantic_unit_id")
    if (
        not source_semantic_unit_ids
        or source_semantic_unit_ids[0] != semantic_unit_id
        or len(source_semantic_unit_ids) != len(set(source_semantic_unit_ids))
    ):
        raise ValueError(
            "source_semantic_unit_ids must uniquely begin with semantic_unit_id"
        )
    if not isinstance(role, RequirementProjectionRole):
        raise TypeError("role must be a RequirementProjectionRole")
    if not semantic_annotation_ids or any(
        not item.strip() for item in semantic_annotation_ids
    ):
        raise ValueError("semantic_annotation_ids must be non-empty")
    return _stable_id(
        "requirement-view-node",
        requirement_view_snapshot_id,
        semantic_unit_id,
        source_semantic_unit_ids,
        role.value,
        semantic_annotation_ids,
    )


def _validate_sorted_catalog(
    value: tuple[str, ...],
    *,
    pattern: re.Pattern[str] | None,
    field_name: str,
) -> tuple[str, ...]:
    if any(not item.strip() for item in value):
        raise ValueError(f"{field_name} cannot contain blank values")
    if pattern is not None and any(not pattern.fullmatch(item) for item in value):
        raise ValueError(f"{field_name} values must use stable identities")
    if len(value) != len(set(value)):
        raise ValueError(f"{field_name} must contain unique values")
    if value != tuple(sorted(value)):
        raise ValueError(f"{field_name} must be sorted")
    return value


def _stable_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _require_non_blank(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must not be blank")


__all__ = [
    "REQUIREMENT_VIEW_SCHEMA_VERSION",
    "RequirementDecisionMode",
    "RequirementProjectionPolicy",
    "RequirementProjectionRole",
    "RequirementViewNode",
    "RequirementViewSnapshot",
    "stable_requirement_view_node_id",
    "stable_requirement_view_snapshot_id",
]
