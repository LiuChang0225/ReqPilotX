from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import TypeVar

from pydantic import ValidationError

from app.application.extraction.assemble_requirements import (
    RequirementAssemblyRejected,
    validate_requirement_assembly_against_inputs,
)
from app.application.extraction.classify_content import (
    SemanticClassificationRejected,
    SemanticTextResolver,
    validate_semantic_classification_against_inputs,
)
from app.application.extraction.structure_snapshot import (
    require_structure_snapshot,
    structure_snapshot_id,
)
from app.domain.extraction.requirement_view import (
    RequirementDecisionMode,
    RequirementProjectionPolicy,
    RequirementProjectionRole,
    RequirementViewNode,
    RequirementViewSnapshot,
    stable_requirement_view_node_id,
    stable_requirement_view_snapshot_id,
)
from app.domain.extraction.requirement_assembly import (
    RequirementAssemblySnapshot,
    RequirementAssemblyStatus,
)
from app.domain.extraction.semantics import (
    RequirementSubtype,
    SemanticAnnotation,
    SemanticAnnotationStatus,
    SemanticClass,
    SemanticClassificationSnapshot,
)
from app.domain.extraction.source_ir import SourceIRSnapshot, SourceNodeKind
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureOptionState,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)


DEFAULT_REQUIREMENT_PROJECTOR_ID = "deterministic-requirement-projector"
DEFAULT_REQUIREMENT_PROJECTOR_VERSION = "1.7.2"
ASSEMBLY_REQUIREMENT_PROJECTOR_ID = "validated-model-assembly-projector"
ASSEMBLY_REQUIREMENT_PROJECTOR_VERSION = "1.2.0"

_NUMBERED_CONTENT_CONTAINER_REASONS = {
    "numbered_requirement_container_signal",
    "numbered_test_method_container_signal",
}
_ABSORBING_SECTION_REASONS = {
    *_NUMBERED_CONTENT_CONTAINER_REASONS,
    "normative_applicability_container_signal",
    "regulatory_certification_option_container_signal",
    "test_equipment_signal",
    "test_objective_signal",
}


class RequirementProjectionRejectionCode(str, Enum):
    INPUT_SNAPSHOT_MISMATCH = "input_snapshot_mismatch"
    BLOCKING_STRUCTURE_FINDING = "blocking_structure_finding"
    SHADOW_REQUIREMENT_SET_FORBIDDEN = "shadow_requirement_set_forbidden"
    NON_REQUIREMENT_ARTIFACT_SET_FORBIDDEN = "non_requirement_artifact_set_forbidden"
    FORMAL_REQUIREMENT_SET_REQUIRED = "formal_requirement_set_required"
    FORMAL_SEMANTICS_UNCONFIRMED = "formal_semantics_unconfirmed"
    CONFLICTING_CONTINUATION_SEMANTICS = "conflicting_continuation_semantics"
    INVALID_PROJECTION = "invalid_projection"
    UNSUPPORTED_PROJECTOR = "unsupported_projector"
    TAMPERED_VIEW = "tampered_view"
    ASSEMBLY_MISMATCH = "assembly_mismatch"


class RequirementProjectionRejected(RuntimeError):
    def __init__(
        self,
        code: RequirementProjectionRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _ProjectionUnit:
    target_structure_node_id: str
    component_structure_node_ids: tuple[str, ...]
    annotations: tuple[SemanticAnnotation, ...]
    semantic_class: SemanticClass
    requirement_subtype: RequirementSubtype | None
    source_span_ids: tuple[str, ...]
    source_refs: tuple[StructureSourceRef, ...]
    source_order: int


def _primary_semantic_unit_id(unit: _ProjectionUnit) -> str:
    for annotation in unit.annotations:
        if annotation.owner_structure_node_id == unit.target_structure_node_id:
            return annotation.semantic_unit_id
    raise RequirementProjectionRejected(
        RequirementProjectionRejectionCode.INVALID_PROJECTION,
        "A projection owner has no primary semantic unit.",
    )


def _source_semantic_unit_ids(
    unit: _ProjectionUnit,
    *,
    primary_semantic_unit_id: str,
) -> tuple[str, ...]:
    unit_ids = tuple(
        dict.fromkeys(
            (
                primary_semantic_unit_id,
                *(
                    annotation.semantic_unit_id
                    for annotation in unit.annotations
                    if annotation.semantic_unit_id != primary_semantic_unit_id
                ),
            )
        )
    )
    if primary_semantic_unit_id not in {
        annotation.semantic_unit_id for annotation in unit.annotations
    }:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
            "A projection primary is absent from its semantic-unit lineage.",
        )
    return unit_ids


def project_requirements(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    projection_policy: RequirementProjectionPolicy = (
        RequirementProjectionPolicy.SHADOW_CANDIDATES
    ),
    requirement_set_id: str | None = None,
    projector_id: str = DEFAULT_REQUIREMENT_PROJECTOR_ID,
    projector_version: str = DEFAULT_REQUIREMENT_PROJECTOR_VERSION,
    text_resolver: SemanticTextResolver | None = None,
) -> RequirementViewSnapshot:
    """Project a Structure/Semantic pair without mutating formal serving state.

    ``SHADOW_CANDIDATES`` keeps rule/model candidates reviewable and can never
    bind a Requirement Set. ``NON_REQUIREMENT_ARTIFACT`` is selected only from
    trusted ingress context and preserves the upstream facts while producing
    an empty RequirementView. ``FORMAL_CONFIRMED`` requires every semantic
    item in scope to be confirmed and an explicit target Requirement Set
    identity. This use case only builds a value object; persistence/activation
    remains a separate, future workflow boundary.
    """

    _validate_projection_inputs(
        source_ir,
        tree,
        semantics,
        projection_policy=projection_policy,
        requirement_set_id=requirement_set_id,
        projector_id=projector_id,
        projector_version=projector_version,
        text_resolver=text_resolver,
    )
    try:
        view = _build_requirement_view(
            source_ir,
            tree,
            semantics,
            projection_policy=projection_policy,
            requirement_set_id=requirement_set_id,
            projector_id=projector_id,
            projector_version=projector_version,
        )
    except RequirementProjectionRejected:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INVALID_PROJECTION,
            "The requirement projection violated its strict grounding contract.",
        ) from exc
    validate_requirement_view_against_inputs(
        view,
        source_ir,
        tree,
        semantics,
        text_resolver=text_resolver,
    )
    return view


def validate_requirement_view_against_inputs(
    view: RequirementViewSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    text_resolver: SemanticTextResolver | None = None,
) -> None:
    """Fail closed unless a portable view exactly matches its source snapshots."""

    if not isinstance(view, RequirementViewSnapshot):
        raise TypeError("view must be a RequirementViewSnapshot")
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(semantics, SemanticClassificationSnapshot):
        raise TypeError("semantics must be a SemanticClassificationSnapshot")
    if (
        view.projector_id != DEFAULT_REQUIREMENT_PROJECTOR_ID
        or view.projector_version != DEFAULT_REQUIREMENT_PROJECTOR_VERSION
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.UNSUPPORTED_PROJECTOR,
            "The RequirementView projector identity is not supported.",
        )

    _validate_projection_inputs(
        source_ir,
        tree,
        semantics,
        projection_policy=view.projection_policy,
        requirement_set_id=view.requirement_set_id,
        projector_id=view.projector_id,
        projector_version=view.projector_version,
        text_resolver=text_resolver,
    )
    try:
        expected = _build_requirement_view(
            source_ir,
            tree,
            semantics,
            projection_policy=view.projection_policy,
            requirement_set_id=view.requirement_set_id,
            projector_id=view.projector_id,
            projector_version=view.projector_version,
        )
    except RequirementProjectionRejected:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INVALID_PROJECTION,
            "The source snapshots cannot produce a valid RequirementView.",
        ) from exc
    if view != expected:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.TAMPERED_VIEW,
            "RequirementView content does not match its Structure/Semantic sources.",
        )


def project_requirements_from_assembly(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    assembly: RequirementAssemblySnapshot,
) -> RequirementViewSnapshot:
    """Build a review-only view from a locally validated model assembly."""

    _validate_assembled_projection_inputs(source_ir, tree, semantics, assembly)
    try:
        view = _build_assembled_requirement_view(tree, semantics, assembly)
    except RequirementProjectionRejected:
        raise
    except (TypeError, ValueError, ValidationError) as exc:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INVALID_PROJECTION,
            "The model assembly could not produce a valid RequirementView.",
        ) from exc
    validate_assembled_requirement_view_against_inputs(
        view,
        source_ir,
        tree,
        semantics,
        assembly,
    )
    return view


def validate_assembled_requirement_view_against_inputs(
    view: RequirementViewSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    assembly: RequirementAssemblySnapshot,
) -> None:
    if not isinstance(view, RequirementViewSnapshot):
        raise TypeError("view must be a RequirementViewSnapshot")
    _validate_assembled_projection_inputs(source_ir, tree, semantics, assembly)
    if (
        view.projector_id != ASSEMBLY_REQUIREMENT_PROJECTOR_ID
        or view.projector_version != ASSEMBLY_REQUIREMENT_PROJECTOR_VERSION
        or view.projection_policy is not RequirementProjectionPolicy.SHADOW_CANDIDATES
        or view.requirement_set_id is not None
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.UNSUPPORTED_PROJECTOR,
            "The assembled RequirementView is not a review-only model projection.",
        )
    expected = _build_assembled_requirement_view(tree, semantics, assembly)
    if view != expected:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.TAMPERED_VIEW,
            "Assembled RequirementView content does not match its validated inputs.",
        )


def _validate_assembled_projection_inputs(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    assembly: RequirementAssemblySnapshot,
) -> None:
    try:
        validate_semantic_classification_against_inputs(semantics, source_ir, tree)
        validate_requirement_assembly_against_inputs(
            assembly,
            source_ir,
            tree,
            semantics,
        )
    except (SemanticClassificationRejected, RequirementAssemblyRejected) as exc:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
            "The model assembly is not bound to the supplied extraction snapshots.",
        ) from exc


def _build_assembled_requirement_view(
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    assembly: RequirementAssemblySnapshot,
) -> RequirementViewSnapshot:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    component_for, component_members = _continuation_components(tree)
    units = _projection_units(
        semantics.annotations,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
    )
    expected_scope = tuple(item.target_structure_node_id for item in units)
    if tuple(item.owner_structure_node_id for item in assembly.items) != expected_scope:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
            "Requirement assembly must exactly cover the canonical semantic units.",
        )
    units_by_target = {item.target_structure_node_id: item for item in units}
    included_items = tuple(
        item
        for item in assembly.items
        if item.status is RequirementAssemblyStatus.CANDIDATE and item.include_in_view
    )
    if any(
        item.target_structure_node_id not in units_by_target for item in included_items
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
            "Requirement assembly targets are not canonical semantic units.",
        )
    item_by_target = {item.target_structure_node_id: item for item in included_items}
    all_items_by_target = {
        item.target_structure_node_id: item for item in assembly.items
    }
    continuation_units_by_target = (
        _model_continuation_units_by_projection_target(
            units_by_target=units_by_target,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
            assembly=assembly,
        )
        if included_items
        else {}
    )
    determinable_children_by_target: dict[str, set[str]] = defaultdict(set)
    for item in included_items:
        if item.parent_structure_node_id is not None and item.role in {
            RequirementProjectionRole.CHILD,
            RequirementProjectionRole.PARAMETER,
        }:
            determinable_children_by_target[item.parent_structure_node_id].add(
                item.target_structure_node_id
            )

    residual_units_by_target: dict[str, _ProjectionUnit] = {}
    residual_parent_by_target: dict[str, str] = {}
    absorbed_continuations_by_target: dict[
        str,
        tuple[_ProjectionUnit, ...],
    ] = {}
    for owner_target, continuations in continuation_units_by_target.items():
        if not determinable_children_by_target.get(owner_target):
            absorbed_continuations_by_target[owner_target] = continuations
            continue
        for group in _consecutive_projection_unit_groups(
            continuations,
            unit_order_by_target={
                target_id: item.unit_order
                for target_id, item in all_items_by_target.items()
            },
        ):
            residual = _projection_unit_with_model_continuations(
                group[0],
                group[1:],
            )
            residual_target = residual.target_structure_node_id
            if (
                residual_target in item_by_target
                or residual_target in residual_units_by_target
            ):
                raise RequirementProjectionRejected(
                    RequirementProjectionRejectionCode.INVALID_PROJECTION,
                    "A rollup residual has conflicting projection ownership.",
                )
            residual_units_by_target[residual_target] = residual
            residual_parent_by_target[residual_target] = owner_target

    projected_units_by_target = {
        target_id: _projection_unit_with_model_continuations(
            unit,
            absorbed_continuations_by_target.get(target_id, ()),
        )
        for target_id, unit in units_by_target.items()
        if target_id in item_by_target
    }
    projected_units_by_target.update(residual_units_by_target)
    role_by_target = {
        target_id: item.role
        for target_id, item in item_by_target.items()
        if item.role is not None
    }
    role_by_target.update(
        {
            target_id: RequirementProjectionRole.CHILD
            for target_id in residual_units_by_target
        }
    )
    parent_by_target = {
        target_id: item.parent_structure_node_id
        for target_id, item in item_by_target.items()
    }
    parent_by_target.update(residual_parent_by_target)
    primary_semantic_unit_id_by_target = {
        target_id: item.semantic_unit_id for target_id, item in item_by_target.items()
    }
    primary_semantic_unit_id_by_target.update(
        {
            target_id: _primary_semantic_unit_id(unit)
            for target_id, unit in residual_units_by_target.items()
        }
    )
    unit_order_by_target = {
        target_id: item.unit_order for target_id, item in item_by_target.items()
    }
    unit_order_by_target.update(
        {
            target_id: all_items_by_target[target_id].unit_order
            for target_id in residual_units_by_target
        }
    )
    projection_targets = tuple(
        sorted(
            projected_units_by_target,
            key=lambda target_id: (
                unit_order_by_target[target_id],
                target_id,
            ),
        )
    )
    snapshot_id = stable_requirement_view_snapshot_id(
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        projector_id=ASSEMBLY_REQUIREMENT_PROJECTOR_ID,
        projector_version=ASSEMBLY_REQUIREMENT_PROJECTOR_VERSION,
        projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
        requirement_set_id=None,
    )
    view_id_by_target = {
        target_id: stable_requirement_view_node_id(
            requirement_view_snapshot_id=snapshot_id,
            semantic_unit_id=primary_semantic_unit_id_by_target[target_id],
            source_semantic_unit_ids=_source_semantic_unit_ids(
                projected_units_by_target[target_id],
                primary_semantic_unit_id=primary_semantic_unit_id_by_target[target_id],
            ),
            role=role_by_target[target_id],
            semantic_annotation_ids=tuple(
                annotation.semantic_annotation_id
                for annotation in projected_units_by_target[target_id].annotations
            ),
        )
        for target_id in projection_targets
    }
    children_by_target: dict[str, set[str]] = defaultdict(set)
    for target_id, parent_target in parent_by_target.items():
        if parent_target is not None:
            children_by_target[parent_target].add(target_id)

    def top_primary(target_id: str) -> str:
        current = target_id
        visited: set[str] = set()
        while True:
            parent_target = parent_by_target[current]
            if parent_target is None:
                return current
            if current in visited:
                raise RequirementProjectionRejected(
                    RequirementProjectionRejectionCode.INVALID_PROJECTION,
                    "Requirement assembly contains a parent cycle.",
                )
            visited.add(current)
            current = parent_target

    additional_context_by_primary: dict[str, set[str]] = defaultdict(set)
    for target_id in projection_targets:
        additional_context_by_primary[top_primary(target_id)].add(target_id)
    full_context_by_primary = {
        primary: _full_context_refs(
            primary,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
            additional_context_ids=additional_context_by_primary[primary],
        )
        for primary in {top_primary(target_id) for target_id in projection_targets}
    }
    projected_nodes: list[RequirementViewNode] = []
    for target_id in projection_targets:
        role = role_by_target[target_id]
        parent_target = parent_by_target[target_id]
        unit = projected_units_by_target[target_id]
        determinable_children = {
            target
            for target in children_by_target.get(target_id, set())
            if role_by_target[target]
            in {RequirementProjectionRole.CHILD, RequirementProjectionRole.PARAMETER}
        }
        decision_mode = (
            RequirementDecisionMode.NONE
            if role
            in {RequirementProjectionRole.CONTENT, RequirementProjectionRole.REFERENCE}
            else RequirementDecisionMode.ROLLUP
            if determinable_children
            else RequirementDecisionMode.DIRECT
        )
        projected_nodes.append(
            RequirementViewNode(
                requirement_view_node_id=view_id_by_target[target_id],
                role=role,
                parent_requirement_view_node_id=(
                    view_id_by_target[parent_target]
                    if parent_target is not None
                    else None
                ),
                decision_mode=decision_mode,
                semantic_class=unit.semantic_class,
                requirement_subtype=unit.requirement_subtype,
                semantic_unit_id=primary_semantic_unit_id_by_target[target_id],
                source_semantic_unit_ids=_source_semantic_unit_ids(
                    unit,
                    primary_semantic_unit_id=primary_semantic_unit_id_by_target[
                        target_id
                    ],
                ),
                owner_structure_node_id=target_id,
                semantic_annotation_ids=tuple(
                    annotation.semantic_annotation_id for annotation in unit.annotations
                ),
                ancestor_structure_node_ids=_ancestor_structure_node_ids(
                    target_id,
                    nodes_by_id=nodes_by_id,
                    component_for=component_for,
                ),
                source_span_ids=unit.source_span_ids,
                source_refs=unit.source_refs,
                full_context_source_refs=full_context_by_primary[
                    top_primary(target_id)
                ],
                relation_ids=_relevant_relation_ids_for_targets(
                    unit.component_structure_node_ids,
                    tree=tree,
                    nodes_by_id=nodes_by_id,
                    component_for=component_for,
                    component_members=component_members,
                ),
                source_order=unit.source_order,
                unit_order=unit_order_by_target[target_id],
            )
        )
    return RequirementViewSnapshot(
        requirement_view_snapshot_id=snapshot_id,
        source_ir_id=tree.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        projector_id=ASSEMBLY_REQUIREMENT_PROJECTOR_ID,
        projector_version=ASSEMBLY_REQUIREMENT_PROJECTOR_VERSION,
        projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
        requirement_set_id=None,
        structure_node_ids=tuple(sorted(nodes_by_id)),
        structure_relation_ids=tuple(
            sorted(item.relation_id for item in tree.relations)
        ),
        semantic_unit_ids=tuple(sorted(semantics.scope_semantic_unit_ids)),
        semantic_annotation_ids=tuple(
            sorted(item.semantic_annotation_id for item in semantics.annotations)
        ),
        source_node_ids=tree.source_node_ids,
        source_cell_ids=tree.source_cell_ids,
        visual_text_observation_ids=tree.visual_text_observation_ids,
        nodes=tuple(projected_nodes),
    )


def _validate_projection_inputs(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    projection_policy: RequirementProjectionPolicy,
    requirement_set_id: str | None,
    projector_id: str,
    projector_version: str,
    text_resolver: SemanticTextResolver | None,
) -> None:
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(semantics, SemanticClassificationSnapshot):
        raise TypeError("semantics must be a SemanticClassificationSnapshot")
    if not isinstance(projection_policy, RequirementProjectionPolicy):
        raise TypeError("projection_policy must be a RequirementProjectionPolicy")
    if not projector_id.strip() or not projector_version.strip():
        raise ValueError("projector identity values must not be blank")
    if (
        projector_id != DEFAULT_REQUIREMENT_PROJECTOR_ID
        or projector_version != DEFAULT_REQUIREMENT_PROJECTOR_VERSION
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.UNSUPPORTED_PROJECTOR,
            "The RequirementView projector identity is not supported.",
        )
    if (
        semantics.source_ir_id != tree.source_ir_id
        or semantics.structure_snapshot_id != structure_snapshot_id(tree)
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
            "SemanticClassification does not belong to the supplied StructureTree.",
        )
    try:
        validate_semantic_classification_against_inputs(
            semantics,
            source_ir,
            tree,
            text_resolver=text_resolver,
        )
    except SemanticClassificationRejected as exc:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
            "Semantic grounding does not match the supplied SourceIR and StructureTree.",
        ) from exc
    structure_nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    if any(
        target_id not in structure_nodes_by_id
        for target_id in semantics.scope_structure_node_ids
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
            "Semantic scope contains a target outside the supplied StructureTree.",
        )
    if semantics.scope_structure_node_ids != tuple(
        sorted(
            semantics.scope_structure_node_ids,
            key=lambda target_id: structure_nodes_by_id[target_id].source_order,
        )
    ):
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
            "Semantic scope is not in StructureTree source order.",
        )
    source_node_ids = set(tree.source_node_ids)
    source_cell_ids = set(tree.source_cell_ids)
    visual_text_observation_ids = set(tree.visual_text_observation_ids)
    for annotation in semantics.annotations:
        for span in annotation.grounding_spans:
            if span.source_ref.kind is StructureSourceRefKind.SOURCE_NODE:
                valid_source = span.source_ref.source_id in source_node_ids
            elif span.source_ref.kind is StructureSourceRefKind.TABLE_CELL:
                valid_source = span.source_ref.source_id in source_cell_ids
            else:
                valid_source = span.source_ref.source_id in visual_text_observation_ids
            if not valid_source:
                raise RequirementProjectionRejected(
                    RequirementProjectionRejectionCode.INPUT_SNAPSHOT_MISMATCH,
                    "Semantic grounding is absent from the StructureTree source catalog.",
                )
    if projection_policy is RequirementProjectionPolicy.SHADOW_CANDIDATES:
        if requirement_set_id is not None:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.SHADOW_REQUIREMENT_SET_FORBIDDEN,
                "A shadow candidate projection cannot target a Requirement Set.",
            )
    elif projection_policy is RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT:
        if requirement_set_id is not None:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.NON_REQUIREMENT_ARTIFACT_SET_FORBIDDEN,
                "A non-requirement artifact cannot target a Requirement Set.",
            )
    else:
        if requirement_set_id is None or not requirement_set_id.strip():
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.FORMAL_REQUIREMENT_SET_REQUIRED,
                "A formal projection requires a target Requirement Set identity.",
            )
        if any(
            annotation.status is not SemanticAnnotationStatus.CONFIRMED
            for annotation in semantics.annotations
        ):
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.FORMAL_SEMANTICS_UNCONFIRMED,
                "Formal projection requires all semantic annotations to be confirmed.",
            )
        if any(finding.blocking for finding in tree.findings):
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.BLOCKING_STRUCTURE_FINDING,
                "Formal projection cannot consume a blocking structure finding.",
            )


def _build_requirement_view(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantics: SemanticClassificationSnapshot,
    *,
    projection_policy: RequirementProjectionPolicy,
    requirement_set_id: str | None,
    projector_id: str,
    projector_version: str,
) -> RequirementViewSnapshot:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    component_for, component_members = _continuation_components(tree)
    active_annotations = (
        ()
        if projection_policy is RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT
        else tuple(
            annotation
            for annotation in semantics.annotations
            if _annotation_is_active(annotation, projection_policy)
        )
    )
    units = _projection_units(
        active_annotations,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
    )
    units = _coalesce_bilingual_projection_units(
        units,
        tree=tree,
        nodes_by_id=nodes_by_id,
    )
    units_by_target = {unit.target_structure_node_id: unit for unit in units}
    option_state_by_target = {
        relation.target_structure_node_id: relation.option_state
        for relation in tree.relations
        if relation.kind is StructureRelationKind.OPTION_STATE
    }
    ineligible_option_targets = {
        target_id
        for target_id, state in option_state_by_target.items()
        if state is not StructureOptionState.CHECKED
    }
    semantic_requirement_targets = {
        unit.target_structure_node_id
        for unit in units
        if unit.semantic_class is SemanticClass.REQUIREMENT
    }
    explicit_clause_section_targets = {
        target_id
        for target_id in semantic_requirement_targets
        if (
            (node := nodes_by_id[target_id]).kind
            is StructureNodeKind.SECTION
            and node.explicit_label is not None
        )
    }
    mixed_content_section_targets = _mixed_content_section_targets(
        explicit_clause_section_targets,
        source_ir=source_ir,
        nodes_by_id=nodes_by_id,
    )
    absorbed_units_by_section = _absorbed_direct_content_units_by_parent(
        explicit_clause_section_targets - mixed_content_section_targets,
        units_by_target=units_by_target,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
        absorbable_classes={
            SemanticClass.REQUIREMENT,
            SemanticClass.UNRESOLVED,
        },
    )
    absorbed_unit_targets = {
        unit.target_structure_node_id
        for absorbed_units in absorbed_units_by_section.values()
        for unit in absorbed_units
    }
    for target_id, absorbed_units in absorbed_units_by_section.items():
        units_by_target[target_id] = _projection_unit_with_absorbed_content(
            units_by_target[target_id],
            absorbed_units,
        )
    for target_id in absorbed_unit_targets:
        units_by_target.pop(target_id, None)
    units = tuple(
        sorted(
            units_by_target.values(),
            key=lambda item: (
                item.source_order,
                item.target_structure_node_id,
            ),
        )
    )
    semantic_requirement_targets = {
        unit.target_structure_node_id
        for unit in units
        if unit.semantic_class is SemanticClass.REQUIREMENT
    }
    numbered_container_targets = {
        target_id
        for target_id in semantic_requirement_targets
        if nodes_by_id[target_id].kind is StructureNodeKind.SECTION
        and _unit_reason_codes(units_by_target[target_id]).intersection(
            _NUMBERED_CONTENT_CONTAINER_REASONS
        )
    }
    absorbing_section_targets = {
        target_id
        for target_id in semantic_requirement_targets
        if nodes_by_id[target_id].kind is StructureNodeKind.SECTION
        and _unit_reason_codes(units_by_target[target_id]).intersection(
            _ABSORBING_SECTION_REASONS
        )
    }
    absorbed_by_section = _absorbed_direct_content_targets(
        absorbing_section_targets - set(absorbed_units_by_section),
        units_by_target=units_by_target,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
        first_only=True,
    )
    numbered_content_clause_targets = {
        unit.target_structure_node_id
        for unit in units
        if _is_direct_numbered_clause_of_container(
            unit.target_structure_node_id,
            container_targets=numbered_container_targets,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
        )
    }
    numbered_container_context_targets = {
        unit.target_structure_node_id
        for unit in units
        if unit.target_structure_node_id not in numbered_container_targets
        and unit.target_structure_node_id not in numbered_content_clause_targets
        and _has_structural_ancestor_in(
            unit.target_structure_node_id,
            ancestor_targets=numbered_container_targets,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
        )
    }
    reference_row_parent, suppressed_reference_context_targets = (
        _reference_row_context_parents(
            units_by_target=units_by_target,
            nodes_by_id=nodes_by_id,
            semantic_requirement_targets=semantic_requirement_targets,
        )
    )
    ordinary_table_row_targets = {
        unit.target_structure_node_id
        for unit in units
        if nodes_by_id[unit.target_structure_node_id].kind
        is StructureNodeKind.TABLE_ROW
        and unit.semantic_class is not SemanticClass.REQUIREMENT
        and unit.target_structure_node_id not in reference_row_parent
    }
    ordinary_merged_table_support_targets = {
        unit.target_structure_node_id
        for unit in units
        if (
            (node := nodes_by_id[unit.target_structure_node_id]).kind
            is StructureNodeKind.CONTENT
            and node.boundary_basis is StructureBoundaryBasis.MERGED_RANGE
            and node.parent_structure_node_id is not None
            and nodes_by_id[node.parent_structure_node_id].kind
            is StructureNodeKind.TABLE
            and unit.semantic_class is not SemanticClass.REQUIREMENT
        )
    }
    provisional_requirement_targets = (
        semantic_requirement_targets
        - numbered_content_clause_targets
        - absorbed_by_section
        - numbered_container_context_targets
        - ineligible_option_targets
    )
    projected_clause_targets = {
        unit.target_structure_node_id
        for unit in units
        if nodes_by_id[unit.target_structure_node_id].kind is StructureNodeKind.CLAUSE
        and (
            unit.target_structure_node_id in provisional_requirement_targets
            or _nearest_requirement_ancestor(
                unit.target_structure_node_id,
                requirement_targets=provisional_requirement_targets,
                nodes_by_id=nodes_by_id,
                component_for=component_for,
            )
            is not None
        )
    }
    absorbed_units_by_clause = _absorbed_direct_content_units_by_parent(
        projected_clause_targets,
        units_by_target=units_by_target,
        nodes_by_id=nodes_by_id,
        component_for=component_for,
        component_members=component_members,
        absorbable_classes={
            SemanticClass.REQUIREMENT,
            SemanticClass.UNRESOLVED,
            SemanticClass.TECHNICAL_INFORMATION,
        },
    )
    absorbed_by_clause = {
        unit.target_structure_node_id
        for absorbed_units in absorbed_units_by_clause.values()
        for unit in absorbed_units
    }
    for target_id, absorbed_units in absorbed_units_by_clause.items():
        units_by_target[target_id] = _projection_unit_with_absorbed_content(
            units_by_target[target_id],
            absorbed_units,
        )
    for target_id in absorbed_by_clause:
        units_by_target.pop(target_id, None)
    units = tuple(
        sorted(
            units_by_target.values(),
            key=lambda item: (
                item.source_order,
                item.target_structure_node_id,
            ),
        )
    )
    suppressed_targets = (
        absorbed_by_section
        | absorbed_by_clause
        | numbered_container_context_targets
        | ordinary_table_row_targets
        | ordinary_merged_table_support_targets
        | suppressed_reference_context_targets
        | ineligible_option_targets
    )
    requirement_targets = provisional_requirement_targets - absorbed_by_clause

    requirement_parent: dict[str, str | None] = {}
    role_by_target: dict[str, RequirementProjectionRole] = {}
    included_targets: set[str] = set()
    for target_id in requirement_targets:
        parent_target = (
            None
            if target_id in numbered_container_targets
            else _nearest_requirement_ancestor(
                target_id,
                requirement_targets=requirement_targets,
                nodes_by_id=nodes_by_id,
                component_for=component_for,
            )
        )
        requirement_parent[target_id] = parent_target
        if parent_target is None:
            role = RequirementProjectionRole.PRIMARY
        elif (
            units_by_target[target_id].requirement_subtype
            is RequirementSubtype.PERFORMANCE_PARAMETER_REQUIREMENT
            and nodes_by_id[target_id].kind is StructureNodeKind.TABLE_ROW
        ):
            role = RequirementProjectionRole.PARAMETER
        else:
            role = RequirementProjectionRole.CHILD
        role_by_target[target_id] = role
        included_targets.add(target_id)

    context_parent: dict[str, str] = {}
    for unit in units:
        target_id = unit.target_structure_node_id
        if target_id in requirement_targets or target_id in suppressed_targets:
            continue
        parent_target = _nearest_requirement_ancestor(
            target_id,
            requirement_targets=requirement_targets,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
        )
        if parent_target is None:
            continue
        context_parent[target_id] = parent_target
        role_by_target[target_id] = (
            RequirementProjectionRole.CONTENT
            if target_id in numbered_content_clause_targets
            else RequirementProjectionRole.REFERENCE
            if unit.semantic_class is SemanticClass.REFERENCE
            else RequirementProjectionRole.CONTENT
        )
        included_targets.add(target_id)

    for target_id, parent_target in reference_row_parent.items():
        if target_id in requirement_targets:
            continue
        context_parent[target_id] = parent_target
        role_by_target[target_id] = RequirementProjectionRole.REFERENCE
        included_targets.add(target_id)

    requirement_children: dict[str, set[str]] = defaultdict(set)
    for child_target, parent_target in requirement_parent.items():
        if parent_target is not None:
            requirement_children[parent_target].add(child_target)

    snapshot_id = stable_requirement_view_snapshot_id(
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        projector_id=projector_id,
        projector_version=projector_version,
        projection_policy=projection_policy,
        requirement_set_id=requirement_set_id,
    )
    view_id_by_target = {
        target_id: stable_requirement_view_node_id(
            requirement_view_snapshot_id=snapshot_id,
            semantic_unit_id=_primary_semantic_unit_id(
                units_by_target[target_id],
            ),
            source_semantic_unit_ids=_source_semantic_unit_ids(
                units_by_target[target_id],
                primary_semantic_unit_id=_primary_semantic_unit_id(
                    units_by_target[target_id],
                ),
            ),
            role=role_by_target[target_id],
            semantic_annotation_ids=tuple(
                annotation.semantic_annotation_id
                for annotation in units_by_target[target_id].annotations
            ),
        )
        for target_id in included_targets
    }

    top_primary_by_target = {
        target_id: _top_primary_target(
            target_id,
            requirement_parent=requirement_parent,
            context_parent=context_parent,
        )
        for target_id in included_targets
    }
    additional_context_by_primary: dict[str, set[str]] = defaultdict(set)
    for target_id, parent_target in reference_row_parent.items():
        additional_context_by_primary[parent_target].add(target_id)
    for target_id in included_targets:
        additional_context_by_primary[top_primary_by_target[target_id]].update(
            units_by_target[target_id].component_structure_node_ids
        )
    full_context_by_primary = {
        primary_target: _full_context_refs(
            primary_target,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
            additional_context_ids=additional_context_by_primary.get(
                primary_target,
                set(),
            ),
        )
        for primary_target in set(top_primary_by_target.values())
    }

    projected_nodes: list[RequirementViewNode] = []
    unit_order_by_id = {
        unit_id: unit_order
        for unit_order, unit_id in enumerate(semantics.scope_semantic_unit_ids)
    }
    for unit in sorted(
        (unit for unit in units if unit.target_structure_node_id in included_targets),
        key=lambda item: (item.source_order, item.target_structure_node_id),
    ):
        target_id = unit.target_structure_node_id
        role = role_by_target[target_id]
        if target_id in requirement_targets:
            parent_target = requirement_parent[target_id]
            decision_mode = (
                RequirementDecisionMode.ROLLUP
                if requirement_children.get(target_id)
                else RequirementDecisionMode.DIRECT
            )
        else:
            parent_target = context_parent[target_id]
            decision_mode = RequirementDecisionMode.NONE
        projected_nodes.append(
            RequirementViewNode(
                requirement_view_node_id=view_id_by_target[target_id],
                role=role,
                parent_requirement_view_node_id=(
                    view_id_by_target[parent_target]
                    if parent_target is not None
                    else None
                ),
                decision_mode=decision_mode,
                semantic_class=unit.semantic_class,
                requirement_subtype=unit.requirement_subtype,
                semantic_unit_id=_primary_semantic_unit_id(unit),
                source_semantic_unit_ids=_source_semantic_unit_ids(
                    unit,
                    primary_semantic_unit_id=_primary_semantic_unit_id(unit),
                ),
                owner_structure_node_id=target_id,
                semantic_annotation_ids=tuple(
                    annotation.semantic_annotation_id for annotation in unit.annotations
                ),
                ancestor_structure_node_ids=_ancestor_structure_node_ids(
                    target_id,
                    nodes_by_id=nodes_by_id,
                    component_for=component_for,
                ),
                source_span_ids=unit.source_span_ids,
                source_refs=unit.source_refs,
                full_context_source_refs=full_context_by_primary[
                    top_primary_by_target[target_id]
                ],
                relation_ids=_relevant_relation_ids(
                    target_id,
                    tree=tree,
                    nodes_by_id=nodes_by_id,
                    component_for=component_for,
                    component_members=component_members,
                ),
                source_order=unit.source_order,
                unit_order=unit_order_by_id[_primary_semantic_unit_id(unit)],
            )
        )

    return RequirementViewSnapshot(
        requirement_view_snapshot_id=snapshot_id,
        source_ir_id=tree.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantics.semantic_unit_plan_id,
        projector_id=projector_id,
        projector_version=projector_version,
        projection_policy=projection_policy,
        requirement_set_id=requirement_set_id,
        structure_node_ids=tuple(sorted(node.structure_node_id for node in tree.nodes)),
        structure_relation_ids=tuple(
            sorted(relation.relation_id for relation in tree.relations)
        ),
        semantic_unit_ids=tuple(sorted(semantics.scope_semantic_unit_ids)),
        semantic_annotation_ids=tuple(
            sorted(
                annotation.semantic_annotation_id
                for annotation in semantics.annotations
            )
        ),
        source_node_ids=tree.source_node_ids,
        source_cell_ids=tree.source_cell_ids,
        visual_text_observation_ids=tree.visual_text_observation_ids,
        nodes=tuple(projected_nodes),
    )


def _annotation_is_active(
    annotation: SemanticAnnotation,
    projection_policy: RequirementProjectionPolicy,
) -> bool:
    if projection_policy is RequirementProjectionPolicy.FORMAL_CONFIRMED:
        return annotation.status is SemanticAnnotationStatus.CONFIRMED
    return annotation.status is not SemanticAnnotationStatus.REJECTED


def _projection_units(
    annotations: tuple[SemanticAnnotation, ...],
    *,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
) -> tuple[_ProjectionUnit, ...]:
    grouped: dict[str, list[SemanticAnnotation]] = defaultdict(list)
    for annotation in annotations:
        grouped[component_for[annotation.target_structure_node_id]].append(annotation)

    units: list[_ProjectionUnit] = []
    for target_id, grouped_annotations in grouped.items():
        ordered_annotations = tuple(
            sorted(
                grouped_annotations,
                key=lambda item: (
                    nodes_by_id[item.target_structure_node_id].source_order,
                    item.semantic_annotation_id,
                ),
            )
        )
        classifications = {
            (annotation.semantic_class, annotation.requirement_subtype)
            for annotation in ordered_annotations
        }
        if len(classifications) != 1:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.CONFLICTING_CONTINUATION_SEMANTICS,
                "Continuation fragments have conflicting semantic classifications.",
            )
        semantic_class, requirement_subtype = next(iter(classifications))
        component_source_refs = {
            source_ref
            for component_node_id in component_members[target_id]
            for source_ref in nodes_by_id[
                component_node_id
            ].effective_semantic_source_refs
        }
        spans = tuple(
            span
            for annotation in ordered_annotations
            for span in annotation.grounding_spans
            if span.source_ref in component_source_refs
        )
        source_span_ids = _dedupe(tuple(span.grounding_span_id for span in spans))
        source_refs = _dedupe(tuple(span.source_ref for span in spans))
        if not source_span_ids or not source_refs:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "A projected semantic item has no exact source grounding.",
            )
        units.append(
            _ProjectionUnit(
                target_structure_node_id=target_id,
                component_structure_node_ids=component_members[target_id],
                annotations=ordered_annotations,
                semantic_class=semantic_class,
                requirement_subtype=requirement_subtype,
                source_span_ids=source_span_ids,
                source_refs=source_refs,
                source_order=nodes_by_id[target_id].source_order,
            )
        )
    return tuple(
        sorted(
            units, key=lambda item: (item.source_order, item.target_structure_node_id)
        )
    )


def _coalesce_bilingual_projection_units(
    units: tuple[_ProjectionUnit, ...],
    *,
    tree: StructureTreeSnapshot,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[_ProjectionUnit, ...]:
    """Union exact grounding for one typed bilingual identity.

    Source semantics remain untouched.  Only the review projection collapses
    two independently classified counterpart owners into one view node.
    """

    units_by_target = {unit.target_structure_node_id: unit for unit in units}
    for group in tree.bilingual_counterpart_groups:
        grouped_units = tuple(
            units_by_target[member_id]
            for member_id in group.member_structure_node_ids
            if member_id in units_by_target
        )
        if len(grouped_units) <= 1:
            continue
        requirement_units = tuple(
            unit
            for unit in grouped_units
            if unit.semantic_class is SemanticClass.REQUIREMENT
        )
        if requirement_units:
            requirement_subtypes = {
                unit.requirement_subtype for unit in requirement_units
            }
            if len(requirement_subtypes) != 1:
                raise RequirementProjectionRejected(
                    RequirementProjectionRejectionCode.INVALID_PROJECTION,
                    "Bilingual requirement counterparts have conflicting subtypes.",
                )
            semantic_class = SemanticClass.REQUIREMENT
            requirement_subtype = next(iter(requirement_subtypes))
        else:
            resolved_classifications = {
                (unit.semantic_class, unit.requirement_subtype)
                for unit in grouped_units
                if unit.semantic_class is not SemanticClass.UNRESOLVED
            }
            if len(resolved_classifications) > 1:
                # Structure identity never overrides two conflicting non-
                # requirement classifications.  Leave them reviewable as-is;
                # mismatch evaluation, when available, remains nonblocking.
                continue
            if resolved_classifications:
                semantic_class, requirement_subtype = next(
                    iter(resolved_classifications)
                )
            else:
                semantic_class = SemanticClass.UNRESOLVED
                requirement_subtype = None
        target_id = (
            group.canonical_structure_node_id
            if group.canonical_structure_node_id in units_by_target
            else min(
                (unit.target_structure_node_id for unit in grouped_units),
                key=lambda item: (nodes_by_id[item].source_order, item),
            )
        )
        ordered_units = tuple(
            sorted(
                grouped_units,
                key=lambda unit: (
                    nodes_by_id[unit.target_structure_node_id].source_order,
                    unit.target_structure_node_id,
                ),
            )
        )
        merged = _ProjectionUnit(
            target_structure_node_id=target_id,
            component_structure_node_ids=_dedupe(
                tuple(
                    component_id
                    for unit in ordered_units
                    for component_id in unit.component_structure_node_ids
                )
                + group.member_structure_node_ids
            ),
            annotations=tuple(
                sorted(
                    _dedupe(
                        tuple(
                            annotation
                            for unit in ordered_units
                            for annotation in unit.annotations
                        )
                    ),
                    key=lambda annotation: (
                        nodes_by_id[annotation.target_structure_node_id].source_order,
                        annotation.semantic_annotation_id,
                    ),
                )
            ),
            semantic_class=semantic_class,
            requirement_subtype=requirement_subtype,
            source_span_ids=_dedupe(
                tuple(
                    span_id
                    for unit in ordered_units
                    for span_id in unit.source_span_ids
                )
            ),
            source_refs=_dedupe(
                tuple(
                    source_ref
                    for unit in ordered_units
                    for source_ref in unit.source_refs
                )
            ),
            source_order=min(unit.source_order for unit in ordered_units),
        )
        for unit in grouped_units:
            units_by_target.pop(unit.target_structure_node_id, None)
        units_by_target[target_id] = merged
    return tuple(
        sorted(
            units_by_target.values(),
            key=lambda item: (item.source_order, item.target_structure_node_id),
        )
    )


def _model_continuation_units_by_projection_target(
    *,
    units_by_target: dict[str, _ProjectionUnit],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
    assembly: RequirementAssemblySnapshot,
) -> dict[str, tuple[_ProjectionUnit, ...]]:
    """Bind model-declared dependent tails to one included structural owner.

    Structure ``CONTINUES`` relations remain the fact-layer source of physical
    continuation components.  This narrower projection rule handles a second
    case: the model identifies an unlabeled direct child as the dependent tail
    of its structural parent even though the resolver retained two nodes.  The
    tail stays excluded as an item, but its exact spans and refs must remain
    part of the parent's judgeable projection.
    """

    assembly_by_target = {
        item.target_structure_node_id: item for item in assembly.items
    }
    included_targets = {
        item.target_structure_node_id
        for item in assembly.items
        if item.status is RequirementAssemblyStatus.CANDIDATE and item.include_in_view
    }
    continuation_targets = {
        target_id
        for target_id, unit in units_by_target.items()
        if unit.annotations
        and all(annotation.is_continuation_fragment for annotation in unit.annotations)
    }
    immediate_parent: dict[str, str] = {}
    for target_id in continuation_targets:
        unit = units_by_target[target_id]
        if unit.semantic_class is not SemanticClass.REQUIREMENT:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "A model continuation must retain requirement semantics.",
            )
        if any(
            nodes_by_id[member_id].kind
            not in {StructureNodeKind.CONTENT, StructureNodeKind.CONTINUATION}
            for member_id in component_members[target_id]
        ):
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "Only unlabeled content may be absorbed as a model continuation.",
            )
        assembly_item = assembly_by_target.get(target_id)
        if (
            assembly_item is None
            or assembly_item.status is not RequirementAssemblyStatus.CANDIDATE
            or assembly_item.include_in_view
            or assembly_item.role is not None
            or assembly_item.parent_structure_node_id is not None
        ):
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.ASSEMBLY_MISMATCH,
                "A model continuation must be a resolved excluded assembly item.",
            )
        parent_targets = {
            component_for[parent_id]
            for member_id in component_members[target_id]
            for parent_id in (nodes_by_id[member_id].parent_structure_node_id,)
            if parent_id is not None
        }
        if len(parent_targets) != 1:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "A model continuation must have one structural parent.",
            )
        immediate_parent[target_id] = next(iter(parent_targets))

    absorbed_by_target: dict[str, list[_ProjectionUnit]] = defaultdict(list)
    for continuation_target in continuation_targets:
        visited = {continuation_target}
        owner_target = immediate_parent[continuation_target]
        while owner_target in continuation_targets:
            if owner_target in visited:
                raise RequirementProjectionRejected(
                    RequirementProjectionRejectionCode.INVALID_PROJECTION,
                    "Model continuation ownership contains a cycle.",
                )
            visited.add(owner_target)
            owner_target = immediate_parent[owner_target]
        if owner_target not in included_targets or owner_target not in units_by_target:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "A model continuation has no included structural owner.",
            )
        owner = units_by_target[owner_target]
        continuation = units_by_target[continuation_target]
        if (
            continuation.semantic_class is not owner.semantic_class
            or continuation.requirement_subtype is not owner.requirement_subtype
            or continuation.source_order <= owner.source_order
        ):
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.CONFLICTING_CONTINUATION_SEMANTICS,
                "A model continuation conflicts with its structural owner.",
            )
        absorbed_by_target[owner_target].append(continuation)

    return {
        target_id: tuple(
            sorted(
                continuations,
                key=lambda item: (
                    item.source_order,
                    item.target_structure_node_id,
                ),
            )
        )
        for target_id, continuations in absorbed_by_target.items()
    }


def _projection_unit_with_model_continuations(
    owner: _ProjectionUnit,
    continuations: tuple[_ProjectionUnit, ...],
) -> _ProjectionUnit:
    if not continuations:
        return owner
    ordered = (owner, *continuations)
    return _ProjectionUnit(
        target_structure_node_id=owner.target_structure_node_id,
        component_structure_node_ids=_dedupe(
            tuple(
                component_id
                for unit in ordered
                for component_id in unit.component_structure_node_ids
            )
        ),
        annotations=tuple(
            annotation for unit in ordered for annotation in unit.annotations
        ),
        semantic_class=owner.semantic_class,
        requirement_subtype=owner.requirement_subtype,
        source_span_ids=_dedupe(
            tuple(span_id for unit in ordered for span_id in unit.source_span_ids)
        ),
        source_refs=_dedupe(
            tuple(source_ref for unit in ordered for source_ref in unit.source_refs)
        ),
        source_order=owner.source_order,
    )


def _consecutive_projection_unit_groups(
    units: tuple[_ProjectionUnit, ...],
    *,
    unit_order_by_target: dict[str, int],
) -> tuple[tuple[_ProjectionUnit, ...], ...]:
    groups: list[list[_ProjectionUnit]] = []
    for unit in sorted(
        units,
        key=lambda item: (
            unit_order_by_target[item.target_structure_node_id],
            item.target_structure_node_id,
        ),
    ):
        if not groups:
            groups.append([unit])
            continue
        previous = groups[-1][-1]
        if (
            unit_order_by_target[unit.target_structure_node_id]
            == unit_order_by_target[previous.target_structure_node_id] + 1
        ):
            groups[-1].append(unit)
        else:
            groups.append([unit])
    return tuple(tuple(group) for group in groups)


def _unit_reason_codes(unit: _ProjectionUnit) -> set[str]:
    return {
        reason_code
        for annotation in unit.annotations
        for reason_code in annotation.reason_codes
    }


def _reference_row_context_parents(
    *,
    units_by_target: dict[str, _ProjectionUnit],
    nodes_by_id: dict[str, StructureNode],
    semantic_requirement_targets: set[str],
) -> tuple[dict[str, str], set[str]]:
    """Associate data rows in a reference table with one applicability rule.

    The rule is deliberately narrow: a REFERENCE section must contain exactly
    one direct requirement and at least one direct table.  Only table rows with
    REFERENCE semantics are linked, and row zero of the earliest table remains
    the non-projectable header.  Later page fragments keep row zero because a
    continuation is not assumed to repeat the header.
    """

    children: dict[str, list[StructureNode]] = defaultdict(list)
    for node in nodes_by_id.values():
        if node.parent_structure_node_id is not None:
            children[node.parent_structure_node_id].append(node)
    for items in children.values():
        items.sort(key=lambda item: (item.source_order, item.structure_node_id))

    result: dict[str, str] = {}
    suppressed_targets: set[str] = set()
    for section_id, section_unit in units_by_target.items():
        section = nodes_by_id[section_id]
        if section.kind is not StructureNodeKind.SECTION:
            continue
        if (
            section_unit.semantic_class is SemanticClass.REQUIREMENT
            and section_unit.requirement_subtype
            is RequirementSubtype.REGULATION_REQUIREMENT
        ):
            direct_requirements = [section_id]
        elif section_unit.semantic_class is SemanticClass.REFERENCE:
            direct_requirements = [
                child.structure_node_id
                for child in children.get(section_id, ())
                if child.structure_node_id in semantic_requirement_targets
            ]
        else:
            continue
        direct_tables = [
            child
            for child in children.get(section_id, ())
            if child.kind is StructureNodeKind.TABLE
            and child.structure_node_id in units_by_target
            and units_by_target[child.structure_node_id].semantic_class
            is SemanticClass.REFERENCE
        ]
        if len(direct_requirements) != 1 or not direct_tables:
            continue
        requirement_target = direct_requirements[0]
        if (
            nodes_by_id[requirement_target].source_order
            >= direct_tables[0].source_order
        ):
            continue
        first_table_id = direct_tables[0].structure_node_id
        for table in direct_tables:
            rows = [
                row
                for row in children.get(table.structure_node_id, ())
                if row.kind is StructureNodeKind.TABLE_ROW
                and row.structure_node_id in units_by_target
                and units_by_target[row.structure_node_id].semantic_class
                is SemanticClass.REFERENCE
            ]
            for row in rows:
                if (
                    table.structure_node_id == first_table_id
                    and row.table_row_index == 0
                ):
                    continue
                result[row.structure_node_id] = requirement_target
        for target_id, unit in units_by_target.items():
            if (
                target_id == section_id
                or target_id in result
                or unit.semantic_class is not SemanticClass.REFERENCE
                or not _has_structure_ancestor(
                    target_id,
                    ancestor_id=section_id,
                    nodes_by_id=nodes_by_id,
                )
            ):
                continue
            suppressed_targets.add(target_id)
    return result, suppressed_targets


def _has_structure_ancestor(
    target_id: str,
    *,
    ancestor_id: str,
    nodes_by_id: dict[str, StructureNode],
) -> bool:
    parent_id = nodes_by_id[target_id].parent_structure_node_id
    while parent_id is not None:
        if parent_id == ancestor_id:
            return True
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return False


def _absorbed_direct_content_targets(
    parent_targets: set[str],
    *,
    units_by_target: dict[str, _ProjectionUnit],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
    first_only: bool,
) -> set[str]:
    absorbed: set[str] = set()
    for parent_target in parent_targets:
        parent_grounding_refs = {
            span.source_ref
            for annotation in units_by_target[parent_target].annotations
            for span in annotation.grounding_spans
        }
        candidates = [
            unit
            for unit in units_by_target.values()
            if unit.target_structure_node_id != parent_target
            and nodes_by_id[unit.target_structure_node_id].kind
            is StructureNodeKind.CONTENT
            and _component_is_direct_child(
                unit.target_structure_node_id,
                parent_target,
                nodes_by_id=nodes_by_id,
                component_for=component_for,
                component_members=component_members,
            )
            and set(unit.source_refs).issubset(parent_grounding_refs)
        ]
        candidates.sort(
            key=lambda item: (item.source_order, item.target_structure_node_id)
        )
        if first_only:
            candidates = candidates[:1]
        absorbed.update(unit.target_structure_node_id for unit in candidates)
    return absorbed


def _absorbed_direct_content_units_by_parent(
    parent_targets: set[str],
    *,
    units_by_target: dict[str, _ProjectionUnit],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
    absorbable_classes: set[SemanticClass],
) -> dict[str, tuple[_ProjectionUnit, ...]]:
    """Return every physical body fragment owned by an explicit clause.

    A PDF block/line is a source fact, not a demand boundary.  Classification
    places all direct body refs in the explicit Section's evidence window; this
    projection step then preserves those exact spans under one requirement
    identity instead of promoting each physical fragment.
    """

    result: dict[str, tuple[_ProjectionUnit, ...]] = {}
    for parent_target in parent_targets:
        parent_grounding_refs = {
            span.source_ref
            for annotation in units_by_target[parent_target].annotations
            for span in annotation.grounding_spans
        }
        candidates = tuple(
            sorted(
                (
                    unit
                    for unit in units_by_target.values()
                    if unit.target_structure_node_id != parent_target
                    and nodes_by_id[unit.target_structure_node_id].kind
                    in {
                        StructureNodeKind.CONTENT,
                        StructureNodeKind.CONTINUATION,
                    }
                    and unit.semantic_class in absorbable_classes
                    and _component_is_direct_child(
                        unit.target_structure_node_id,
                        parent_target,
                        nodes_by_id=nodes_by_id,
                        component_for=component_for,
                        component_members=component_members,
                    )
                    and set(unit.source_refs).issubset(parent_grounding_refs)
                ),
                key=lambda item: (
                    item.source_order,
                    item.target_structure_node_id,
                ),
            )
        )
        if candidates:
            result[parent_target] = candidates
    return result


def _mixed_content_section_targets(
    section_targets: set[str],
    *,
    source_ir: SourceIRSnapshot,
    nodes_by_id: dict[str, StructureNode],
) -> set[str]:
    """Return explicit Sections that own a table, figure, or attachment.

    A pure-text clause may be split into several PDF blocks without creating
    several demands, so its body fragments can be folded into the clause.  A
    Section containing an embedded structured object is different: it is a
    mixed-content container whose surrounding paragraphs, object, caption and
    post-object obligations must remain independently addressable.  Folding
    those children into the Section loses both the table/figure presentation
    and valid decision boundaries.
    """

    embedded_source_kinds = {
        SourceNodeKind.TABLE,
        SourceNodeKind.IMAGE,
        SourceNodeKind.FIGURE,
        SourceNodeKind.ATTACHMENT,
    }
    source_kind_by_id = {
        node.source_node_id: node.kind for node in source_ir.nodes
    }
    children_by_parent: dict[str, list[StructureNode]] = {}
    for node in nodes_by_id.values():
        if node.parent_structure_node_id is not None:
            children_by_parent.setdefault(
                node.parent_structure_node_id,
                [],
            ).append(node)
    result: set[str] = set()
    for section_id in section_targets:
        pending = list(children_by_parent.get(section_id, ()))
        while pending:
            node = pending.pop()
            if node.kind is StructureNodeKind.SECTION:
                continue
            if node.kind is StructureNodeKind.TABLE or any(
                source_ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_kind_by_id.get(source_ref.source_id)
                in embedded_source_kinds
                for source_ref in node.effective_semantic_source_refs
            ):
                result.add(section_id)
                break
            pending.extend(children_by_parent.get(node.structure_node_id, ()))
    return result


def _projection_unit_with_absorbed_content(
    owner: _ProjectionUnit,
    absorbed: tuple[_ProjectionUnit, ...],
) -> _ProjectionUnit:
    ordered = (owner, *absorbed)
    return _ProjectionUnit(
        target_structure_node_id=owner.target_structure_node_id,
        component_structure_node_ids=_dedupe(
            tuple(
                component_id
                for unit in ordered
                for component_id in unit.component_structure_node_ids
            )
        ),
        annotations=tuple(
            annotation
            for unit in ordered
            for annotation in unit.annotations
        ),
        semantic_class=owner.semantic_class,
        requirement_subtype=owner.requirement_subtype,
        source_span_ids=_dedupe(
            tuple(
                span_id
                for unit in ordered
                for span_id in unit.source_span_ids
            )
        ),
        source_refs=_dedupe(
            tuple(
                source_ref
                for unit in ordered
                for source_ref in unit.source_refs
            )
        ),
        source_order=owner.source_order,
    )


def _is_direct_numbered_clause_of_container(
    target_id: str,
    *,
    container_targets: set[str],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
) -> bool:
    target = nodes_by_id[target_id]
    if target.kind is not StructureNodeKind.CLAUSE or target.list_ordinal is None:
        return False
    return any(
        _component_is_direct_child(
            target_id,
            container_target,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
        )
        for container_target in container_targets
    )


def _has_structural_ancestor_in(
    target_id: str,
    *,
    ancestor_targets: set[str],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
) -> bool:
    parent_id = nodes_by_id[target_id].parent_structure_node_id
    visited: set[str] = set()
    while parent_id is not None:
        if parent_id in visited:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "Structure ancestry is cyclic.",
            )
        visited.add(parent_id)
        if component_for[parent_id] in ancestor_targets:
            return True
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return False


def _component_is_direct_child(
    child_target: str,
    parent_target: str,
    *,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
) -> bool:
    return any(
        node.parent_structure_node_id is not None
        and component_for[node.parent_structure_node_id] == parent_target
        for node in (
            nodes_by_id[member_id] for member_id in component_members[child_target]
        )
    )


def _continuation_components(
    tree: StructureTreeSnapshot,
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    parent = {node_id: node_id for node_id in nodes_by_id}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for relation in tree.relations:
        if relation.kind is StructureRelationKind.CONTINUES:
            source_node = nodes_by_id[relation.source_structure_node_id]
            target_node = nodes_by_id[relation.target_structure_node_id]
            if not _continuation_nodes_are_same_logical_unit(
                source_node,
                target_node,
            ):
                continue
            union(
                relation.source_structure_node_id,
                relation.target_structure_node_id,
            )

    raw_components: dict[str, list[str]] = defaultdict(list)
    for node_id in nodes_by_id:
        raw_components[find(node_id)].append(node_id)
    component_for: dict[str, str] = {}
    component_members: dict[str, tuple[str, ...]] = {}
    for members in raw_components.values():
        ordered = tuple(
            sorted(
                members,
                key=lambda item: (nodes_by_id[item].source_order, item),
            )
        )
        canonical = ordered[0]
        component_members[canonical] = ordered
        for member in ordered:
            component_for[member] = canonical
    return component_for, component_members


def _continuation_nodes_are_same_logical_unit(
    source: StructureNode,
    target: StructureNode,
) -> bool:
    if StructureNodeKind.CONTINUATION in {source.kind, target.kind}:
        return (
            source.parent_structure_node_id == target.parent_structure_node_id
            or source.parent_structure_node_id == target.structure_node_id
            or target.parent_structure_node_id == source.structure_node_id
        )
    if source.kind is not target.kind:
        return False
    if source.parent_structure_node_id != target.parent_structure_node_id:
        return False
    if source.kind is StructureNodeKind.CLAUSE:
        return source.list_ordinal == target.list_ordinal
    if source.kind is StructureNodeKind.SECTION:
        return source.explicit_label == target.explicit_label
    return True


def _nearest_requirement_ancestor(
    target_id: str,
    *,
    requirement_targets: set[str],
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
) -> str | None:
    parent_id = nodes_by_id[target_id].parent_structure_node_id
    visited: set[str] = set()
    while parent_id is not None:
        if parent_id in visited:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "Structure ancestry is cyclic.",
            )
        visited.add(parent_id)
        canonical = component_for[parent_id]
        if canonical != target_id and canonical in requirement_targets:
            return canonical
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return None


def _ancestor_structure_node_ids(
    target_id: str,
    *,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str] | None = None,
) -> tuple[str, ...]:
    ancestors: list[str] = []
    parent_id = nodes_by_id[target_id].parent_structure_node_id
    while parent_id is not None:
        ancestors.append(parent_id)
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    ancestors.reverse()
    if component_for is None:
        return tuple(ancestors)

    # A continued table is one logical structural owner even though rows on
    # later pages remain children of their physical table part.  Include the
    # continuation representative next to each physical ancestor so a
    # projected row can be parented by the single logical-table view node
    # without weakening the general parent/ancestor contract.
    logical_ancestors: list[str] = []
    for ancestor_id in ancestors:
        for candidate_id in (
            ancestor_id,
            component_for.get(ancestor_id, ancestor_id),
        ):
            if (
                candidate_id != target_id
                and candidate_id not in logical_ancestors
            ):
                logical_ancestors.append(candidate_id)
    return tuple(logical_ancestors)


def _top_primary_target(
    target_id: str,
    *,
    requirement_parent: dict[str, str | None],
    context_parent: dict[str, str],
) -> str:
    current = (
        target_id if target_id in requirement_parent else context_parent[target_id]
    )
    parent = requirement_parent[current]
    while parent is not None:
        current = parent
        parent = requirement_parent[current]
    return current


def _full_context_refs(
    primary_target_id: str,
    *,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
    additional_context_ids: set[str] | None = None,
) -> tuple[StructureSourceRef, ...]:
    children_by_parent: dict[str, list[str]] = defaultdict(list)
    for node in nodes_by_id.values():
        if node.parent_structure_node_id is not None:
            children_by_parent[node.parent_structure_node_id].append(
                node.structure_node_id
            )

    context_ids: set[str] = set()
    pending = [primary_target_id]
    while pending:
        node_id = pending.pop()
        if node_id in context_ids:
            continue
        context_ids.add(node_id)
        pending.extend(children_by_parent.get(node_id, ()))
    for node_id in tuple(context_ids):
        canonical = component_for[node_id]
        context_ids.update(component_members[canonical])
    for node_id in additional_context_ids or ():
        canonical = component_for[node_id]
        context_ids.update(component_members[canonical])

    ordered_nodes = sorted(
        (nodes_by_id[node_id] for node_id in context_ids),
        key=lambda node: (node.source_order, node.structure_node_id),
    )
    refs = _dedupe(
        tuple(
            ref for node in ordered_nodes for ref in node.effective_semantic_source_refs
        )
    )
    if not refs:
        raise RequirementProjectionRejected(
            RequirementProjectionRejectionCode.INVALID_PROJECTION,
            "A PRIMARY projection has no source-grounded full context.",
        )
    return refs


def _relevant_relation_ids(
    target_id: str,
    *,
    tree: StructureTreeSnapshot,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    relevant: set[str] = set()
    component = set(component_members[component_for[target_id]])

    child_id = target_id
    parent_id = nodes_by_id[child_id].parent_structure_node_id
    while parent_id is not None:
        matches = [
            relation
            for relation in tree.relations
            if relation.kind is StructureRelationKind.CONTAINS
            and relation.source_structure_node_id == parent_id
            and relation.target_structure_node_id == child_id
        ]
        if len(matches) != 1:
            raise RequirementProjectionRejected(
                RequirementProjectionRejectionCode.INVALID_PROJECTION,
                "Structure ancestry lacks one exact contains relation.",
            )
        relevant.add(matches[0].relation_id)
        child_id = parent_id
        parent_id = nodes_by_id[child_id].parent_structure_node_id

    for relation in tree.relations:
        if relation.kind is StructureRelationKind.CONTAINS:
            continue
        if (
            relation.source_structure_node_id in component
            or relation.target_structure_node_id in component
        ):
            relevant.add(relation.relation_id)
    return tuple(
        relation.relation_id
        for relation in tree.relations
        if relation.relation_id in relevant
    )


def _relevant_relation_ids_for_targets(
    target_ids: tuple[str, ...],
    *,
    tree: StructureTreeSnapshot,
    nodes_by_id: dict[str, StructureNode],
    component_for: dict[str, str],
    component_members: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    relevant = {
        relation_id
        for target_id in target_ids
        for relation_id in _relevant_relation_ids(
            target_id,
            tree=tree,
            nodes_by_id=nodes_by_id,
            component_for=component_for,
            component_members=component_members,
        )
    }
    return tuple(
        relation.relation_id
        for relation in tree.relations
        if relation.relation_id in relevant
    )


_T = TypeVar("_T")


def _dedupe(values: tuple[_T, ...]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(values))


__all__ = [
    "ASSEMBLY_REQUIREMENT_PROJECTOR_ID",
    "ASSEMBLY_REQUIREMENT_PROJECTOR_VERSION",
    "DEFAULT_REQUIREMENT_PROJECTOR_ID",
    "DEFAULT_REQUIREMENT_PROJECTOR_VERSION",
    "RequirementProjectionRejected",
    "RequirementProjectionRejectionCode",
    "project_requirements",
    "project_requirements_from_assembly",
    "validate_assembled_requirement_view_against_inputs",
    "validate_requirement_view_against_inputs",
]
