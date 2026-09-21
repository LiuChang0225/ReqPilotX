from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from reqpilotx_server.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
)
from reqpilotx_server.domain.extraction.semantic_units import (
    SemanticUnit,
    SemanticUnitKind,
    SemanticUnitPlan,
    stable_semantic_unit_id,
    stable_semantic_unit_plan_id,
)
from reqpilotx_server.domain.extraction.semantics import (
    SemanticClassificationSnapshot,
    SemanticGroundingSpan,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import StructureNode

if TYPE_CHECKING:
    from reqpilotx_server.application.extraction.classify_content import SemanticTextResolver


DEFAULT_SEMANTIC_UNITIZER_VERSION = "provider-neutral-full-span/v1"


class SemanticUnitPlanRejectionCode(str, Enum):
    INPUT_MISMATCH = "semantic_unit_plan_input_mismatch"
    INVALID_SCOPE = "semantic_unit_plan_invalid_scope"
    INVALID_PARENT = "semantic_unit_plan_invalid_parent"
    INVALID_GROUNDING = "semantic_unit_plan_invalid_grounding"


class SemanticUnitPlanRejected(RuntimeError):
    """Fail-closed unit-plan rejection without source text in the message."""

    def __init__(
        self,
        code: SemanticUnitPlanRejectionCode,
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


def build_semantic_unit_plan(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    grounding_scaffold: SemanticClassificationSnapshot,
    *,
    unitizer_version: str = DEFAULT_SEMANTIC_UNITIZER_VERSION,
    text_resolver: SemanticTextResolver | None = None,
) -> SemanticUnitPlan:
    """Build the provider-neutral 1:1 full-span semantic unit plan.

    ``grounding_scaffold`` is the existing verified model-text view. Semantic
    classes from that scaffold do not participate in unit identity: the plan
    uses only input bindings, Structure ownership and owner-local source spans.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(grounding_scaffold, SemanticClassificationSnapshot):
        raise TypeError("grounding_scaffold must be a SemanticClassificationSnapshot")
    if not isinstance(unitizer_version, str) or not unitizer_version.strip():
        raise ValueError("unitizer_version must be a non-blank string")
    _validate_grounding_scaffold(
        grounding_scaffold,
        source_ir,
        tree,
        text_resolver=text_resolver,
    )

    owner_spans_by_id = {
        annotation.owner_structure_node_id: _owner_source_spans(
            annotation.owner_structure_node_id,
            annotation.grounding_spans,
            nodes_by_id={item.structure_node_id: item for item in tree.nodes},
        )
        for annotation in grounding_scaffold.annotations
    }
    plan = build_semantic_unit_plan_from_owner_spans(
        source_ir,
        tree,
        scope_owner_structure_node_ids=(
            grounding_scaffold.scope_owner_structure_node_ids
        ),
        owner_source_spans_by_id=owner_spans_by_id,
        unitizer_version=unitizer_version,
    )
    _validate_plan_against_verified_scaffold(
        plan,
        source_ir,
        tree,
        grounding_scaffold,
    )
    return plan


def build_semantic_unit_plan_from_owner_spans(
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    *,
    scope_owner_structure_node_ids: tuple[str, ...],
    owner_source_spans_by_id: dict[str, tuple[SemanticGroundingSpan, ...]],
    unitizer_version: str = DEFAULT_SEMANTIC_UNITIZER_VERSION,
) -> SemanticUnitPlan:
    """Build the 1:1 plan from already resolved owner-local source spans.

    This lower-level constructor lets semantic classification establish unit
    identity before the classification snapshot itself exists.
    """

    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(scope_owner_structure_node_ids, tuple):
        raise TypeError("scope_owner_structure_node_ids must be a tuple")
    if not isinstance(owner_source_spans_by_id, dict):
        raise TypeError("owner_source_spans_by_id must be a dict")
    if not isinstance(unitizer_version, str) or not unitizer_version.strip():
        raise ValueError("unitizer_version must be a non-blank string")

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    if (
        not scope_owner_structure_node_ids
        or len(scope_owner_structure_node_ids)
        != len(set(scope_owner_structure_node_ids))
        or set(owner_source_spans_by_id) != set(scope_owner_structure_node_ids)
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_SCOPE,
            "Semantic unit owner scope is invalid.",
        )
    expected_scope = tuple(
        sorted(
            scope_owner_structure_node_ids,
            key=lambda owner_id: (
                nodes_by_id[owner_id].source_order,
                owner_id,
            )
            if owner_id in nodes_by_id
            else (-1, owner_id),
        )
    )
    if (
        any(owner_id not in nodes_by_id for owner_id in scope_owner_structure_node_ids)
        or expected_scope != scope_owner_structure_node_ids
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_SCOPE,
            "Semantic unit owners must preserve StructureTree source order.",
        )
    normalized_spans_by_id = {
        owner_id: _owner_source_spans(
            owner_id,
            owner_source_spans_by_id[owner_id],
            nodes_by_id=nodes_by_id,
        )
        for owner_id in scope_owner_structure_node_ids
    }
    unit_ids_by_owner = {
        owner_id: stable_semantic_unit_id(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=structure_snapshot_id(tree),
            owner_structure_node_id=owner_id,
            unitizer_version=unitizer_version,
            ordered_source_spans=normalized_spans_by_id[owner_id],
        )
        for owner_id in scope_owner_structure_node_ids
    }
    units = tuple(
        SemanticUnit(
            semantic_unit_id=unit_ids_by_owner[owner_id],
            owner_structure_node_id=owner_id,
            parent_owner_structure_node_id=nodes_by_id[
                owner_id
            ].parent_structure_node_id,
            parent_semantic_unit_id=unit_ids_by_owner.get(
                nodes_by_id[owner_id].parent_structure_node_id or ""
            ),
            source_order=nodes_by_id[owner_id].source_order,
            unit_order=unit_order,
            owner_unit_index=0,
            unit_kind=SemanticUnitKind.OWNER_FULL_SPAN,
            source_spans=normalized_spans_by_id[owner_id],
        )
        for unit_order, owner_id in enumerate(scope_owner_structure_node_ids)
    )
    plan = SemanticUnitPlan(
        semantic_unit_plan_id=stable_semantic_unit_plan_id(
            source_ir_id=source_ir.source_ir_id,
            structure_snapshot_id=structure_snapshot_id(tree),
            unitizer_version=unitizer_version,
            scope_owner_structure_node_ids=scope_owner_structure_node_ids,
            semantic_unit_ids=tuple(item.semantic_unit_id for item in units),
        ),
        source_ir_id=source_ir.source_ir_id,
        structure_snapshot_id=structure_snapshot_id(tree),
        unitizer_version=unitizer_version,
        scope_owner_structure_node_ids=scope_owner_structure_node_ids,
        units=units,
    )
    return plan


def validate_semantic_unit_plan_against_inputs(
    plan: SemanticUnitPlan,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    grounding_scaffold: SemanticClassificationSnapshot,
    *,
    text_resolver: SemanticTextResolver | None = None,
) -> None:
    """Verify exact owner, order, parent and Unicode-span coverage."""

    if not isinstance(plan, SemanticUnitPlan):
        raise TypeError("plan must be a SemanticUnitPlan")
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(grounding_scaffold, SemanticClassificationSnapshot):
        raise TypeError("grounding_scaffold must be a SemanticClassificationSnapshot")
    _validate_grounding_scaffold(
        grounding_scaffold,
        source_ir,
        tree,
        text_resolver=text_resolver,
    )
    _validate_plan_against_verified_scaffold(
        plan,
        source_ir,
        tree,
        grounding_scaffold,
    )


def _validate_plan_against_verified_scaffold(
    plan: SemanticUnitPlan,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    grounding_scaffold: SemanticClassificationSnapshot,
) -> None:
    if (
        plan.source_ir_id != source_ir.source_ir_id
        or plan.structure_snapshot_id != structure_snapshot_id(tree)
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INPUT_MISMATCH,
            "Semantic unit plan is not bound to the supplied inputs.",
        )
    if (
        plan.scope_owner_structure_node_ids
        != grounding_scaffold.scope_owner_structure_node_ids
        or plan.unitizer_version != grounding_scaffold.unitizer_version
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_SCOPE,
            "Semantic unit owners do not exactly cover the grounding scope.",
        )

    nodes_by_id = {item.structure_node_id: item for item in tree.nodes}
    units_by_owner = {item.owner_structure_node_id: item for item in plan.units}
    for annotation, unit in zip(
        grounding_scaffold.annotations,
        plan.units,
        strict=True,
    ):
        owner_id = annotation.owner_structure_node_id
        owner = nodes_by_id.get(owner_id)
        if (
            owner is None
            or unit.owner_structure_node_id != owner_id
            or unit.source_order != owner.source_order
            or unit.owner_unit_index != 0
            or unit.unit_kind is not SemanticUnitKind.OWNER_FULL_SPAN
        ):
            raise SemanticUnitPlanRejected(
                SemanticUnitPlanRejectionCode.INVALID_SCOPE,
                "Semantic unit ownership or source order does not match StructureTree.",
            )
        expected_parent_owner_id = owner.parent_structure_node_id
        expected_parent = units_by_owner.get(expected_parent_owner_id or "")
        expected_parent_unit_id = (
            expected_parent.semantic_unit_id if expected_parent is not None else None
        )
        if (
            unit.parent_owner_structure_node_id != expected_parent_owner_id
            or unit.parent_semantic_unit_id != expected_parent_unit_id
        ):
            raise SemanticUnitPlanRejected(
                SemanticUnitPlanRejectionCode.INVALID_PARENT,
                "Semantic unit parent does not match its Structure owner parent.",
            )
        expected_spans = _owner_source_spans(
            owner_id,
            annotation.grounding_spans,
            nodes_by_id=nodes_by_id,
        )
        if unit.source_spans != expected_spans:
            raise SemanticUnitPlanRejected(
                SemanticUnitPlanRejectionCode.INVALID_GROUNDING,
                "Semantic unit spans must exactly cover the owner source view.",
            )
        if unit.semantic_unit_id != annotation.semantic_unit_id:
            raise SemanticUnitPlanRejected(
                SemanticUnitPlanRejectionCode.INVALID_SCOPE,
                "Semantic unit identity does not match the verified grounding scope.",
            )
    if (
        plan.semantic_unit_ids != grounding_scaffold.scope_semantic_unit_ids
        or plan.semantic_unit_plan_id != grounding_scaffold.semantic_unit_plan_id
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_SCOPE,
            "Semantic unit identity does not exactly cover the grounding scope.",
        )


def _validate_grounding_scaffold(
    grounding_scaffold: SemanticClassificationSnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    *,
    text_resolver: SemanticTextResolver | None,
) -> None:
    from reqpilotx_server.application.extraction.classify_content import (
        SemanticClassificationRejected,
        SemanticClassificationRejectionCode,
        validate_semantic_classification_against_inputs,
    )

    try:
        validate_semantic_classification_against_inputs(
            grounding_scaffold,
            source_ir,
            tree,
            text_resolver=text_resolver,
        )
    except SemanticClassificationRejected as exc:
        code = {
            SemanticClassificationRejectionCode.INPUT_MISMATCH: (
                SemanticUnitPlanRejectionCode.INPUT_MISMATCH
            ),
            SemanticClassificationRejectionCode.INVALID_SCOPE: (
                SemanticUnitPlanRejectionCode.INVALID_SCOPE
            ),
            SemanticClassificationRejectionCode.INVALID_GROUNDING: (
                SemanticUnitPlanRejectionCode.INVALID_GROUNDING
            ),
        }[exc.code]
        raise SemanticUnitPlanRejected(
            code,
            "Semantic unit grounding scaffold is invalid.",
        ) from exc


def _owner_source_spans(
    owner_structure_node_id: str,
    grounding_spans: tuple[SemanticGroundingSpan, ...],
    *,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[SemanticGroundingSpan, ...]:
    owner = nodes_by_id.get(owner_structure_node_id)
    if owner is None:
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_SCOPE,
            "Semantic unit owner is absent from StructureTree.",
        )
    semantic_owner_refs = owner.effective_semantic_source_refs
    owner_refs = set(semantic_owner_refs)
    spans = tuple(item for item in grounding_spans if item.source_ref in owner_refs)
    observed_refs = tuple(item.source_ref for item in spans)
    if (
        len(spans) != len(semantic_owner_refs)
        or len(observed_refs) != len(set(observed_refs))
        or set(observed_refs) != owner_refs
    ):
        raise SemanticUnitPlanRejected(
            SemanticUnitPlanRejectionCode.INVALID_GROUNDING,
            "Semantic unit spans do not exactly cover owner source references.",
        )
    return spans


__all__ = [
    "DEFAULT_SEMANTIC_UNITIZER_VERSION",
    "SemanticUnitPlanRejected",
    "SemanticUnitPlanRejectionCode",
    "build_semantic_unit_plan",
    "build_semantic_unit_plan_from_owner_spans",
    "validate_semantic_unit_plan_against_inputs",
]
