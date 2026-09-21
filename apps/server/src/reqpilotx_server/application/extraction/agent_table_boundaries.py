from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reqpilotx_server.application.extraction.table_record_projection import (
    ManualRowGroup,
    StructureSnapshot,
    build_manual_row_group_projection,
    manual_row_group_eligible_coordinates,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from reqpilotx_server.domain.extraction.table_records import (
    TableRecordProjectionDecisionOrigin,
    TableRecordProjectionMode,
    TableRecordProjectionV1,
)


AGENT_TABLE_PARENT_BOUNDARY_SCHEMA_VERSION = "agent-table-parent-boundary/v1"
AGENT_TABLE_PROJECTION_MATERIALIZATION_SCHEMA_VERSION = (
    "agent-table-projection-materialization/v1"
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentTableRowBindingV1(_FrozenModel):
    table_id: str = Field(min_length=1, max_length=160)
    row_index: int = Field(ge=0)
    cell_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not item.strip() for item in value):
            raise ValueError("agent table row cell IDs must be unique and non-empty")
        return value


class AgentTableParentBoundaryV1(_FrozenModel):
    schema_version: Literal["agent-table-parent-boundary/v1"] = (
        AGENT_TABLE_PARENT_BOUNDARY_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_artifact_id: str = Field(min_length=1, max_length=200)
    boundary_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_table_id: str = Field(min_length=1, max_length=200)
    parent_provision_id: str = Field(min_length=1, max_length=200)
    source_order: int = Field(ge=0)
    row_bindings: tuple[AgentTableRowBindingV1, ...] = Field(min_length=1)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_boundary(self) -> AgentTableParentBoundaryV1:
        coordinates = tuple(
            (binding.table_id, binding.row_index) for binding in self.row_bindings
        )
        if len(coordinates) != len(set(coordinates)):
            raise ValueError("agent table parent cannot repeat a physical row")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("agent table parent source refs must be unique")
        row_cell_ids = {
            cell_id
            for binding in self.row_bindings
            for cell_id in binding.cell_ids
        }
        referenced_cell_ids = {
            ref.source_id
            for ref in self.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
        }
        if referenced_cell_ids != row_cell_ids:
            raise ValueError(
                "agent table parent source refs must bind exactly its row cells"
            )
        expected = stable_agent_table_parent_boundary_sha256(
            source_sha256=self.source_sha256,
            boundary_artifact_id=self.boundary_artifact_id,
            boundary_artifact_sha256=self.boundary_artifact_sha256,
            logical_table_id=self.logical_table_id,
            parent_provision_id=self.parent_provision_id,
            source_order=self.source_order,
            row_bindings=self.row_bindings,
            source_refs=self.source_refs,
        )
        if self.boundary_sha256 != expected:
            raise ValueError("agent table parent hash does not bind its evidence")
        return self


class AgentTableParentRecordBindingV1(_FrozenModel):
    """Typed lineage from one Agent-owned parent to one materialized record."""

    parent_provision_id: str = Field(min_length=1, max_length=200)
    source_order: int = Field(ge=0)
    boundary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    table_record_id: str = Field(pattern=r"^table-record-[0-9a-f]{32}$")
    table_record_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentTableProjectionMaterializationV1(_FrozenModel):
    """Source-bound result of applying Agent parents to the table engine.

    ``agent_logical_table_id`` is the Agent artifact's domain identity.
    ``materializer_logical_table_id`` and ``materializer_structure_snapshot_id``
    are implementation handles for the existing deterministic materializer;
    they do not become parent-boundary authority.
    """

    schema_version: Literal["agent-table-projection-materialization/v1"] = (
        AGENT_TABLE_PROJECTION_MATERIALIZATION_SCHEMA_VERSION
    )
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    boundary_artifact_id: str = Field(min_length=1, max_length=200)
    boundary_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_logical_table_id: str = Field(min_length=1, max_length=200)
    agent_logical_table_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materializer_logical_table_id: str = Field(min_length=1, max_length=200)
    materializer_structure_snapshot_id: str = Field(min_length=1, max_length=200)
    table_fact_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_projection_id: str = Field(
        pattern=r"^table-record-projection-[0-9a-f]{32}$"
    )
    input_projection_revision: int = Field(ge=0)
    projection: TableRecordProjectionV1
    parent_record_bindings: tuple[AgentTableParentRecordBindingV1, ...] = Field(
        min_length=1
    )
    materialization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_materialization(self) -> AgentTableProjectionMaterializationV1:
        fact = self.projection.table_fact
        if (
            fact.source_sha256 != self.source_sha256
            or fact.logical_table_id != self.materializer_logical_table_id
            or fact.structure_snapshot_id
            != self.materializer_structure_snapshot_id
            or fact.fact_binding_sha256 != self.table_fact_binding_sha256
        ):
            raise ValueError("Agent table materialization fact lineage is inconsistent")
        if self.projection.projection_revision <= self.input_projection_revision:
            raise ValueError(
                "Agent table materialization must advance the projection revision"
            )
        if (
            self.projection.mode is not TableRecordProjectionMode.RECORDS
            or self.projection.decision is None
            or self.projection.decision.origin
            is not TableRecordProjectionDecisionOrigin.AGENT
        ):
            raise ValueError(
                "Agent table materialization requires an AGENT record projection"
            )
        bindings = self.parent_record_bindings
        if tuple(item.source_order for item in bindings) != tuple(
            range(len(bindings))
        ):
            raise ValueError(
                "Agent table parent-record bindings must follow source order"
            )
        if len({item.parent_provision_id for item in bindings}) != len(bindings):
            raise ValueError("Agent table parent-record bindings must be unique")
        if len(bindings) != len(self.projection.records):
            raise ValueError("every Agent table parent must bind one materialized record")
        for binding, record in zip(bindings, self.projection.records, strict=True):
            if (
                binding.source_order != record.source_order
                or binding.table_record_id != record.record_id
                or binding.table_record_content_sha256 != record.content_sha256
            ):
                raise ValueError("Agent parent-record binding differs from projection")
        expected = stable_agent_table_projection_materialization_sha256(
            source_sha256=self.source_sha256,
            boundary_artifact_id=self.boundary_artifact_id,
            boundary_artifact_sha256=self.boundary_artifact_sha256,
            agent_logical_table_id=self.agent_logical_table_id,
            agent_logical_table_scope_sha256=(
                self.agent_logical_table_scope_sha256
            ),
            d1_table_map_artifact_id=self.d1_table_map_artifact_id,
            d1_table_map_artifact_sha256=self.d1_table_map_artifact_sha256,
            materializer_logical_table_id=self.materializer_logical_table_id,
            materializer_structure_snapshot_id=(
                self.materializer_structure_snapshot_id
            ),
            table_fact_binding_sha256=self.table_fact_binding_sha256,
            input_projection_id=self.input_projection_id,
            input_projection_revision=self.input_projection_revision,
            projection=self.projection,
            parent_record_bindings=self.parent_record_bindings,
        )
        if self.materialization_sha256 != expected:
            raise ValueError("Agent table materialization hash does not bind its result")
        return self


def build_agent_table_parent_boundary(
    *,
    source_sha256: str,
    boundary_artifact_id: str,
    boundary_artifact_sha256: str,
    logical_table_id: str,
    parent_provision_id: str,
    source_order: int,
    row_bindings: tuple[AgentTableRowBindingV1, ...],
    source_refs: tuple[StructureSourceRef, ...],
) -> AgentTableParentBoundaryV1:
    boundary_sha256 = stable_agent_table_parent_boundary_sha256(
        source_sha256=source_sha256,
        boundary_artifact_id=boundary_artifact_id,
        boundary_artifact_sha256=boundary_artifact_sha256,
        logical_table_id=logical_table_id,
        parent_provision_id=parent_provision_id,
        source_order=source_order,
        row_bindings=row_bindings,
        source_refs=source_refs,
    )
    return AgentTableParentBoundaryV1(
        source_sha256=source_sha256,
        boundary_artifact_id=boundary_artifact_id,
        boundary_artifact_sha256=boundary_artifact_sha256,
        logical_table_id=logical_table_id,
        parent_provision_id=parent_provision_id,
        source_order=source_order,
        row_bindings=row_bindings,
        source_refs=source_refs,
        boundary_sha256=boundary_sha256,
    )


def agent_boundaries_to_manual_row_groups(
    boundaries: tuple[AgentTableParentBoundaryV1, ...],
    *,
    expected_source_sha256: str,
    expected_boundary_artifact_id: str,
    expected_boundary_artifact_sha256: str,
    expected_logical_table_id: str,
    eligible_row_coordinates: tuple[tuple[str, int], ...],
) -> tuple[ManualRowGroup, ...]:
    """Adapt accepted Agent parents to the proven table record materializer.

    This function deliberately performs no automatic row inference. The Agent
    artifact owns parent boundaries; the legacy table engine receives only an
    exact, ordered partition to materialize headers, merged values, notes,
    images, evidence, and stable record identities.
    """

    if not boundaries:
        raise ValueError("agent table projection requires parent boundaries")
    if tuple(item.source_order for item in boundaries) != tuple(
        range(len(boundaries))
    ):
        raise ValueError("agent table parents must use contiguous source order")
    parent_ids = tuple(item.parent_provision_id for item in boundaries)
    if len(parent_ids) != len(set(parent_ids)):
        raise ValueError("agent table parent IDs must be unique")
    for item in boundaries:
        if (
            item.source_sha256 != expected_source_sha256
            or item.boundary_artifact_id != expected_boundary_artifact_id
            or item.boundary_artifact_sha256
            != expected_boundary_artifact_sha256
            or item.logical_table_id != expected_logical_table_id
        ):
            raise ValueError("agent table boundary lineage does not match the scope")
    groups = tuple(
        ManualRowGroup(
            row_coordinates=tuple(
                (binding.table_id, binding.row_index)
                for binding in item.row_bindings
            )
        )
        for item in boundaries
    )
    flattened = tuple(
        coordinate for group in groups for coordinate in group.row_coordinates
    )
    if flattened != eligible_row_coordinates:
        raise ValueError(
            "agent table parents must exactly partition eligible rows in source order"
        )
    return groups


def materialize_agent_table_parent_boundaries(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    base_projection: TableRecordProjectionV1,
    boundaries: tuple[AgentTableParentBoundaryV1, ...],
    expected_boundary_artifact_id: str,
    expected_boundary_artifact_sha256: str,
    expected_agent_logical_table_id: str,
    agent_logical_table_scope_sha256: str,
    d1_table_map_artifact_id: str,
    d1_table_map_artifact_sha256: str,
    projection_revision: int | None = None,
) -> AgentTableProjectionMaterializationV1:
    """Feed validated Agent parents into the existing table materializer.

    The Agent artifact owns the parent groups.  The current table projection
    contributes only a content-addressed immutable fact target and the legacy
    structure handle needed by the deterministic materializer.  Any mismatch
    between Agent evidence, current SourceIR facts, or the materializer's row
    domain fails closed instead of falling back to parser/resolver grouping.
    """

    if base_projection.table_fact.source_sha256 != source_ir.source_sha256:
        raise ValueError("base table projection does not bind the current source")
    materializer_handle = base_projection.table_fact.owner_structure_node_id
    current_fact_projection = build_manual_row_group_projection(
        source_ir,
        structure,
        table_structure_node_id=materializer_handle,
        row_groups=(),
        projection_revision=base_projection.projection_revision,
    )
    if current_fact_projection.table_fact != base_projection.table_fact:
        raise ValueError(
            "base table projection fact is stale for the current materializer context"
        )

    eligible_coordinates = manual_row_group_eligible_coordinates(
        source_ir,
        structure,
        table_structure_node_id=materializer_handle,
    )
    row_groups = agent_boundaries_to_manual_row_groups(
        boundaries,
        expected_source_sha256=source_ir.source_sha256,
        expected_boundary_artifact_id=expected_boundary_artifact_id,
        expected_boundary_artifact_sha256=expected_boundary_artifact_sha256,
        expected_logical_table_id=expected_agent_logical_table_id,
        eligible_row_coordinates=eligible_coordinates,
    )
    resolved_revision = (
        base_projection.projection_revision + 1
        if projection_revision is None
        else projection_revision
    )
    if resolved_revision <= base_projection.projection_revision:
        raise ValueError("Agent table projection revision must advance")
    projection = build_manual_row_group_projection(
        source_ir,
        structure,
        table_structure_node_id=materializer_handle,
        row_groups=row_groups,
        projection_revision=resolved_revision,
        decision_origin=TableRecordProjectionDecisionOrigin.AGENT,
        decision_evidence=(
            f"Agent boundary artifact {expected_boundary_artifact_id}",
            f"Agent boundary SHA-256 {expected_boundary_artifact_sha256}",
            f"Agent logical table {expected_agent_logical_table_id}",
        ),
    )
    if projection.table_fact != base_projection.table_fact:
        raise ValueError("Agent materialization changed the immutable table fact")

    parent_record_bindings: list[AgentTableParentRecordBindingV1] = []
    for boundary, record in zip(boundaries, projection.records, strict=True):
        expected_rows = tuple(
            (binding.table_id, binding.row_index, binding.cell_ids)
            for binding in boundary.row_bindings
        )
        materialized_rows = tuple(
            (binding.table_id, binding.row_index, binding.cell_ids)
            for binding in record.row_bindings
        )
        if expected_rows != materialized_rows:
            raise ValueError(
                "Agent table row cell bindings differ from immutable SourceIR facts"
            )
        if not set(boundary.source_refs).issubset(record.source_refs):
            raise ValueError(
                "Agent table parent evidence is not present in the materialized record"
            )
        parent_record_bindings.append(
            AgentTableParentRecordBindingV1(
                parent_provision_id=boundary.parent_provision_id,
                source_order=boundary.source_order,
                boundary_sha256=boundary.boundary_sha256,
                table_record_id=record.record_id,
                table_record_content_sha256=record.content_sha256,
            )
        )

    materialization_sha256 = stable_agent_table_projection_materialization_sha256(
        source_sha256=source_ir.source_sha256,
        boundary_artifact_id=expected_boundary_artifact_id,
        boundary_artifact_sha256=expected_boundary_artifact_sha256,
        agent_logical_table_id=expected_agent_logical_table_id,
        agent_logical_table_scope_sha256=agent_logical_table_scope_sha256,
        d1_table_map_artifact_id=d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=d1_table_map_artifact_sha256,
        materializer_logical_table_id=projection.table_fact.logical_table_id,
        materializer_structure_snapshot_id=(
            projection.table_fact.structure_snapshot_id
        ),
        table_fact_binding_sha256=projection.table_fact.fact_binding_sha256,
        input_projection_id=base_projection.projection_id,
        input_projection_revision=base_projection.projection_revision,
        projection=projection,
        parent_record_bindings=tuple(parent_record_bindings),
    )
    return AgentTableProjectionMaterializationV1(
        source_sha256=source_ir.source_sha256,
        boundary_artifact_id=expected_boundary_artifact_id,
        boundary_artifact_sha256=expected_boundary_artifact_sha256,
        agent_logical_table_id=expected_agent_logical_table_id,
        agent_logical_table_scope_sha256=agent_logical_table_scope_sha256,
        d1_table_map_artifact_id=d1_table_map_artifact_id,
        d1_table_map_artifact_sha256=d1_table_map_artifact_sha256,
        materializer_logical_table_id=projection.table_fact.logical_table_id,
        materializer_structure_snapshot_id=(
            projection.table_fact.structure_snapshot_id
        ),
        table_fact_binding_sha256=projection.table_fact.fact_binding_sha256,
        input_projection_id=base_projection.projection_id,
        input_projection_revision=base_projection.projection_revision,
        projection=projection,
        parent_record_bindings=tuple(parent_record_bindings),
        materialization_sha256=materialization_sha256,
    )


def stable_agent_table_projection_materialization_sha256(
    *,
    source_sha256: str,
    boundary_artifact_id: str,
    boundary_artifact_sha256: str,
    agent_logical_table_id: str,
    agent_logical_table_scope_sha256: str,
    d1_table_map_artifact_id: str,
    d1_table_map_artifact_sha256: str,
    materializer_logical_table_id: str,
    materializer_structure_snapshot_id: str,
    table_fact_binding_sha256: str,
    input_projection_id: str,
    input_projection_revision: int,
    projection: TableRecordProjectionV1,
    parent_record_bindings: tuple[AgentTableParentRecordBindingV1, ...],
) -> str:
    payload = {
        "schema_version": AGENT_TABLE_PROJECTION_MATERIALIZATION_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "boundary_artifact_id": boundary_artifact_id,
        "boundary_artifact_sha256": boundary_artifact_sha256,
        "agent_logical_table_id": agent_logical_table_id,
        "agent_logical_table_scope_sha256": agent_logical_table_scope_sha256,
        "d1_table_map_artifact_id": d1_table_map_artifact_id,
        "d1_table_map_artifact_sha256": d1_table_map_artifact_sha256,
        "materializer_logical_table_id": materializer_logical_table_id,
        "materializer_structure_snapshot_id": materializer_structure_snapshot_id,
        "table_fact_binding_sha256": table_fact_binding_sha256,
        "input_projection_id": input_projection_id,
        "input_projection_revision": input_projection_revision,
        "projection": projection.model_dump(mode="json"),
        "parent_record_bindings": [
            item.model_dump(mode="json") for item in parent_record_bindings
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def stable_agent_table_parent_boundary_sha256(
    *,
    source_sha256: str,
    boundary_artifact_id: str,
    boundary_artifact_sha256: str,
    logical_table_id: str,
    parent_provision_id: str,
    source_order: int,
    row_bindings: tuple[AgentTableRowBindingV1, ...],
    source_refs: tuple[StructureSourceRef, ...],
) -> str:
    payload = {
        "schema_version": AGENT_TABLE_PARENT_BOUNDARY_SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "boundary_artifact_id": boundary_artifact_id,
        "boundary_artifact_sha256": boundary_artifact_sha256,
        "logical_table_id": logical_table_id,
        "parent_provision_id": parent_provision_id,
        "source_order": source_order,
        "row_bindings": [item.model_dump(mode="json") for item in row_bindings],
        "source_refs": [item.model_dump(mode="json") for item in source_refs],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AGENT_TABLE_PARENT_BOUNDARY_SCHEMA_VERSION",
    "AGENT_TABLE_PROJECTION_MATERIALIZATION_SCHEMA_VERSION",
    "AgentTableParentBoundaryV1",
    "AgentTableParentRecordBindingV1",
    "AgentTableProjectionMaterializationV1",
    "AgentTableRowBindingV1",
    "agent_boundaries_to_manual_row_groups",
    "build_agent_table_parent_boundary",
    "materialize_agent_table_parent_boundaries",
    "stable_agent_table_parent_boundary_sha256",
    "stable_agent_table_projection_materialization_sha256",
]
