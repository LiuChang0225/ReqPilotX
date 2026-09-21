from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from reqpilotx_server.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableIR,
)
from reqpilotx_server.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from reqpilotx_server.domain.extraction.structure_revision import canonical_model_sha256
from reqpilotx_server.domain.extraction.table_records import (
    TableRecordProjectionV1,
    normalize_table_record_source_refs,
)


AGENT_LOGICAL_TABLE_SCOPE_SCHEMA_VERSION = "agent-logical-table-scope/v1"
AGENT_TABLE_PARENT_MATERIALIZER_INPUT_SCHEMA_VERSION = (
    "agent-table-parent-materializer-input/v1"
)
_REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,119}$")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class AgentD1TableMapArtifactIdentityV1(_FrozenModel):
    """Host-pinned identity of the D1 evidence artifact used by the Agent."""

    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_id: str = Field(min_length=1, max_length=240)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("D1 table-map artifact ID must not be blank")
        return value


class AgentLogicalTableScopeEvidenceV1(_FrozenModel):
    """Content-free evidence handle supporting one logical-object decision."""

    evidence_ref: str = Field(min_length=1, max_length=400)
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    page_numbers: tuple[StrictInt, ...] = ()

    @field_validator("evidence_ref")
    @classmethod
    def validate_evidence_ref(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("logical-table evidence ref must not be blank")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("logical-table evidence pages must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError("logical-table evidence pages must be unique and sorted")
        return value


class AgentLogicalTablePhysicalPartV1(_FrozenModel):
    """Exact, non-authoritative binding to one current physical TableIR fact."""

    table_id: str = Field(min_length=1, max_length=160)
    source_node_id: str = Field(pattern=r"^node-[0-9a-f]{32}$")
    source_order: StrictInt = Field(ge=0)
    page_numbers: tuple[StrictInt, ...] = ()
    row_count: StrictInt = Field(ge=1)
    column_count: StrictInt = Field(ge=1)
    cell_ids: tuple[str, ...] = ()
    table_ir_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("physical table ID must not be blank")
        return value

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(not cell_id.strip() for cell_id in value):
            raise ValueError("physical table cell IDs must be unique and non-empty")
        return value

    @field_validator("page_numbers")
    @classmethod
    def validate_page_numbers(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(page_number < 1 for page_number in value):
            raise ValueError("physical table pages must be positive")
        if value != tuple(sorted(set(value))):
            raise ValueError("physical table pages must be unique and sorted")
        return value

    @model_validator(mode="after")
    def validate_source_refs(self) -> AgentLogicalTablePhysicalPartV1:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("physical table source refs must be unique")
        source_node_refs = tuple(
            ref
            for ref in self.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
        )
        if source_node_refs != (
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=self.source_node_id,
            ),
        ):
            raise ValueError("physical table must bind exactly its SourceIR table node")
        if any(
            ref.kind
            not in {
                StructureSourceRefKind.SOURCE_NODE,
                StructureSourceRefKind.TABLE_CELL,
            }
            for ref in self.source_refs
        ):
            raise ValueError("physical table scope accepts only node and cell refs")
        referenced_cell_ids = {
            ref.source_id
            for ref in self.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
        }
        if referenced_cell_ids != set(self.cell_ids):
            raise ValueError("physical table source refs must bind exactly its cells")
        if self.source_refs != normalize_table_record_source_refs(self.source_refs):
            raise ValueError("physical table source refs must use canonical order")
        return self


class AgentLogicalTableScopeV1(_FrozenModel):
    """Agent-owned grouping of physical tables into one logical object.

    The artifact deliberately has no Structure node or ``CONTINUES`` relation.
    SourceIR and the D1 table-map artifact are immutable evidence/tool inputs;
    they do not decide which physical tables form the logical object.
    """

    schema_version: Literal["agent-logical-table-scope/v1"] = (
        AGENT_LOGICAL_TABLE_SCOPE_SCHEMA_VERSION
    )
    source_ir_id: str = Field(min_length=1, max_length=200)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_ir_canonical_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1
    logical_object_id: str = Field(pattern=r"^agent-logical-table-[0-9a-f]{32}$")
    physical_parts: tuple[AgentLogicalTablePhysicalPartV1, ...] = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1, max_length=16)
    evidence: tuple[AgentLogicalTableScopeEvidenceV1, ...] = Field(min_length=1)
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("source_ir_id")
    @classmethod
    def validate_source_ir_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("SourceIR ID must not be blank")
        return value

    @field_validator("reason_codes")
    @classmethod
    def validate_reason_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))) or any(
            _REASON_CODE_PATTERN.fullmatch(code) is None for code in value
        ):
            raise ValueError(
                "logical-table reason codes must be unique, sorted snake_case"
            )
        return value

    @field_validator("evidence")
    @classmethod
    def validate_evidence(
        cls,
        value: tuple[AgentLogicalTableScopeEvidenceV1, ...],
    ) -> tuple[AgentLogicalTableScopeEvidenceV1, ...]:
        refs = tuple(item.evidence_ref for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("logical-table evidence refs must be unique")
        canonical = tuple(
            sorted(
                value,
                key=lambda item: (
                    item.evidence_ref,
                    item.evidence_sha256,
                    item.page_numbers,
                ),
            )
        )
        if value != canonical:
            raise ValueError("logical-table evidence must use canonical order")
        return value

    @model_validator(mode="after")
    def validate_scope(self) -> AgentLogicalTableScopeV1:
        if self.d1_table_map_artifact.source_sha256 != self.source_sha256:
            raise ValueError("D1 table-map artifact belongs to another source")
        table_ids = tuple(part.table_id for part in self.physical_parts)
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("logical-table scope cannot repeat a physical table")
        source_node_ids = tuple(part.source_node_id for part in self.physical_parts)
        if len(source_node_ids) != len(set(source_node_ids)):
            raise ValueError("logical-table scope cannot repeat a source node")
        canonical_parts = tuple(
            sorted(
                self.physical_parts,
                key=lambda part: (part.source_order, part.table_id),
            )
        )
        if self.physical_parts != canonical_parts:
            raise ValueError("logical-table physical parts must follow source order")
        if len({part.source_order for part in self.physical_parts}) != len(
            self.physical_parts
        ):
            raise ValueError("logical-table physical parts require unique source order")

        expected_object_id = stable_agent_logical_table_object_id(
            source_sha256=self.source_sha256,
            physical_table_ids=table_ids,
        )
        if self.logical_object_id != expected_object_id:
            raise ValueError("logical-object identity does not bind its physical tables")

        evidence_by_ref = {item.evidence_ref: item for item in self.evidence}
        for part in self.physical_parts:
            expected_evidence = AgentLogicalTableScopeEvidenceV1(
                evidence_ref=_table_map_evidence_ref(part.table_id),
                evidence_sha256=part.d1_table_map_sha256,
                page_numbers=part.page_numbers,
            )
            if evidence_by_ref.get(expected_evidence.evidence_ref) != expected_evidence:
                raise ValueError(
                    "logical-table scope must retain exact D1 table-map evidence"
                )

        expected_scope_sha256 = stable_agent_logical_table_scope_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            d1_table_map_artifact=self.d1_table_map_artifact,
            logical_object_id=self.logical_object_id,
            physical_parts=self.physical_parts,
            reason_codes=self.reason_codes,
            evidence=self.evidence,
        )
        if self.scope_sha256 != expected_scope_sha256:
            raise ValueError("logical-table scope hash does not bind its decision")
        return self

    @property
    def physical_table_ids(self) -> tuple[str, ...]:
        return tuple(part.table_id for part in self.physical_parts)


class AgentTableParentBoundaryMaterializerInputV1(_FrozenModel):
    """Validated arguments that map directly to the existing materializer.

    ``base_projection`` is accepted only when its immutable table fact is an
    exact match for the Agent-owned scope. Its Structure identity remains an
    implementation handle and never becomes logical-object authority.
    """

    schema_version: Literal["agent-table-parent-materializer-input/v1"] = (
        AGENT_TABLE_PARENT_MATERIALIZER_INPUT_SCHEMA_VERSION
    )
    scope: AgentLogicalTableScopeV1
    expected_agent_logical_table_id: str = Field(
        pattern=r"^agent-logical-table-[0-9a-f]{32}$"
    )
    agent_logical_table_scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    d1_table_map_artifact_id: str = Field(min_length=1, max_length=240)
    d1_table_map_artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    base_projection: TableRecordProjectionV1

    @model_validator(mode="after")
    def validate_projection_scope(self) -> AgentTableParentBoundaryMaterializerInputV1:
        if (
            self.expected_agent_logical_table_id != self.scope.logical_object_id
            or self.agent_logical_table_scope_sha256 != self.scope.scope_sha256
            or self.d1_table_map_artifact_id
            != self.scope.d1_table_map_artifact.artifact_id
            or self.d1_table_map_artifact_sha256
            != self.scope.d1_table_map_artifact.artifact_sha256
        ):
            raise ValueError("materializer input logical-table lineage is inconsistent")
        fact = self.base_projection.table_fact
        if (
            fact.source_ir_id != self.scope.source_ir_id
            or fact.source_sha256 != self.scope.source_sha256
            or fact.source_ir_canonical_sha256
            != self.scope.source_ir_canonical_sha256
        ):
            raise ValueError("materializer projection belongs to another source")
        if fact.table_ids != self.scope.physical_table_ids:
            raise ValueError(
                "materializer projection parts differ from Agent logical-table scope"
            )
        return self

def build_agent_logical_table_scope(
    source_ir: SourceIRSnapshot,
    *,
    d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    physical_table_ids: tuple[str, ...],
    reason_codes: tuple[str, ...],
    supplemental_evidence: tuple[AgentLogicalTableScopeEvidenceV1, ...] = (),
) -> AgentLogicalTableScopeV1:
    """Pin an Agent-selected logical grouping to current immutable table facts."""

    if d1_table_map_artifact.source_sha256 != source_ir.source_sha256:
        raise ValueError("D1 table-map artifact belongs to another source")
    if not physical_table_ids:
        raise ValueError("logical-table scope requires at least one physical table")
    if len(physical_table_ids) != len(set(physical_table_ids)):
        raise ValueError("logical-table scope cannot repeat a physical table")
    catalog = _physical_table_catalog(source_ir)
    unknown = tuple(table_id for table_id in physical_table_ids if table_id not in catalog)
    if unknown:
        raise ValueError("logical-table scope references an unknown physical table")
    selected_nodes = tuple(catalog[table_id] for table_id in physical_table_ids)
    canonical_ids = tuple(
        node.table_payload.table_id
        for node in sorted(
            selected_nodes,
            key=lambda item: (item.source_order, item.table_payload.table_id),
        )
        if node.table_payload is not None
    )
    if physical_table_ids != canonical_ids:
        raise ValueError("logical-table physical table IDs must follow SourceIR order")

    physical_parts = tuple(_physical_part(node) for node in selected_nodes)
    table_map_evidence = tuple(
        AgentLogicalTableScopeEvidenceV1(
            evidence_ref=_table_map_evidence_ref(part.table_id),
            evidence_sha256=part.d1_table_map_sha256,
            page_numbers=part.page_numbers,
        )
        for part in physical_parts
    )
    evidence = tuple(
        sorted(
            (*table_map_evidence, *supplemental_evidence),
            key=lambda item: (
                item.evidence_ref,
                item.evidence_sha256,
                item.page_numbers,
            ),
        )
    )
    if len({item.evidence_ref for item in evidence}) != len(evidence):
        raise ValueError("logical-table evidence refs must be unique")
    logical_object_id = stable_agent_logical_table_object_id(
        source_sha256=source_ir.source_sha256,
        physical_table_ids=physical_table_ids,
    )
    source_ir_hash = source_ir_canonical_sha256(source_ir)
    scope_sha256 = stable_agent_logical_table_scope_sha256(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_ir_hash,
        d1_table_map_artifact=d1_table_map_artifact,
        logical_object_id=logical_object_id,
        physical_parts=physical_parts,
        reason_codes=reason_codes,
        evidence=evidence,
    )
    return AgentLogicalTableScopeV1(
        source_ir_id=source_ir.source_ir_id,
        source_sha256=source_ir.source_sha256,
        source_ir_canonical_sha256=source_ir_hash,
        d1_table_map_artifact=d1_table_map_artifact,
        logical_object_id=logical_object_id,
        physical_parts=physical_parts,
        reason_codes=reason_codes,
        evidence=evidence,
        scope_sha256=scope_sha256,
    )


def validate_agent_logical_table_scope(
    source_ir: SourceIRSnapshot,
    scope: AgentLogicalTableScopeV1,
    *,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
) -> AgentLogicalTableScopeV1:
    """Reject stale, reordered, unknown, or tampered physical-table bindings."""

    if (
        scope.source_ir_id != source_ir.source_ir_id
        or scope.source_sha256 != source_ir.source_sha256
        or scope.source_ir_canonical_sha256 != source_ir_canonical_sha256(source_ir)
    ):
        raise ValueError("logical-table scope belongs to another source snapshot")
    if scope.d1_table_map_artifact != expected_d1_table_map_artifact:
        raise ValueError("logical-table scope D1 table-map artifact identity drifted")
    if expected_d1_table_map_artifact.source_sha256 != source_ir.source_sha256:
        raise ValueError("expected D1 table-map artifact belongs to another source")

    catalog = _physical_table_catalog(source_ir)
    unknown = tuple(
        table_id for table_id in scope.physical_table_ids if table_id not in catalog
    )
    if unknown:
        raise ValueError("logical-table scope references an unknown physical table")
    current_nodes = tuple(catalog[table_id] for table_id in scope.physical_table_ids)
    current_order = tuple(
        node.table_payload.table_id
        for node in sorted(
            current_nodes,
            key=lambda item: (item.source_order, item.table_payload.table_id),
        )
        if node.table_payload is not None
    )
    if current_order != scope.physical_table_ids:
        raise ValueError("logical-table physical table IDs are reordered")
    current_parts = tuple(_physical_part(node) for node in current_nodes)
    if current_parts != scope.physical_parts:
        raise ValueError("logical-table physical TableIR binding is stale or tampered")
    expected_object_id = stable_agent_logical_table_object_id(
        source_sha256=source_ir.source_sha256,
        physical_table_ids=current_order,
    )
    if scope.logical_object_id != expected_object_id:
        raise ValueError("logical-object identity drifted")
    return scope


def agent_logical_table_scope_to_parent_materializer_input(
    source_ir: SourceIRSnapshot,
    scope: AgentLogicalTableScopeV1,
    *,
    expected_d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    base_projection: TableRecordProjectionV1,
) -> AgentTableParentBoundaryMaterializerInputV1:
    """Bind a compatible existing table projection without granting it authority."""

    validated_scope = validate_agent_logical_table_scope(
        source_ir,
        scope,
        expected_d1_table_map_artifact=expected_d1_table_map_artifact,
    )
    fact_parts = base_projection.table_fact.table_parts
    if len(fact_parts) != len(validated_scope.physical_parts):
        raise ValueError(
            "materializer projection parts differ from Agent logical-table scope"
        )
    for scoped_part, fact_part in zip(
        validated_scope.physical_parts,
        fact_parts,
        strict=True,
    ):
        if (
            fact_part.table_id != scoped_part.table_id
            or fact_part.source_order != scoped_part.source_order
            or fact_part.row_count != scoped_part.row_count
            or fact_part.column_count != scoped_part.column_count
            or fact_part.cell_ids != scoped_part.cell_ids
            or fact_part.table_content_sha256 != scoped_part.table_ir_sha256
            or fact_part.source_refs != scoped_part.source_refs
        ):
            raise ValueError(
                "materializer table fact differs from Agent logical-table scope"
            )
    return AgentTableParentBoundaryMaterializerInputV1(
        scope=validated_scope,
        expected_agent_logical_table_id=validated_scope.logical_object_id,
        agent_logical_table_scope_sha256=validated_scope.scope_sha256,
        d1_table_map_artifact_id=(
            validated_scope.d1_table_map_artifact.artifact_id
        ),
        d1_table_map_artifact_sha256=(
            validated_scope.d1_table_map_artifact.artifact_sha256
        ),
        base_projection=base_projection,
    )


def stable_agent_logical_table_object_id(
    *,
    source_sha256: str,
    physical_table_ids: tuple[str, ...],
) -> str:
    digest = _canonical_sha256(
        {
            "source_sha256": source_sha256,
            "physical_table_ids": list(physical_table_ids),
        }
    )
    return f"agent-logical-table-{digest[:32]}"


def stable_agent_logical_table_scope_sha256(
    *,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    d1_table_map_artifact: AgentD1TableMapArtifactIdentityV1,
    logical_object_id: str,
    physical_parts: tuple[AgentLogicalTablePhysicalPartV1, ...],
    reason_codes: tuple[str, ...],
    evidence: tuple[AgentLogicalTableScopeEvidenceV1, ...],
) -> str:
    return _canonical_sha256(
        {
            "schema_version": AGENT_LOGICAL_TABLE_SCOPE_SCHEMA_VERSION,
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "d1_table_map_artifact": d1_table_map_artifact.model_dump(mode="json"),
            "logical_object_id": logical_object_id,
            "physical_parts": [
                part.model_dump(mode="json") for part in physical_parts
            ],
            "reason_codes": list(reason_codes),
            "evidence": [item.model_dump(mode="json") for item in evidence],
        }
    )


def source_ir_canonical_sha256(source_ir: SourceIRSnapshot) -> str:
    payload = source_ir.model_dump(mode="json", exclude={"created_at"})
    return _canonical_sha256(payload)


def stable_d1_table_map_sha256(node: SourceNode) -> str:
    """Match the complete D1 ``document.get_table_map`` evidence digest."""

    table = _require_table_payload(node)
    cells = [
        {
            "cell_id": cell.cell_id,
            "row": cell.row,
            "column": cell.column,
            "row_span": cell.row_span,
            "column_span": cell.column_span,
            "text": cell.raw_text,
            "text_sha256": hashlib.sha256(cell.raw_text.encode("utf-8")).hexdigest(),
            "bbox": cell.native_locator.bbox,
        }
        for cell in table.cells
    ]
    return _canonical_sha256(
        {
            "table_id": table.table_id,
            "page_number": node.native_locator.page_number,
            "bbox": node.native_locator.bbox,
            "row_count": table.row_count,
            "column_count": table.column_count,
            "header_regions": [
                (region.row, region.column, region.row_span, region.column_span)
                for region in table.header_regions
            ],
            "merged_regions": [
                (region.row, region.column, region.row_span, region.column_span)
                for region in table.merged_regions
            ],
            "notes": list(table.notes),
            "cells": cells,
        }
    )


def _physical_table_catalog(source_ir: SourceIRSnapshot) -> dict[str, SourceNode]:
    catalog: dict[str, SourceNode] = {}
    for node in source_ir.nodes:
        if node.kind is not SourceNodeKind.TABLE:
            continue
        table = _require_table_payload(node)
        if table.table_id in catalog:
            raise ValueError("SourceIR contains an ambiguous physical table ID")
        catalog[table.table_id] = node
    return catalog


def _physical_part(node: SourceNode) -> AgentLogicalTablePhysicalPartV1:
    table = _require_table_payload(node)
    ordered_cells = tuple(
        sorted(table.cells, key=lambda cell: (cell.row, cell.column, cell.cell_id))
    )
    page_numbers = tuple(
        sorted(
            {
                page_number
                for page_number in (
                    node.native_locator.page_number,
                    *(cell.native_locator.page_number for cell in ordered_cells),
                )
                if page_number is not None
            }
        )
    )
    source_refs = normalize_table_record_source_refs(
        (
            StructureSourceRef(
                kind=StructureSourceRefKind.SOURCE_NODE,
                source_id=node.source_node_id,
            ),
            *tuple(
                StructureSourceRef(
                    kind=StructureSourceRefKind.TABLE_CELL,
                    source_id=cell.cell_id,
                )
                for cell in ordered_cells
            ),
        )
    )
    return AgentLogicalTablePhysicalPartV1(
        table_id=table.table_id,
        source_node_id=node.source_node_id,
        source_order=node.source_order,
        page_numbers=page_numbers,
        row_count=table.row_count,
        column_count=table.column_count,
        cell_ids=tuple(cell.cell_id for cell in ordered_cells),
        table_ir_sha256=canonical_model_sha256(table),
        d1_table_map_sha256=stable_d1_table_map_sha256(node),
        source_refs=source_refs,
    )


def _require_table_payload(node: SourceNode) -> TableIR:
    if node.kind is not SourceNodeKind.TABLE or node.table_payload is None:
        raise ValueError("logical-table scope requires a physical SourceIR table")
    return node.table_payload


def _table_map_evidence_ref(table_id: str) -> str:
    return f"document-table-map:{table_id}"


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AGENT_LOGICAL_TABLE_SCOPE_SCHEMA_VERSION",
    "AGENT_TABLE_PARENT_MATERIALIZER_INPUT_SCHEMA_VERSION",
    "AgentD1TableMapArtifactIdentityV1",
    "AgentLogicalTablePhysicalPartV1",
    "AgentLogicalTableScopeEvidenceV1",
    "AgentLogicalTableScopeV1",
    "AgentTableParentBoundaryMaterializerInputV1",
    "agent_logical_table_scope_to_parent_materializer_input",
    "build_agent_logical_table_scope",
    "source_ir_canonical_sha256",
    "stable_agent_logical_table_object_id",
    "stable_agent_logical_table_scope_sha256",
    "stable_d1_table_map_sha256",
    "validate_agent_logical_table_scope",
]
