from __future__ import annotations

from enum import Enum

from reqpilotx_server.application.extraction.structure_snapshot import (
    StructureSnapshot,
    require_structure_snapshot,
    structure_snapshot_id,
)
from reqpilotx_server.domain.extraction.requirement_assembly import RequirementAssemblySnapshot
from reqpilotx_server.domain.extraction.requirement_view import RequirementProjectionRole
from reqpilotx_server.domain.extraction.semantics import (
    SemanticClass,
    SemanticClassificationSnapshot,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import StructureNode


class RequirementAssemblyRejectionCode(str, Enum):
    INPUT_MISMATCH = "input_mismatch"
    INVALID_SCOPE = "invalid_scope"
    INVALID_ROLE = "invalid_role"
    INVALID_PARENT = "invalid_parent"


class RequirementAssemblyRejected(RuntimeError):
    def __init__(self, code: RequirementAssemblyRejectionCode, message: str) -> None:
        self.code = code
        super().__init__(message)


def validate_requirement_assembly_against_inputs(
    assembly: RequirementAssemblySnapshot,
    source_ir: SourceIRSnapshot,
    tree: StructureSnapshot,
    semantics: SemanticClassificationSnapshot,
) -> None:
    """Fail closed unless a model assembly is a legal structural projection."""

    if not isinstance(assembly, RequirementAssemblySnapshot):
        raise TypeError("assembly must be a RequirementAssemblySnapshot")
    if not isinstance(source_ir, SourceIRSnapshot):
        raise TypeError("source_ir must be a SourceIRSnapshot")
    require_structure_snapshot(tree)
    if not isinstance(semantics, SemanticClassificationSnapshot):
        raise TypeError("semantics must be a SemanticClassificationSnapshot")
    if (
        assembly.source_ir_id != source_ir.source_ir_id
        or assembly.source_ir_id != tree.source_ir_id
        or assembly.structure_snapshot_id != structure_snapshot_id(tree)
        or assembly.semantic_snapshot_id != semantics.semantic_snapshot_id
        or assembly.semantic_unit_plan_id != semantics.semantic_unit_plan_id
    ):
        raise RequirementAssemblyRejected(
            RequirementAssemblyRejectionCode.INPUT_MISMATCH,
            "Requirement assembly does not belong to the supplied snapshots.",
        )

    nodes_by_id = {node.structure_node_id: node for node in tree.nodes}
    annotations_by_unit = {
        item.semantic_unit_id: item for item in semantics.annotations
    }
    items_by_unit = {item.semantic_unit_id: item for item in assembly.items}
    flattened_unit_ids = tuple(
        unit_id for item in assembly.items for unit_id in item.source_semantic_unit_ids
    )
    flattened_owner_ids = tuple(
        owner_id
        for item in assembly.items
        for owner_id in item.source_owner_structure_node_ids
    )
    if (
        assembly.scope_semantic_unit_ids != semantics.scope_semantic_unit_ids
        or assembly.scope_owner_structure_node_ids
        != semantics.scope_owner_structure_node_ids
        or len(flattened_unit_ids) != len(set(flattened_unit_ids))
        or set(flattened_unit_ids) != set(semantics.scope_semantic_unit_ids)
        or len(flattened_owner_ids) != len(semantics.scope_owner_structure_node_ids)
    ):
        raise RequirementAssemblyRejected(
            RequirementAssemblyRejectionCode.INVALID_SCOPE,
            "Requirement assembly must exactly cover the semantic-unit scope.",
        )
    if any(
        owner_id not in nodes_by_id
        for owner_id in assembly.scope_owner_structure_node_ids
    ):
        raise RequirementAssemblyRejected(
            RequirementAssemblyRejectionCode.INVALID_SCOPE,
            "Requirement assembly contains an owner outside the StructureTree.",
        )
    if any(
        unit_id not in annotations_by_unit
        for unit_id in assembly.scope_semantic_unit_ids
    ):
        raise RequirementAssemblyRejected(
            RequirementAssemblyRejectionCode.INVALID_SCOPE,
            "Requirement assembly contains a unit without semantic grounding.",
        )
    unit_order_by_id = {
        unit_id: unit_order
        for unit_order, unit_id in enumerate(semantics.scope_semantic_unit_ids)
    }
    for item in assembly.items:
        annotation = annotations_by_unit[item.semantic_unit_id]
        if (
            item.unit_order != unit_order_by_id[item.semantic_unit_id]
            or item.owner_structure_node_id != annotation.owner_structure_node_id
            or item.source_order
            != nodes_by_id[item.owner_structure_node_id].source_order
        ):
            raise RequirementAssemblyRejected(
                RequirementAssemblyRejectionCode.INVALID_SCOPE,
                "Requirement assembly unit ownership or order does not match semantics.",
            )
        if any(
            annotations_by_unit[unit_id].owner_structure_node_id != owner_id
            for unit_id, owner_id in zip(
                item.source_semantic_unit_ids,
                item.source_owner_structure_node_ids,
                strict=True,
            )
        ):
            raise RequirementAssemblyRejected(
                RequirementAssemblyRejectionCode.INVALID_SCOPE,
                "Requirement assembly source lineage does not match semantics.",
            )
        lineage_orders = tuple(
            unit_order_by_id[unit_id] for unit_id in item.source_semantic_unit_ids
        )
        if lineage_orders != tuple(sorted(lineage_orders)):
            raise RequirementAssemblyRejected(
                RequirementAssemblyRejectionCode.INVALID_SCOPE,
                "Requirement assembly source lineage does not preserve source order.",
            )
        if not item.include_in_view:
            continue
        if annotation.is_continuation_fragment:
            raise RequirementAssemblyRejected(
                RequirementAssemblyRejectionCode.INVALID_ROLE,
                "Continuation fragments cannot become review items.",
            )
        if annotation.semantic_class is SemanticClass.REQUIREMENT:
            if item.role not in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.CONTENT,
                RequirementProjectionRole.PARAMETER,
            }:
                raise RequirementAssemblyRejected(
                    RequirementAssemblyRejectionCode.INVALID_ROLE,
                    "Requirement semantics received an illegal projection role.",
                )
        elif item.role not in {
            RequirementProjectionRole.CONTENT,
            RequirementProjectionRole.REFERENCE,
        }:
            raise RequirementAssemblyRejected(
                RequirementAssemblyRejectionCode.INVALID_ROLE,
                "Non-requirement semantics received an illegal projection role.",
            )

        parent_unit_id = item.parent_semantic_unit_id
        if item.role is RequirementProjectionRole.PRIMARY:
            continue
        if parent_unit_id is None or parent_unit_id not in items_by_unit:
            _reject_parent()
        parent = items_by_unit[parent_unit_id]
        parent_annotation = annotations_by_unit[parent_unit_id]
        if (
            item.parent_owner_structure_node_id != parent.owner_structure_node_id
            or not parent.include_in_view
            or parent.role
            not in {
                RequirementProjectionRole.PRIMARY,
                RequirementProjectionRole.CHILD,
                RequirementProjectionRole.PARAMETER,
            }
            or parent_annotation.semantic_class is not SemanticClass.REQUIREMENT
            or parent.source_order > item.source_order
            or parent.unit_order >= item.unit_order
        ):
            _reject_parent()
        target_ancestors = _ancestor_ids(
            item.owner_structure_node_id,
            nodes_by_id,
        )
        reference_shared_container = (
            item.role is RequirementProjectionRole.REFERENCE
            and nodes_by_id[parent.owner_structure_node_id].parent_structure_node_id
            == nodes_by_id[item.owner_structure_node_id].parent_structure_node_id
        )
        shares_semantic_owner = (
            parent.owner_structure_node_id == item.owner_structure_node_id
            and parent.semantic_unit_id != item.semantic_unit_id
        )
        if (
            parent.owner_structure_node_id not in target_ancestors
            and not shares_semantic_owner
            and not reference_shared_container
        ):
            _reject_parent()

    for unit_id in tuple(item.semantic_unit_id for item in assembly.items):
        visited: set[str] = set()
        current = unit_id
        while True:
            item = items_by_unit[current]
            parent_unit_id = item.parent_semantic_unit_id
            if parent_unit_id is None:
                break
            if parent_unit_id in visited:
                _reject_parent()
            visited.add(parent_unit_id)
            current = parent_unit_id


def _ancestor_ids(
    target_id: str,
    nodes_by_id: dict[str, StructureNode],
) -> tuple[str, ...]:
    result: list[str] = []
    node = nodes_by_id[target_id]
    parent_id = node.parent_structure_node_id
    while parent_id is not None:
        result.append(parent_id)
        parent_id = nodes_by_id[parent_id].parent_structure_node_id
    return tuple(result)


def _reject_parent() -> None:
    raise RequirementAssemblyRejected(
        RequirementAssemblyRejectionCode.INVALID_PARENT,
        "Requirement assembly parentage is not a preceding structural requirement ancestry.",
    )


__all__ = [
    "RequirementAssemblyRejected",
    "RequirementAssemblyRejectionCode",
    "validate_requirement_assembly_against_inputs",
]
