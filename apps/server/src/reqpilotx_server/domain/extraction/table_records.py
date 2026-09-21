from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from enum import Enum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from reqpilotx_server.domain.extraction.structure import StructureSourceRef


TABLE_RECORD_PROJECTION_SCHEMA_VERSION = "table-record-projection/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class TableRecordProjectionMode(str, Enum):
    """Whether a logical table stays whole or exposes proven records."""

    WHOLE_TABLE = "WHOLE_TABLE"
    RECORDS = "RECORDS"


class TableRecordProjectionDecisionOrigin(str, Enum):
    """Who selected the structural projection mode."""

    AUTO = "AUTO"
    AGENT = "AGENT"
    MANUAL = "MANUAL"


class TableRecordProjectionDecisionV1(_StrictFrozenModel):
    """Reader-facing explanation that does not participate in identity.

    Projection identity predates this optional explanation.  Keeping it
    outside ``projection_sha256`` lets existing v1 artifacts validate and
    lets presentation text improve without changing the structural result.
    """

    origin: TableRecordProjectionDecisionOrigin
    mode: TableRecordProjectionMode
    reason_code: StrictStr = Field(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    message: StrictStr = Field(min_length=1, max_length=500)
    evidence: tuple[StrictStr, ...] = Field(default=(), max_length=24)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _require_non_blank(value, "projection decision message")

    @field_validator("evidence")
    @classmethod
    def validate_evidence(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_non_blank(values, "projection decision evidence")


class TableRecordKind(str, Enum):
    """A structural record boundary, never a requirement classification."""

    ROW = "ROW"
    MATRIX_INTERSECTION = "MATRIX_INTERSECTION"


class TableRecordHeaderAxis(str, Enum):
    ROW = "ROW"
    COLUMN = "COLUMN"
    CONTEXT = "CONTEXT"


class TableFactPartV1(_StrictFrozenModel):
    """Immutable reference to one physical SourceIR table fact.

    A logical table may contain several physical parts joined by a verified
    continuation relation. The source fact remains authoritative; this model
    records identities and digests instead of copying or rewriting its cells.
    """

    table_id: StrictStr = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    row_count: StrictInt = Field(ge=1)
    column_count: StrictInt = Field(ge=1)
    cell_ids: tuple[StrictStr, ...] = ()
    table_content_sha256: StrictStr
    source_refs: tuple[StructureSourceRef, ...]
    visual_asset_refs: tuple[StrictStr, ...] = ()

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        return _require_non_blank(value, "table_id")

    @field_validator("cell_ids", "visual_asset_refs")
    @classmethod
    def validate_unique_non_blank_ids(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        if any(not value.strip() for value in values):
            raise ValueError("table fact identities must not be blank")
        if len(values) != len(set(values)):
            raise ValueError("table fact identities must be unique")
        return values

    @field_validator("table_content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        return _require_sha256(value, "table_content_sha256")

    @model_validator(mode="after")
    def validate_source_refs(self) -> TableFactPartV1:
        if not self.source_refs:
            raise ValueError("a table fact part requires immutable source refs")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("table fact source refs must be unique")
        return self


class TableFactBindingV1(_StrictFrozenModel):
    """Revision-bound pointer to the complete, immutable logical table."""

    source_ir_id: StrictStr = Field(min_length=1, max_length=200)
    source_sha256: StrictStr
    source_ir_canonical_sha256: StrictStr
    structure_snapshot_id: StrictStr = Field(min_length=1, max_length=200)
    logical_table_id: StrictStr = Field(min_length=1, max_length=200)
    owner_structure_node_id: StrictStr = Field(min_length=1, max_length=200)
    table_parts: tuple[TableFactPartV1, ...] = Field(min_length=1)
    whole_table_source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    fact_binding_sha256: StrictStr

    @field_validator(
        "source_ir_id",
        "structure_snapshot_id",
        "logical_table_id",
        "owner_structure_node_id",
    )
    @classmethod
    def validate_non_blank_identity(cls, value: str) -> str:
        return _require_non_blank(value, "table fact identity")

    @field_validator(
        "source_sha256",
        "source_ir_canonical_sha256",
        "fact_binding_sha256",
    )
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        return _require_sha256(value, "table fact digest")

    @model_validator(mode="after")
    def validate_binding(self) -> TableFactBindingV1:
        table_ids = tuple(part.table_id for part in self.table_parts)
        if len(table_ids) != len(set(table_ids)):
            raise ValueError("logical table parts must use unique table IDs")
        source_orders = tuple(part.source_order for part in self.table_parts)
        if len(source_orders) != len(set(source_orders)):
            raise ValueError("logical table parts must use unique source order")
        expected_parts = tuple(
            sorted(self.table_parts, key=lambda part: (part.source_order, part.table_id))
        )
        if self.table_parts != expected_parts:
            raise ValueError("logical table parts must follow source order")
        if len(self.whole_table_source_refs) != len(
            set(self.whole_table_source_refs)
        ):
            raise ValueError("whole-table source refs must be unique")
        expected_hash = stable_table_fact_binding_sha256(
            source_ir_id=self.source_ir_id,
            source_sha256=self.source_sha256,
            source_ir_canonical_sha256=self.source_ir_canonical_sha256,
            structure_snapshot_id=self.structure_snapshot_id,
            logical_table_id=self.logical_table_id,
            owner_structure_node_id=self.owner_structure_node_id,
            table_parts=self.table_parts,
            whole_table_source_refs=self.whole_table_source_refs,
        )
        if self.fact_binding_sha256 != expected_hash:
            raise ValueError(
                "fact_binding_sha256 must bind the immutable whole table"
            )
        return self

    @property
    def table_ids(self) -> tuple[str, ...]:
        return tuple(part.table_id for part in self.table_parts)


class TableRecordRowBindingV1(_StrictFrozenModel):
    table_id: StrictStr = Field(min_length=1, max_length=160)
    row_index: StrictInt = Field(ge=0)
    cell_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        return _require_non_blank(value, "row binding table_id")

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_non_blank(values, "row binding cell_ids")


class TableRecordColumnBindingV1(_StrictFrozenModel):
    table_id: StrictStr = Field(min_length=1, max_length=160)
    column_index: StrictInt = Field(ge=0)
    cell_ids: tuple[StrictStr, ...] = Field(min_length=1)

    @field_validator("table_id")
    @classmethod
    def validate_table_id(cls, value: str) -> str:
        return _require_non_blank(value, "column binding table_id")

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_non_blank(values, "column binding cell_ids")


class TableRecordHeaderBindingV1(_StrictFrozenModel):
    table_id: StrictStr = Field(min_length=1, max_length=160)
    axis: TableRecordHeaderAxis
    level: StrictInt = Field(ge=0)
    cell_ids: tuple[StrictStr, ...] = Field(min_length=1)
    text: StrictStr = Field(min_length=1, max_length=500_000)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator("table_id", "text")
    @classmethod
    def validate_non_blank_text(cls, value: str) -> str:
        return _require_non_blank(value, "header binding value")

    @field_validator("cell_ids")
    @classmethod
    def validate_cell_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_non_blank(values, "header binding cell_ids")

    @model_validator(mode="after")
    def validate_source_refs(self) -> TableRecordHeaderBindingV1:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("header binding source refs must be unique")
        return self


class TableRecordSharedMergedValueV1(_StrictFrozenModel):
    """A merged/shared value that must accompany every affected record."""

    table_id: StrictStr = Field(min_length=1, max_length=160)
    cell_id: StrictStr = Field(min_length=1, max_length=160)
    row: StrictInt = Field(ge=0)
    column: StrictInt = Field(ge=0)
    row_span: StrictInt = Field(default=1, ge=1)
    column_span: StrictInt = Field(default=1, ge=1)
    text: StrictStr = Field(min_length=1, max_length=500_000)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator("table_id", "cell_id", "text")
    @classmethod
    def validate_non_blank_value(cls, value: str) -> str:
        return _require_non_blank(value, "shared merged value")

    @model_validator(mode="after")
    def validate_shared_value(self) -> TableRecordSharedMergedValueV1:
        if self.row_span == 1 and self.column_span == 1:
            raise ValueError("a shared merged value must span multiple cells")
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("shared value source refs must be unique")
        return self


class TableRecordNoteV1(_StrictFrozenModel):
    table_id: StrictStr = Field(min_length=1, max_length=160)
    source_order: StrictInt = Field(ge=0)
    text: StrictStr = Field(min_length=1, max_length=500_000)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator("table_id", "text")
    @classmethod
    def validate_non_blank_value(cls, value: str) -> str:
        return _require_non_blank(value, "table record note")

    @model_validator(mode="after")
    def validate_source_refs(self) -> TableRecordNoteV1:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("table record note source refs must be unique")
        return self


class TableRecordVisualRefV1(_StrictFrozenModel):
    asset_id: StrictStr = Field(min_length=1, max_length=200)
    table_id: StrictStr = Field(min_length=1, max_length=160)
    cell_id: StrictStr | None = Field(default=None, max_length=160)
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)

    @field_validator("asset_id", "table_id")
    @classmethod
    def validate_non_blank_value(cls, value: str) -> str:
        return _require_non_blank(value, "table visual reference")

    @field_validator("cell_id")
    @classmethod
    def validate_optional_cell_id(cls, value: str | None) -> str | None:
        if value is not None:
            return _require_non_blank(value, "table visual cell_id")
        return value

    @model_validator(mode="after")
    def validate_source_refs(self) -> TableRecordVisualRefV1:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("table visual source refs must be unique")
        return self


class TableRecordV1(_StrictFrozenModel):
    """One proven structural table record.

    ``is_requirement`` is deliberately fixed to false. Requirement
    classification and atomization happen later and may yield zero, one, or
    many requirements without changing this record's topology or identity.
    """

    record_id: StrictStr = Field(
        pattern=r"^table-record-[0-9a-f]{32}$"
    )
    is_requirement: Literal[False] = False
    kind: TableRecordKind
    logical_table_id: StrictStr = Field(min_length=1, max_length=200)
    table_ids: tuple[StrictStr, ...] = Field(min_length=1)
    source_order: StrictInt = Field(ge=0)
    owner_structure_node_id: StrictStr = Field(min_length=1, max_length=200)
    row_bindings: tuple[TableRecordRowBindingV1, ...] = Field(min_length=1)
    column_bindings: tuple[TableRecordColumnBindingV1, ...] = Field(min_length=1)
    # An explicitly numbered structural row is a proven record boundary even
    # when the source table declares no header cells.  Keep that absence
    # truthful instead of inventing a synthetic column label.  Header-driven
    # and matrix projections still enforce the axes they actually require.
    header_bindings: tuple[TableRecordHeaderBindingV1, ...] = ()
    shared_merged_values: tuple[TableRecordSharedMergedValueV1, ...] = ()
    notes: tuple[TableRecordNoteV1, ...] = ()
    figure_refs: tuple[TableRecordVisualRefV1, ...] = ()
    image_refs: tuple[TableRecordVisualRefV1, ...] = ()
    source_refs: tuple[StructureSourceRef, ...] = Field(min_length=1)
    content_sha256: StrictStr

    @field_validator("logical_table_id", "owner_structure_node_id")
    @classmethod
    def validate_non_blank_identity(cls, value: str) -> str:
        return _require_non_blank(value, "table record identity")

    @field_validator("table_ids")
    @classmethod
    def validate_table_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return _require_unique_non_blank(values, "record table_ids")

    @field_validator("content_sha256")
    @classmethod
    def validate_content_sha256(cls, value: str) -> str:
        return _require_sha256(value, "record content_sha256")

    @model_validator(mode="after")
    def validate_record(self) -> TableRecordV1:
        if len(self.source_refs) != len(set(self.source_refs)):
            raise ValueError("record source refs must be unique")
        _require_unique_coordinates(
            ((item.table_id, item.row_index) for item in self.row_bindings),
            "row bindings",
        )
        _require_unique_coordinates(
            ((item.table_id, item.column_index) for item in self.column_bindings),
            "column bindings",
        )

        used_table_ids = _record_used_table_ids(
            row_bindings=self.row_bindings,
            column_bindings=self.column_bindings,
            header_bindings=self.header_bindings,
            shared_merged_values=self.shared_merged_values,
            notes=self.notes,
            figure_refs=self.figure_refs,
            image_refs=self.image_refs,
        )
        if set(used_table_ids) != set(self.table_ids):
            raise ValueError("record table_ids must match all bound table parts")

        axes = {binding.axis for binding in self.header_bindings}
        if self.kind is TableRecordKind.ROW:
            if axes and TableRecordHeaderAxis.COLUMN not in axes:
                raise ValueError("a row record requires a column header binding")
        else:
            if len(self.row_bindings) != 1 or len(self.column_bindings) != 1:
                raise ValueError(
                    "a matrix intersection binds exactly one row and one column"
                )
            if not {
                TableRecordHeaderAxis.ROW,
                TableRecordHeaderAxis.COLUMN,
            }.issubset(axes):
                raise ValueError(
                    "a matrix intersection requires row and column headers"
                )

        expected_id = stable_table_record_id(
            logical_table_id=self.logical_table_id,
            table_ids=self.table_ids,
            kind=self.kind,
            row_bindings=self.row_bindings,
            column_bindings=self.column_bindings,
        )
        if self.record_id != expected_id:
            raise ValueError("record_id must bind the structural record boundary")
        expected_hash = stable_table_record_content_sha256(
            record_id=self.record_id,
            kind=self.kind,
            logical_table_id=self.logical_table_id,
            table_ids=self.table_ids,
            source_order=self.source_order,
            owner_structure_node_id=self.owner_structure_node_id,
            row_bindings=self.row_bindings,
            column_bindings=self.column_bindings,
            header_bindings=self.header_bindings,
            shared_merged_values=self.shared_merged_values,
            notes=self.notes,
            figure_refs=self.figure_refs,
            image_refs=self.image_refs,
            source_refs=self.source_refs,
        )
        if self.content_sha256 != expected_hash:
            raise ValueError("content_sha256 must bind all record evidence")
        return self


class TableRecordProjectionV1(_StrictFrozenModel):
    """A deterministic structural projection over one immutable table fact."""

    schema_version: Literal["table-record-projection/v1"] = (
        TABLE_RECORD_PROJECTION_SCHEMA_VERSION
    )
    projection_id: StrictStr = Field(
        pattern=r"^table-record-projection-[0-9a-f]{32}$"
    )
    table_fact: TableFactBindingV1
    mode: TableRecordProjectionMode
    projection_revision: StrictInt = Field(ge=0)
    records: tuple[TableRecordV1, ...] = ()
    # Optional and deliberately excluded from the projection hash.  Old v1
    # artifacts do not carry it; new builders always do.
    decision: TableRecordProjectionDecisionV1 | None = None
    projection_sha256: StrictStr

    @field_validator("projection_sha256")
    @classmethod
    def validate_projection_sha256(cls, value: str) -> str:
        return _require_sha256(value, "projection_sha256")

    @model_validator(mode="after")
    def validate_projection(self) -> TableRecordProjectionV1:
        if self.mode is TableRecordProjectionMode.WHOLE_TABLE and self.records:
            raise ValueError("WHOLE_TABLE mode must not contain derived records")
        if self.mode is TableRecordProjectionMode.RECORDS and not self.records:
            raise ValueError("RECORDS mode requires at least one derived record")
        if self.decision is not None and self.decision.mode is not self.mode:
            raise ValueError("projection decision mode must match projection mode")

        record_ids = tuple(record.record_id for record in self.records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("projection record IDs must be unique")
        source_orders = tuple(record.source_order for record in self.records)
        if len(source_orders) != len(set(source_orders)):
            raise ValueError("projection records must use unique source order")
        if self.records != tuple(
            sorted(
                self.records,
                key=lambda record: (record.source_order, record.record_id),
            )
        ):
            raise ValueError("projection records must follow source order")

        table_part_by_id = {
            part.table_id: part for part in self.table_fact.table_parts
        }
        for record in self.records:
            self._validate_record_against_fact(record, table_part_by_id)

        expected_hash = stable_table_record_projection_sha256(
            table_fact=self.table_fact,
            mode=self.mode,
            projection_revision=self.projection_revision,
            records=self.records,
        )
        if self.projection_sha256 != expected_hash:
            raise ValueError(
                "projection_sha256 must bind the fact, mode, revision, and records"
            )
        expected_id = stable_table_record_projection_id(
            table_fact=self.table_fact,
            projection_revision=self.projection_revision,
            projection_sha256=self.projection_sha256,
        )
        if self.projection_id != expected_id:
            raise ValueError("projection_id must bind projection lineage and content")
        return self

    def _validate_record_against_fact(
        self,
        record: TableRecordV1,
        table_part_by_id: dict[str, TableFactPartV1],
    ) -> None:
        if record.logical_table_id != self.table_fact.logical_table_id:
            raise ValueError("record logical_table_id differs from its table fact")
        if (
            record.owner_structure_node_id
            != self.table_fact.owner_structure_node_id
        ):
            raise ValueError("record owner differs from its table fact")
        if not set(record.table_ids).issubset(table_part_by_id):
            raise ValueError("record references a table outside its logical table")

        for binding in record.row_bindings:
            part = table_part_by_id[binding.table_id]
            if binding.row_index >= part.row_count:
                raise ValueError("record row binding exceeds table bounds")
            _require_known_cells(binding.cell_ids, part, "row binding")
        for binding in record.column_bindings:
            part = table_part_by_id[binding.table_id]
            if binding.column_index >= part.column_count:
                raise ValueError("record column binding exceeds table bounds")
            _require_known_cells(binding.cell_ids, part, "column binding")
        for binding in record.header_bindings:
            part = table_part_by_id[binding.table_id]
            _require_known_cells(binding.cell_ids, part, "header binding")
        for value in record.shared_merged_values:
            part = table_part_by_id[value.table_id]
            if value.row + value.row_span > part.row_count:
                raise ValueError("shared merged value exceeds table row bounds")
            if value.column + value.column_span > part.column_count:
                raise ValueError("shared merged value exceeds table column bounds")
            _require_known_cells((value.cell_id,), part, "shared merged value")
        for visual in (*record.figure_refs, *record.image_refs):
            part = table_part_by_id[visual.table_id]
            if visual.asset_id not in part.visual_asset_refs:
                raise ValueError(
                    "visual reference is not bound by its immutable table fact"
                )
            if visual.cell_id is not None:
                _require_known_cells((visual.cell_id,), part, "visual reference")


def build_table_fact_binding(
    *,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
    owner_structure_node_id: str,
    table_parts: tuple[TableFactPartV1, ...],
    whole_table_source_refs: tuple[StructureSourceRef, ...],
) -> TableFactBindingV1:
    normalized_parts = tuple(
        sorted(table_parts, key=lambda part: (part.source_order, part.table_id))
    )
    normalized_refs = normalize_table_record_source_refs(
        whole_table_source_refs
    )
    binding_hash = stable_table_fact_binding_sha256(
        source_ir_id=source_ir_id,
        source_sha256=source_sha256,
        source_ir_canonical_sha256=source_ir_canonical_sha256,
        structure_snapshot_id=structure_snapshot_id,
        logical_table_id=logical_table_id,
        owner_structure_node_id=owner_structure_node_id,
        table_parts=normalized_parts,
        whole_table_source_refs=normalized_refs,
    )
    return TableFactBindingV1(
        source_ir_id=source_ir_id,
        source_sha256=source_sha256,
        source_ir_canonical_sha256=source_ir_canonical_sha256,
        structure_snapshot_id=structure_snapshot_id,
        logical_table_id=logical_table_id,
        owner_structure_node_id=owner_structure_node_id,
        table_parts=normalized_parts,
        whole_table_source_refs=normalized_refs,
        fact_binding_sha256=binding_hash,
    )


def build_table_record(
    *,
    table_fact: TableFactBindingV1,
    kind: TableRecordKind,
    source_order: int,
    row_bindings: tuple[TableRecordRowBindingV1, ...],
    column_bindings: tuple[TableRecordColumnBindingV1, ...],
    header_bindings: tuple[TableRecordHeaderBindingV1, ...],
    shared_merged_values: tuple[TableRecordSharedMergedValueV1, ...] = (),
    notes: tuple[TableRecordNoteV1, ...] = (),
    figure_refs: tuple[TableRecordVisualRefV1, ...] = (),
    image_refs: tuple[TableRecordVisualRefV1, ...] = (),
    source_refs: tuple[StructureSourceRef, ...],
) -> TableRecordV1:
    table_order = {
        part.table_id: index for index, part in enumerate(table_fact.table_parts)
    }
    normalized_rows = tuple(
        sorted(
            row_bindings,
            key=lambda item: (
                table_order.get(item.table_id, len(table_order)),
                item.row_index,
                item.cell_ids,
            ),
        )
    )
    normalized_columns = tuple(
        sorted(
            column_bindings,
            key=lambda item: (
                table_order.get(item.table_id, len(table_order)),
                item.column_index,
                item.cell_ids,
            ),
        )
    )
    axis_order = {
        TableRecordHeaderAxis.ROW: 0,
        TableRecordHeaderAxis.COLUMN: 1,
        TableRecordHeaderAxis.CONTEXT: 2,
    }
    normalized_headers = tuple(
        sorted(
            header_bindings,
            key=lambda item: (
                axis_order[item.axis],
                item.level,
                table_order.get(item.table_id, len(table_order)),
                item.cell_ids,
                item.text,
            ),
        )
    )
    normalized_shared_values = tuple(
        sorted(
            shared_merged_values,
            key=lambda item: (
                table_order.get(item.table_id, len(table_order)),
                item.row,
                item.column,
                item.cell_id,
            ),
        )
    )
    normalized_notes = tuple(
        sorted(
            notes,
            key=lambda item: (
                item.source_order,
                table_order.get(item.table_id, len(table_order)),
                item.text,
            ),
        )
    )
    normalized_figures = _normalize_visual_refs(figure_refs, table_order)
    normalized_images = _normalize_visual_refs(image_refs, table_order)
    normalized_source_refs = normalize_table_record_source_refs(source_refs)
    used_table_ids = _record_used_table_ids(
        row_bindings=normalized_rows,
        column_bindings=normalized_columns,
        header_bindings=normalized_headers,
        shared_merged_values=normalized_shared_values,
        notes=normalized_notes,
        figure_refs=normalized_figures,
        image_refs=normalized_images,
    )
    normalized_table_ids = tuple(
        table_id
        for table_id in table_fact.table_ids
        if table_id in set(used_table_ids)
    )
    record_id = stable_table_record_id(
        logical_table_id=table_fact.logical_table_id,
        table_ids=normalized_table_ids,
        kind=kind,
        row_bindings=normalized_rows,
        column_bindings=normalized_columns,
    )
    content_hash = stable_table_record_content_sha256(
        record_id=record_id,
        kind=kind,
        logical_table_id=table_fact.logical_table_id,
        table_ids=normalized_table_ids,
        source_order=source_order,
        owner_structure_node_id=table_fact.owner_structure_node_id,
        row_bindings=normalized_rows,
        column_bindings=normalized_columns,
        header_bindings=normalized_headers,
        shared_merged_values=normalized_shared_values,
        notes=normalized_notes,
        figure_refs=normalized_figures,
        image_refs=normalized_images,
        source_refs=normalized_source_refs,
    )
    return TableRecordV1(
        record_id=record_id,
        kind=kind,
        logical_table_id=table_fact.logical_table_id,
        table_ids=normalized_table_ids,
        source_order=source_order,
        owner_structure_node_id=table_fact.owner_structure_node_id,
        row_bindings=normalized_rows,
        column_bindings=normalized_columns,
        header_bindings=normalized_headers,
        shared_merged_values=normalized_shared_values,
        notes=normalized_notes,
        figure_refs=normalized_figures,
        image_refs=normalized_images,
        source_refs=normalized_source_refs,
        content_sha256=content_hash,
    )


def build_table_record_projection(
    *,
    table_fact: TableFactBindingV1,
    mode: TableRecordProjectionMode,
    projection_revision: int,
    records: tuple[TableRecordV1, ...] = (),
    decision: TableRecordProjectionDecisionV1 | None = None,
) -> TableRecordProjectionV1:
    normalized_records = normalize_table_record_projection_records(records)
    projection_hash = stable_table_record_projection_sha256(
        table_fact=table_fact,
        mode=mode,
        projection_revision=projection_revision,
        records=normalized_records,
    )
    projection_id = stable_table_record_projection_id(
        table_fact=table_fact,
        projection_revision=projection_revision,
        projection_sha256=projection_hash,
    )
    return TableRecordProjectionV1(
        projection_id=projection_id,
        table_fact=table_fact,
        mode=mode,
        projection_revision=projection_revision,
        records=normalized_records,
        decision=decision,
        projection_sha256=projection_hash,
    )


def normalize_table_record_source_refs(
    source_refs: tuple[StructureSourceRef, ...],
) -> tuple[StructureSourceRef, ...]:
    return tuple(
        sorted(
            set(source_refs),
            key=lambda source_ref: (
                source_ref.kind.value,
                source_ref.source_id,
            ),
        )
    )


def normalize_table_record_projection_records(
    records: tuple[TableRecordV1, ...],
) -> tuple[TableRecordV1, ...]:
    return tuple(
        sorted(records, key=lambda record: (record.source_order, record.record_id))
    )


def stable_table_fact_binding_sha256(
    *,
    source_ir_id: str,
    source_sha256: str,
    source_ir_canonical_sha256: str,
    structure_snapshot_id: str,
    logical_table_id: str,
    owner_structure_node_id: str,
    table_parts: tuple[TableFactPartV1, ...],
    whole_table_source_refs: tuple[StructureSourceRef, ...],
) -> str:
    return _sha256(
        {
            "source_ir_id": source_ir_id,
            "source_sha256": source_sha256,
            "source_ir_canonical_sha256": source_ir_canonical_sha256,
            "structure_snapshot_id": structure_snapshot_id,
            "logical_table_id": logical_table_id,
            "owner_structure_node_id": owner_structure_node_id,
            "table_parts": table_parts,
            "whole_table_source_refs": whole_table_source_refs,
        }
    )


def stable_table_record_id(
    *,
    logical_table_id: str,
    table_ids: tuple[str, ...],
    kind: TableRecordKind,
    row_bindings: tuple[TableRecordRowBindingV1, ...],
    column_bindings: tuple[TableRecordColumnBindingV1, ...],
) -> str:
    return _stable_id(
        "table-record",
        {
            "logical_table_id": logical_table_id,
            "table_ids": table_ids,
            "kind": kind,
            "row_bindings": row_bindings,
            "column_bindings": column_bindings,
        },
    )


def stable_table_record_content_sha256(
    *,
    record_id: str,
    kind: TableRecordKind,
    logical_table_id: str,
    table_ids: tuple[str, ...],
    source_order: int,
    owner_structure_node_id: str,
    row_bindings: tuple[TableRecordRowBindingV1, ...],
    column_bindings: tuple[TableRecordColumnBindingV1, ...],
    header_bindings: tuple[TableRecordHeaderBindingV1, ...],
    shared_merged_values: tuple[TableRecordSharedMergedValueV1, ...],
    notes: tuple[TableRecordNoteV1, ...],
    figure_refs: tuple[TableRecordVisualRefV1, ...],
    image_refs: tuple[TableRecordVisualRefV1, ...],
    source_refs: tuple[StructureSourceRef, ...],
) -> str:
    return _sha256(
        {
            "record_id": record_id,
            "is_requirement": False,
            "kind": kind,
            "logical_table_id": logical_table_id,
            "table_ids": table_ids,
            "source_order": source_order,
            "owner_structure_node_id": owner_structure_node_id,
            "row_bindings": row_bindings,
            "column_bindings": column_bindings,
            "header_bindings": header_bindings,
            "shared_merged_values": shared_merged_values,
            "notes": notes,
            "figure_refs": figure_refs,
            "image_refs": image_refs,
            "source_refs": source_refs,
        }
    )


def stable_table_record_projection_sha256(
    *,
    table_fact: TableFactBindingV1,
    mode: TableRecordProjectionMode,
    projection_revision: int,
    records: tuple[TableRecordV1, ...],
) -> str:
    return _sha256(
        {
            "schema_version": TABLE_RECORD_PROJECTION_SCHEMA_VERSION,
            "table_fact": table_fact,
            "mode": mode,
            "projection_revision": projection_revision,
            "records": records,
        }
    )


def stable_table_record_projection_id(
    *,
    table_fact: TableFactBindingV1,
    projection_revision: int,
    projection_sha256: str,
) -> str:
    return _stable_id(
        "table-record-projection",
        {
            "fact_binding_sha256": table_fact.fact_binding_sha256,
            "logical_table_id": table_fact.logical_table_id,
            "projection_revision": projection_revision,
            "projection_sha256": projection_sha256,
        },
    )


def verify_table_record_content_sha256(record: TableRecordV1) -> bool:
    return record.content_sha256 == stable_table_record_content_sha256(
        record_id=record.record_id,
        kind=record.kind,
        logical_table_id=record.logical_table_id,
        table_ids=record.table_ids,
        source_order=record.source_order,
        owner_structure_node_id=record.owner_structure_node_id,
        row_bindings=record.row_bindings,
        column_bindings=record.column_bindings,
        header_bindings=record.header_bindings,
        shared_merged_values=record.shared_merged_values,
        notes=record.notes,
        figure_refs=record.figure_refs,
        image_refs=record.image_refs,
        source_refs=record.source_refs,
    )


def verify_table_fact_binding_sha256(binding: TableFactBindingV1) -> bool:
    return binding.fact_binding_sha256 == stable_table_fact_binding_sha256(
        source_ir_id=binding.source_ir_id,
        source_sha256=binding.source_sha256,
        source_ir_canonical_sha256=binding.source_ir_canonical_sha256,
        structure_snapshot_id=binding.structure_snapshot_id,
        logical_table_id=binding.logical_table_id,
        owner_structure_node_id=binding.owner_structure_node_id,
        table_parts=binding.table_parts,
        whole_table_source_refs=binding.whole_table_source_refs,
    )


def verify_table_record_projection_sha256(
    projection: TableRecordProjectionV1,
) -> bool:
    return (
        projection.projection_sha256
        == stable_table_record_projection_sha256(
            table_fact=projection.table_fact,
            mode=projection.mode,
            projection_revision=projection.projection_revision,
            records=projection.records,
        )
    )


def _normalize_visual_refs(
    refs: tuple[TableRecordVisualRefV1, ...],
    table_order: dict[str, int],
) -> tuple[TableRecordVisualRefV1, ...]:
    return tuple(
        sorted(
            refs,
            key=lambda item: (
                table_order.get(item.table_id, len(table_order)),
                item.cell_id or "",
                item.asset_id,
            ),
        )
    )


def _record_used_table_ids(
    *,
    row_bindings: tuple[TableRecordRowBindingV1, ...],
    column_bindings: tuple[TableRecordColumnBindingV1, ...],
    header_bindings: tuple[TableRecordHeaderBindingV1, ...],
    shared_merged_values: tuple[TableRecordSharedMergedValueV1, ...],
    notes: tuple[TableRecordNoteV1, ...],
    figure_refs: tuple[TableRecordVisualRefV1, ...],
    image_refs: tuple[TableRecordVisualRefV1, ...],
) -> tuple[str, ...]:
    return tuple(
        {
            *[item.table_id for item in row_bindings],
            *[item.table_id for item in column_bindings],
            *[item.table_id for item in header_bindings],
            *[item.table_id for item in shared_merged_values],
            *[item.table_id for item in notes],
            *[item.table_id for item in figure_refs],
            *[item.table_id for item in image_refs],
        }
    )


def _require_known_cells(
    cell_ids: tuple[str, ...],
    part: TableFactPartV1,
    field_name: str,
) -> None:
    if not set(cell_ids).issubset(part.cell_ids):
        raise ValueError(f"{field_name} references cells outside its table fact")


def _require_unique_coordinates(
    coordinates: Iterable[tuple[str, int]],
    field_name: str,
) -> None:
    values = tuple(coordinates)
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must use unique coordinates")


def _require_unique_non_blank(
    values: tuple[str, ...],
    field_name: str,
) -> tuple[str, ...]:
    if any(not value.strip() for value in values):
        raise ValueError(f"{field_name} must not contain blank values")
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return values


def _require_non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _require_sha256(value: str, field_name: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}-{_sha256(value)[:32]}"


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        _json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


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


__all__ = [
    "TABLE_RECORD_PROJECTION_SCHEMA_VERSION",
    "TableFactBindingV1",
    "TableFactPartV1",
    "TableRecordColumnBindingV1",
    "TableRecordHeaderAxis",
    "TableRecordHeaderBindingV1",
    "TableRecordKind",
    "TableRecordNoteV1",
    "TableRecordProjectionMode",
    "TableRecordProjectionDecisionOrigin",
    "TableRecordProjectionDecisionV1",
    "TableRecordProjectionV1",
    "TableRecordRowBindingV1",
    "TableRecordSharedMergedValueV1",
    "TableRecordV1",
    "TableRecordVisualRefV1",
    "build_table_fact_binding",
    "build_table_record",
    "build_table_record_projection",
    "normalize_table_record_projection_records",
    "normalize_table_record_source_refs",
    "stable_table_fact_binding_sha256",
    "stable_table_record_content_sha256",
    "stable_table_record_id",
    "stable_table_record_projection_id",
    "stable_table_record_projection_sha256",
    "verify_table_fact_binding_sha256",
    "verify_table_record_content_sha256",
    "verify_table_record_projection_sha256",
]
