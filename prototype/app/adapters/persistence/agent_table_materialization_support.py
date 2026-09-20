from __future__ import annotations

import re

from app.application.extraction.agent_table_boundaries import (
    AgentTableProjectionMaterializationV1,
)
from app.application.extraction.agent_logical_table_scope import (
    AgentLogicalTableScopeV1,
)
from app.domain.extraction.table_records import TableRecordProjectionV1
from app.ports.agent_table_materialization_repository import (
    AgentTableMaterializationConflict,
)


_COMMAND_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")


def validated_materialization_scope(
    *,
    project_id: str,
    document_id: str,
    base_projection: TableRecordProjectionV1,
    logical_table_scope: AgentLogicalTableScopeV1,
    materialization: AgentTableProjectionMaterializationV1,
) -> tuple[str, ...]:
    if not str(project_id or "").strip() or not str(document_id or "").strip():
        raise ValueError("project_id and document_id must not be blank")
    fact = base_projection.table_fact
    if (
        logical_table_scope.source_ir_id != fact.source_ir_id
        or logical_table_scope.source_sha256 != fact.source_sha256
        or logical_table_scope.source_ir_canonical_sha256
        != fact.source_ir_canonical_sha256
        or logical_table_scope.logical_object_id
        != materialization.agent_logical_table_id
        or logical_table_scope.scope_sha256
        != materialization.agent_logical_table_scope_sha256
        or logical_table_scope.d1_table_map_artifact.artifact_id
        != materialization.d1_table_map_artifact_id
        or logical_table_scope.d1_table_map_artifact.artifact_sha256
        != materialization.d1_table_map_artifact_sha256
        or logical_table_scope.physical_table_ids != fact.table_ids
        or materialization.source_sha256 != fact.source_sha256
        or materialization.materializer_structure_snapshot_id
        != fact.structure_snapshot_id
        or materialization.materializer_logical_table_id
        != fact.logical_table_id
        or materialization.table_fact_binding_sha256
        != fact.fact_binding_sha256
        or materialization.input_projection_id != base_projection.projection_id
        or materialization.input_projection_revision
        != base_projection.projection_revision
        or materialization.projection.table_fact != fact
    ):
        raise AgentTableMaterializationConflict(
            "Agent table materialization differs from its base projection lineage"
        )
    return (
        project_id,
        document_id,
        fact.source_sha256,
        fact.structure_snapshot_id,
        fact.logical_table_id,
    )


def same_projection_identity(
    left: TableRecordProjectionV1,
    right: TableRecordProjectionV1,
) -> bool:
    return (
        left.projection_id == right.projection_id
        and left.projection_revision == right.projection_revision
        and left.projection_sha256 == right.projection_sha256
        and left.table_fact == right.table_fact
    )


def validate_materialization_command_id(value: str) -> None:
    if _COMMAND_ID_PATTERN.fullmatch(str(value or "")) is None:
        raise ValueError("command_id has an invalid shape")


__all__ = [
    "same_projection_identity",
    "validate_materialization_command_id",
    "validated_materialization_scope",
]
