from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.application.extraction.adapt_semantic_unit_decisions import (
    CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION,
    ControlledSemanticUnitDecisionExecution,
)
from app.application.extraction.assemble_requirements import (
    RequirementAssemblyRejected,
    validate_requirement_assembly_against_inputs,
)
from app.application.extraction.classify_content import (
    SemanticClassificationRejected,
    validate_semantic_classification_against_inputs,
)
from app.domain.extraction.requirement_assembly import (
    RequirementAssemblyItem,
    RequirementAssemblySnapshot,
    RequirementAssemblyStatus,
    stable_requirement_assembly_id,
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
from app.domain.extraction.semantic_units import SemanticUnitPlan
from app.domain.extraction.semantics import (
    SemanticAnnotation,
    SemanticAnnotationStatus,
    SemanticCandidateSource,
    SemanticClass,
    SemanticClassificationSnapshot,
    stable_semantic_annotation_id,
    stable_semantic_snapshot_id,
)
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.domain.extraction.structure import (
    StructureNode,
    StructureSourceRef,
    StructureTreeSnapshot,
)


CONTROLLED_SEMANTIC_CLASSIFIER_ID = "controlled-semantic-unit-classifier"
CONTROLLED_SEMANTIC_CLASSIFIER_VERSION = "1.0.0"
CONTROLLED_SEMANTIC_ASSEMBLER_ID = "controlled-semantic-unit-assembler"
CONTROLLED_SEMANTIC_ASSEMBLER_VERSION = "1.0.0"
CONTROLLED_SEMANTIC_PROJECTOR_ID = "controlled-semantic-unit-projector"
CONTROLLED_SEMANTIC_PROJECTOR_VERSION = "1.0.0"

_SEMANTIC_UNIT_ID_PATTERN = re.compile(r"^semantic-unit-[0-9a-f]{32}$")
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ControlledSemanticUnitDownstreamRejectionCode(str, Enum):
    INPUT_MISMATCH = "controlled_semantic_downstream_input_mismatch"
    INVALID_SEMANTICS = "controlled_semantic_downstream_invalid_semantics"
    INVALID_ASSEMBLY = "controlled_semantic_downstream_invalid_assembly"
    INVALID_PROJECTION = "controlled_semantic_downstream_invalid_projection"


class ControlledSemanticUnitDownstreamRejected(RuntimeError):
    """Fail-closed controlled downstream error without source text."""

    def __init__(
        self,
        code: ControlledSemanticUnitDownstreamRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


class ControlledSemanticUnitAssemblyDirective(_StrictFrozenModel):
    """Provider-neutral assembly choice over one primary semantic unit."""

    semantic_unit_id: str = Field(pattern=r"^semantic-unit-[0-9a-f]{32}$")
    absorbed_semantic_unit_ids: tuple[str, ...] = ()
    include_in_view: bool
    role: RequirementProjectionRole | None = None
    parent_semantic_unit_id: str | None = Field(
        default=None,
        pattern=r"^semantic-unit-[0-9a-f]{32}$",
    )
    status: RequirementAssemblyStatus = RequirementAssemblyStatus.CANDIDATE
    reason_codes: tuple[str, ...] = ()

    @field_validator("absorbed_semantic_unit_ids")
    @classmethod
    def validate_absorbed_unit_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _SEMANTIC_UNIT_ID_PATTERN.fullmatch(item) for item in value):
            raise ValueError("absorbed units require stable semantic-unit IDs")
        if len(value) != len(set(value)):
            raise ValueError("absorbed semantic units must be unique")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _REASON_CODE_PATTERN.fullmatch(item) for item in value):
            raise ValueError("assembly reason codes must be machine-readable")
        if value != tuple(sorted(set(value))):
            raise ValueError("assembly reason codes must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_directive(self) -> ControlledSemanticUnitAssemblyDirective:
        if self.semantic_unit_id in self.absorbed_semantic_unit_ids:
            raise ValueError("an assembly directive cannot absorb its primary unit")
        if self.status is RequirementAssemblyStatus.UNRESOLVED:
            if (
                self.include_in_view
                or self.role is not None
                or self.parent_semantic_unit_id is not None
                or self.absorbed_semantic_unit_ids
                or not self.reason_codes
            ):
                raise ValueError(
                    "unresolved directives must be excluded, unparented, and explained"
                )
            return self
        if not self.include_in_view:
            if (
                self.role is not None
                or self.parent_semantic_unit_id is not None
                or self.absorbed_semantic_unit_ids
            ):
                raise ValueError(
                    "excluded directives cannot define role, parent, or absorption"
                )
            return self
        if self.role is None:
            raise ValueError("included directives require a projection role")
        if self.role is RequirementProjectionRole.PRIMARY:
            if self.parent_semantic_unit_id is not None:
                raise ValueError("PRIMARY directives cannot have a parent")
        elif self.parent_semantic_unit_id is None:
            raise ValueError("non-PRIMARY directives require a semantic-unit parent")
        return self

    @property
    def source_semantic_unit_ids(self) -> tuple[str, ...]:
        return (self.semantic_unit_id, *self.absorbed_semantic_unit_ids)


@dataclass(frozen=True)
class ControlledSemanticUnitDownstreamExecution:
    semantic_unit_plan: SemanticUnitPlan
    semantics: SemanticClassificationSnapshot
    assembly: RequirementAssemblySnapshot
    requirement_view: RequirementViewSnapshot

    def __post_init__(self) -> None:
        plan_id = self.semantic_unit_plan.semantic_unit_plan_id
        if (
            self.semantics.semantic_unit_plan_id != plan_id
            or self.assembly.semantic_unit_plan_id != plan_id
            or self.requirement_view.semantic_unit_plan_id != plan_id
            or self.assembly.semantic_snapshot_id != self.semantics.semantic_snapshot_id
            or self.requirement_view.semantic_snapshot_id
            != self.semantics.semantic_snapshot_id
        ):
            raise ValueError(
                "controlled downstream snapshots must retain one unit-plan identity"
            )


def build_controlled_semantic_classification(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    execution: ControlledSemanticUnitDecisionExecution,
) -> SemanticClassificationSnapshot:
    """Convert one-decision-per-unit results into a multi-owner semantic snapshot."""

    if not isinstance(execution, ControlledSemanticUnitDecisionExecution):
        raise TypeError("execution must be a ControlledSemanticUnitDecisionExecution")
    _validate_input_bindings(source_ir, tree, execution.plan.semantic_unit_plan)
    requests = execution.plan.requests
    if any(request.source_sha256 != source_ir.source_sha256 for request in requests):
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INPUT_MISMATCH,
            "Controlled unit requests do not belong to the supplied source bytes.",
        )

    unit_plan = execution.plan.semantic_unit_plan
    semantic_snapshot_id = stable_semantic_snapshot_id(
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=tree.structure_snapshot_id,
        classifier_id=CONTROLLED_SEMANTIC_CLASSIFIER_ID,
        classifier_version=CONTROLLED_SEMANTIC_CLASSIFIER_VERSION,
        semantic_unit_plan_id=unit_plan.semantic_unit_plan_id,
        unitizer_version=unit_plan.unitizer_version,
        scope_semantic_unit_ids=unit_plan.semantic_unit_ids,
    )
    provider_run_id = _stable_runtime_id(
        "controlled-semantic-provider-run",
        execution.provider_id,
        execution.provider_version,
        execution.model_id,
        tuple(item.decision_request_id for item in execution.decisions),
    )
    annotations: list[SemanticAnnotation] = []
    for request, decision in zip(requests, execution.decisions, strict=True):
        selected_span_ids = set(decision.evidence_grounding_span_ids)
        selected_refs = _dedupe(
            tuple(
                evidence.grounding_span.source_ref
                for evidence in request.evidence
                if evidence.grounding_span.grounding_span_id in selected_span_ids
            )
        )
        annotations.append(
            SemanticAnnotation(
                semantic_annotation_id=stable_semantic_annotation_id(
                    semantic_snapshot_id=semantic_snapshot_id,
                    semantic_unit_id=decision.semantic_unit_id,
                ),
                semantic_unit_id=decision.semantic_unit_id,
                owner_structure_node_id=decision.owner_structure_node_id,
                semantic_class=decision.semantic_class,
                requirement_subtype=decision.requirement_subtype,
                candidate_source=SemanticCandidateSource.MODEL,
                provider_run_id=provider_run_id,
                grounding_spans=request.semantic_unit.source_spans,
                selected_evidence_source_refs=selected_refs,
                is_continuation_fragment=decision.is_continuation_fragment,
                status=(
                    SemanticAnnotationStatus.UNRESOLVED
                    if decision.semantic_class is SemanticClass.UNRESOLVED
                    else SemanticAnnotationStatus.CANDIDATE
                ),
                reason_codes=decision.reason_codes,
            )
        )
    result = SemanticClassificationSnapshot(
        semantic_snapshot_id=semantic_snapshot_id,
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=tree.structure_snapshot_id,
        classifier_id=CONTROLLED_SEMANTIC_CLASSIFIER_ID,
        classifier_version=CONTROLLED_SEMANTIC_CLASSIFIER_VERSION,
        semantic_unit_plan_id=unit_plan.semantic_unit_plan_id,
        unitizer_version=unit_plan.unitizer_version,
        scope_semantic_unit_ids=unit_plan.semantic_unit_ids,
        scope_owner_structure_node_ids=tuple(
            item.owner_structure_node_id for item in unit_plan.units
        ),
        annotations=tuple(annotations),
    )
    try:
        validate_semantic_classification_against_inputs(
            result,
            source_ir,
            tree,
            semantic_unit_plan=unit_plan,
        )
    except SemanticClassificationRejected as exc:
        raise ControlledSemanticUnitDownstreamRejected(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_SEMANTICS,
            "Controlled semantic classification violated its unit-plan contract.",
        ) from exc
    return result


def assemble_controlled_semantic_units(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
    semantics: SemanticClassificationSnapshot,
    directives: tuple[ControlledSemanticUnitAssemblyDirective, ...],
) -> RequirementAssemblySnapshot:
    """Build a source-partitioning assembly without Structure-target duplication."""

    _validate_input_bindings(source_ir, tree, semantic_unit_plan)
    if not isinstance(semantics, SemanticClassificationSnapshot):
        raise TypeError("semantics must be a SemanticClassificationSnapshot")
    if not isinstance(directives, tuple) or not directives:
        raise TypeError("directives must be a non-empty tuple")
    if any(
        not isinstance(item, ControlledSemanticUnitAssemblyDirective)
        for item in directives
    ):
        raise TypeError(
            "directives must contain ControlledSemanticUnitAssemblyDirective values"
        )
    try:
        validate_semantic_classification_against_inputs(
            semantics,
            source_ir,
            tree,
            semantic_unit_plan=semantic_unit_plan,
        )
    except SemanticClassificationRejected as exc:
        raise ControlledSemanticUnitDownstreamRejected(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_SEMANTICS,
            "Controlled assembly received invalid semantic input.",
        ) from exc

    units_by_id = {item.semantic_unit_id: item for item in semantic_unit_plan.units}
    annotations_by_unit = {
        item.semantic_unit_id: item for item in semantics.annotations
    }
    primary_ids = tuple(item.semantic_unit_id for item in directives)
    flattened_ids = tuple(
        unit_id
        for directive in directives
        for unit_id in directive.source_semantic_unit_ids
    )
    if (
        len(primary_ids) != len(set(primary_ids))
        or flattened_ids != semantic_unit_plan.semantic_unit_ids
        or any(unit_id not in units_by_id for unit_id in flattened_ids)
    ):
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
            "Controlled directives must exactly partition semantic-unit scope in order.",
        )
    unit_order_by_id = {
        item.semantic_unit_id: item.unit_order for item in semantic_unit_plan.units
    }
    if tuple(unit_order_by_id[item] for item in primary_ids) != tuple(
        sorted(unit_order_by_id[item] for item in primary_ids)
    ):
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
            "Controlled assembly primaries must preserve semantic unit order.",
        )

    items: list[RequirementAssemblyItem] = []
    for directive in directives:
        source_unit_ids = directive.source_semantic_unit_ids
        lineage_orders = tuple(unit_order_by_id[item] for item in source_unit_ids)
        if lineage_orders != tuple(sorted(lineage_orders)):
            _reject(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
                "Controlled assembly lineage must preserve semantic unit order.",
            )
        annotation = annotations_by_unit[directive.semantic_unit_id]
        if (annotation.status is SemanticAnnotationStatus.UNRESOLVED) != (
            directive.status is RequirementAssemblyStatus.UNRESOLVED
        ):
            _reject(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
                "Controlled assembly must preserve unresolved semantic status.",
            )
        for absorbed_id in directive.absorbed_semantic_unit_ids:
            absorbed = annotations_by_unit[absorbed_id]
            if (
                not absorbed.is_continuation_fragment
                or absorbed.semantic_class is not annotation.semantic_class
                or absorbed.requirement_subtype is not annotation.requirement_subtype
                or units_by_id[absorbed_id].owner_structure_node_id
                != units_by_id[directive.semantic_unit_id].owner_structure_node_id
            ):
                _reject(
                    ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
                    "Only compatible same-owner continuation units may be absorbed.",
                )
        parent_owner_id = (
            units_by_id[directive.parent_semantic_unit_id].owner_structure_node_id
            if directive.parent_semantic_unit_id is not None
            and directive.parent_semantic_unit_id in units_by_id
            else None
        )
        if (
            directive.parent_semantic_unit_id is not None
            and directive.parent_semantic_unit_id not in primary_ids
        ):
            _reject(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
                "Controlled assembly parents must be included primary directives.",
            )
        primary_unit = units_by_id[directive.semantic_unit_id]
        items.append(
            RequirementAssemblyItem(
                semantic_unit_id=directive.semantic_unit_id,
                source_semantic_unit_ids=source_unit_ids,
                owner_structure_node_id=primary_unit.owner_structure_node_id,
                source_owner_structure_node_ids=tuple(
                    units_by_id[item].owner_structure_node_id
                    for item in source_unit_ids
                ),
                unit_order=primary_unit.unit_order,
                source_order=primary_unit.source_order,
                include_in_view=directive.include_in_view,
                role=directive.role,
                parent_semantic_unit_id=directive.parent_semantic_unit_id,
                parent_owner_structure_node_id=parent_owner_id,
                status=directive.status,
                reason_codes=directive.reason_codes,
            )
        )
    item_tuple = tuple(items)
    provider_run_id = _stable_runtime_id(
        "controlled-semantic-assembly-run",
        semantics.semantic_snapshot_id,
        tuple(item.model_dump(mode="json") for item in directives),
    )
    result = RequirementAssemblySnapshot(
        requirement_assembly_id=stable_requirement_assembly_id(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=tree.structure_snapshot_id,
            semantic_snapshot_id=semantics.semantic_snapshot_id,
            semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
            assembler_id=CONTROLLED_SEMANTIC_ASSEMBLER_ID,
            assembler_version=CONTROLLED_SEMANTIC_ASSEMBLER_VERSION,
            provider_run_id=provider_run_id,
            items=item_tuple,
        ),
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=tree.structure_snapshot_id,
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
        assembler_id=CONTROLLED_SEMANTIC_ASSEMBLER_ID,
        assembler_version=CONTROLLED_SEMANTIC_ASSEMBLER_VERSION,
        provider_run_id=provider_run_id,
        scope_semantic_unit_ids=semantic_unit_plan.semantic_unit_ids,
        scope_owner_structure_node_ids=tuple(
            item.owner_structure_node_id for item in semantic_unit_plan.units
        ),
        items=item_tuple,
    )
    try:
        validate_requirement_assembly_against_inputs(
            result,
            source_ir,
            tree,
            semantics,
        )
    except RequirementAssemblyRejected as exc:
        raise ControlledSemanticUnitDownstreamRejected(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_ASSEMBLY,
            "Controlled assembly violated its source or parent contract.",
        ) from exc
    return result


def project_controlled_semantic_units(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
    semantics: SemanticClassificationSnapshot,
    assembly: RequirementAssemblySnapshot,
) -> RequirementViewSnapshot:
    """Project controlled unit-addressed assembly into a review-only View."""

    _validate_input_bindings(source_ir, tree, semantic_unit_plan)
    try:
        validate_semantic_classification_against_inputs(
            semantics,
            source_ir,
            tree,
            semantic_unit_plan=semantic_unit_plan,
        )
        validate_requirement_assembly_against_inputs(
            assembly,
            source_ir,
            tree,
            semantics,
        )
    except (SemanticClassificationRejected, RequirementAssemblyRejected) as exc:
        raise ControlledSemanticUnitDownstreamRejected(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_PROJECTION,
            "Controlled projection inputs are not mutually bound.",
        ) from exc

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    annotations_by_unit = {
        item.semantic_unit_id: item for item in semantics.annotations
    }
    included_items = tuple(
        item
        for item in assembly.items
        if item.status is RequirementAssemblyStatus.CANDIDATE and item.include_in_view
    )
    item_by_unit = {item.semantic_unit_id: item for item in included_items}
    snapshot_id = stable_requirement_view_snapshot_id(
        structure_snapshot_id=tree.structure_snapshot_id,
        semantic_snapshot_id=semantics.semantic_snapshot_id,
        semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
        projector_id=CONTROLLED_SEMANTIC_PROJECTOR_ID,
        projector_version=CONTROLLED_SEMANTIC_PROJECTOR_VERSION,
        projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
        requirement_set_id=None,
    )
    annotation_ids_by_unit = {
        item.semantic_unit_id: tuple(
            annotations_by_unit[unit_id].semantic_annotation_id
            for unit_id in item.source_semantic_unit_ids
        )
        for item in included_items
    }
    view_id_by_unit = {
        item.semantic_unit_id: stable_requirement_view_node_id(
            requirement_view_snapshot_id=snapshot_id,
            semantic_unit_id=item.semantic_unit_id,
            source_semantic_unit_ids=item.source_semantic_unit_ids,
            role=item.role,
            semantic_annotation_ids=annotation_ids_by_unit[item.semantic_unit_id],
        )
        for item in included_items
        if item.role is not None
    }
    children_by_unit: dict[str, list[RequirementAssemblyItem]] = {}
    for item in included_items:
        if item.parent_semantic_unit_id is not None:
            children_by_unit.setdefault(item.parent_semantic_unit_id, []).append(item)

    top_primary_by_unit = {
        item.semantic_unit_id: _top_primary_unit(item, item_by_unit)
        for item in included_items
    }
    full_context_by_primary: dict[str, tuple[StructureSourceRef, ...]] = {}
    for primary_id in set(top_primary_by_unit.values()):
        branch_items = tuple(
            item
            for item in included_items
            if top_primary_by_unit[item.semantic_unit_id] == primary_id
        )
        full_context_by_primary[primary_id] = _dedupe(
            tuple(
                source_ref
                for item in branch_items
                for owner_id in item.source_owner_structure_node_ids
                for source_ref in nodes_by_id[owner_id].source_refs
            )
        )

    projected_nodes: list[RequirementViewNode] = []
    for item in included_items:
        if item.role is None:
            _reject(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_PROJECTION,
                "Included controlled assembly items require a role.",
            )
        source_annotations = tuple(
            annotations_by_unit[unit_id] for unit_id in item.source_semantic_unit_ids
        )
        primary_annotation = source_annotations[0]
        has_determinable_child = any(
            child.role
            in {RequirementProjectionRole.CHILD, RequirementProjectionRole.PARAMETER}
            for child in children_by_unit.get(item.semantic_unit_id, ())
        )
        decision_mode = (
            RequirementDecisionMode.NONE
            if item.role
            in {RequirementProjectionRole.CONTENT, RequirementProjectionRole.REFERENCE}
            else RequirementDecisionMode.ROLLUP
            if has_determinable_child
            else RequirementDecisionMode.DIRECT
        )
        source_spans = tuple(
            span
            for annotation in source_annotations
            for span in annotation.grounding_spans
        )
        projected_nodes.append(
            RequirementViewNode(
                requirement_view_node_id=view_id_by_unit[item.semantic_unit_id],
                role=item.role,
                parent_requirement_view_node_id=(
                    view_id_by_unit[item.parent_semantic_unit_id]
                    if item.parent_semantic_unit_id is not None
                    else None
                ),
                decision_mode=decision_mode,
                semantic_class=primary_annotation.semantic_class,
                requirement_subtype=primary_annotation.requirement_subtype,
                semantic_unit_id=item.semantic_unit_id,
                source_semantic_unit_ids=item.source_semantic_unit_ids,
                owner_structure_node_id=item.owner_structure_node_id,
                semantic_annotation_ids=annotation_ids_by_unit[item.semantic_unit_id],
                ancestor_structure_node_ids=_ancestor_ids(
                    item.owner_structure_node_id,
                    nodes_by_id,
                ),
                source_span_ids=tuple(span.grounding_span_id for span in source_spans),
                source_refs=_dedupe(tuple(span.source_ref for span in source_spans)),
                full_context_source_refs=full_context_by_primary[
                    top_primary_by_unit[item.semantic_unit_id]
                ],
                relation_ids=(),
                source_order=item.source_order,
                unit_order=item.unit_order,
            )
        )
    try:
        return RequirementViewSnapshot(
            requirement_view_snapshot_id=snapshot_id,
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=tree.structure_snapshot_id,
            semantic_snapshot_id=semantics.semantic_snapshot_id,
            semantic_unit_plan_id=semantic_unit_plan.semantic_unit_plan_id,
            projector_id=CONTROLLED_SEMANTIC_PROJECTOR_ID,
            projector_version=CONTROLLED_SEMANTIC_PROJECTOR_VERSION,
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
    except (TypeError, ValueError) as exc:
        raise ControlledSemanticUnitDownstreamRejected(
            ControlledSemanticUnitDownstreamRejectionCode.INVALID_PROJECTION,
            "Controlled RequirementView violated its unit-order contract.",
        ) from exc


def build_controlled_semantic_unit_downstream(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    execution: ControlledSemanticUnitDecisionExecution,
    directives: tuple[ControlledSemanticUnitAssemblyDirective, ...],
) -> ControlledSemanticUnitDownstreamExecution:
    """Run the inactive Semantic → Assembly → View multi-unit chain."""

    semantic_unit_plan = execution.plan.semantic_unit_plan
    semantics = build_controlled_semantic_classification(source_ir, tree, execution)
    assembly = assemble_controlled_semantic_units(
        source_ir,
        tree,
        semantic_unit_plan,
        semantics,
        directives,
    )
    requirement_view = project_controlled_semantic_units(
        source_ir,
        tree,
        semantic_unit_plan,
        semantics,
        assembly,
    )
    return ControlledSemanticUnitDownstreamExecution(
        semantic_unit_plan=semantic_unit_plan,
        semantics=semantics,
        assembly=assembly,
        requirement_view=requirement_view,
    )


def _validate_input_bindings(
    source_ir: SourceIRSnapshot,
    tree: StructureTreeSnapshot,
    semantic_unit_plan: SemanticUnitPlan,
) -> None:
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    if not isinstance(tree, StructureTreeSnapshot):
        raise TypeError("tree must be a StructureTreeSnapshot")
    if not isinstance(semantic_unit_plan, SemanticUnitPlan):
        raise TypeError("semantic_unit_plan must be a SemanticUnitPlan")
    if (
        tree.source_ir_id != source_ir.source_ir_id
        or semantic_unit_plan.source_ir_id != source_ir.source_ir_id
        or semantic_unit_plan.structure_snapshot_id != tree.structure_snapshot_id
    ):
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INPUT_MISMATCH,
            "Controlled downstream inputs do not share one source/structure identity.",
        )
    if semantic_unit_plan.unitizer_version != CONTROLLED_EXPLICIT_LIST_UNITIZER_VERSION:
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INPUT_MISMATCH,
            "Controlled downstream only accepts the controlled explicit-list unitizer.",
        )
    owner_ids = tuple(item.owner_structure_node_id for item in semantic_unit_plan.units)
    if len(owner_ids) == len(set(owner_ids)):
        _reject(
            ControlledSemanticUnitDownstreamRejectionCode.INPUT_MISMATCH,
            "Controlled downstream requires at least one multi-unit owner.",
        )


def _top_primary_unit(
    item: RequirementAssemblyItem,
    item_by_unit: dict[str, RequirementAssemblyItem],
) -> str:
    current = item
    visited: set[str] = set()
    while current.parent_semantic_unit_id is not None:
        if current.semantic_unit_id in visited:
            _reject(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_PROJECTION,
                "Controlled assembly parentage contains a cycle.",
            )
        visited.add(current.semantic_unit_id)
        try:
            current = item_by_unit[current.parent_semantic_unit_id]
        except KeyError as exc:
            raise ControlledSemanticUnitDownstreamRejected(
                ControlledSemanticUnitDownstreamRejectionCode.INVALID_PROJECTION,
                "Controlled assembly parent is absent from the projected View.",
            ) from exc
    return current.semantic_unit_id


def _ancestor_ids(
    owner_id: str,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    result: list[str] = []
    parent_id = nodes_by_id[owner_id].parent_structure_node_id
    while parent_id is not None:
        result.append(parent_id)
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    result.reverse()
    return tuple(result)


def _stable_runtime_id(prefix: str, *parts: object) -> str:
    payload = json.dumps(
        parts,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}-{digest}"


def _dedupe(values: tuple[StructureSourceRef, ...]) -> tuple[StructureSourceRef, ...]:
    return tuple(dict.fromkeys(values))


def _reject(
    code: ControlledSemanticUnitDownstreamRejectionCode,
    message: str,
) -> None:
    raise ControlledSemanticUnitDownstreamRejected(code, message)


__all__ = [
    "CONTROLLED_SEMANTIC_ASSEMBLER_ID",
    "CONTROLLED_SEMANTIC_ASSEMBLER_VERSION",
    "CONTROLLED_SEMANTIC_CLASSIFIER_ID",
    "CONTROLLED_SEMANTIC_CLASSIFIER_VERSION",
    "CONTROLLED_SEMANTIC_PROJECTOR_ID",
    "CONTROLLED_SEMANTIC_PROJECTOR_VERSION",
    "ControlledSemanticUnitAssemblyDirective",
    "ControlledSemanticUnitDownstreamExecution",
    "ControlledSemanticUnitDownstreamRejected",
    "ControlledSemanticUnitDownstreamRejectionCode",
    "assemble_controlled_semantic_units",
    "build_controlled_semantic_classification",
    "build_controlled_semantic_unit_downstream",
    "project_controlled_semantic_units",
]
