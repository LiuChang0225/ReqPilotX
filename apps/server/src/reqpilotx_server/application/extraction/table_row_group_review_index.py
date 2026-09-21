from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from reqpilotx_server.application.extraction.structure_snapshot import structure_snapshot_id
from reqpilotx_server.application.extraction.table_record_projection import (
    TABLE_RECORD_PROJECTION_RUNTIME_VERSION,
    build_adopted_table_projection_catalog,
    build_manual_row_group_projection,
    propose_manual_row_groups,
)
from reqpilotx_server.domain.extraction.source_ir import SourceIRSnapshot
from reqpilotx_server.domain.extraction.structure_revision import EffectiveStructureSnapshotV1
from reqpilotx_server.domain.extraction.table_records import (
    TableRecordKind,
    TableRecordProjectionMode,
    TableRecordProjectionV1,
)


TABLE_ROW_GROUP_REVIEW_INDEX_KEY = "table_row_group_review_index"
TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION = (
    "table-row-group-review-index/v1"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INDEX_KEYS = frozenset(
    {
        "schema_version",
        "runtime_version",
        "source_ir_id",
        "source_sha256",
        "source_ir_canonical_sha256",
        "structure_snapshot_id",
        "entries",
        "canonical_sha256",
    }
)
_ENTRY_KEYS = frozenset(
    {
        "logical_table_id",
        "automatic_projection_id",
        "automatic_projection_sha256",
        "fact_binding_sha256",
        "eligible_rows",
        "suggested_row_groups",
        "editable",
        "read_only_reason",
    }
)


def build_table_row_group_review_index(
    source_ir: SourceIRSnapshot,
    structure: EffectiveStructureSnapshotV1,
    *,
    automatic_projections: tuple[TableRecordProjectionV1, ...],
) -> dict[str, Any]:
    """Build a compact restart-safe read model for the row-group dialog.

    The index is only a read optimization.  A mutation still rebuilds the
    complete SourceIR and effective Structure before validating the requested
    partition and committing it with the projection CAS.
    """

    expected_structure_snapshot_id = structure_snapshot_id(structure)
    source_ir_canonical_sha256 = _source_ir_canonical_sha256(source_ir)
    projected_tables_by_id = build_adopted_table_projection_catalog(
        source_ir,
        structure,
    )
    table_catalog = {
        node.table_payload.table_id: (
            projected_tables_by_id[node.table_payload.table_id],
            node,
        )
        for node in source_ir.nodes
        if node.table_payload is not None
    }
    entries: list[dict[str, Any]] = []
    for projection in automatic_projections:
        fact = projection.table_fact
        if (
            fact.source_ir_id != source_ir.source_ir_id
            or fact.source_sha256 != source_ir.source_sha256
            or fact.source_ir_canonical_sha256
            != source_ir_canonical_sha256
            or fact.structure_snapshot_id
            != expected_structure_snapshot_id
        ):
            raise ValueError(
                "table row-group review index requires source-bound projections"
            )

        matrix_projection = (
            projection.mode is TableRecordProjectionMode.RECORDS
            and any(
                record.kind is not TableRecordKind.ROW
                for record in projection.records
            )
        )
        if matrix_projection:
            suggested_coordinates: tuple[
                tuple[tuple[str, int], ...], ...
            ] = ()
            editable = False
            read_only_reason = (
                "当前为矩阵交点投影，需要行列表头编辑器，不能按物理行改组。"
            )
        else:
            try:
                suggested = propose_manual_row_groups(
                    source_ir,
                    structure,
                    table_structure_node_id=fact.logical_table_id,
                )
                if suggested:
                    build_manual_row_group_projection(
                        source_ir,
                        structure,
                        table_structure_node_id=fact.logical_table_id,
                        row_groups=suggested,
                        projection_revision=projection.projection_revision,
                    )
            except ValueError:
                # The optimization must not make extraction fail.  Omitting a
                # table preserves the existing fail-closed rebuild path.
                continue
            suggested_coordinates = tuple(
                group.row_coordinates for group in suggested
            )
            editable = bool(suggested_coordinates)
            read_only_reason = (
                None if editable else "当前表格没有可分组的业务行。"
            )

        eligible_coordinates = tuple(
            coordinate
            for group in suggested_coordinates
            for coordinate in group
        )
        entries.append(
            {
                "logical_table_id": fact.logical_table_id,
                "automatic_projection_id": projection.projection_id,
                "automatic_projection_sha256": projection.projection_sha256,
                "fact_binding_sha256": fact.fact_binding_sha256,
                "eligible_rows": [
                    _serialize_eligible_row(table_catalog, coordinate)
                    for coordinate in eligible_coordinates
                ],
                "suggested_row_groups": _serialize_groups(
                    suggested_coordinates
                ),
                "editable": editable,
                "read_only_reason": read_only_reason,
            }
        )

    payload: dict[str, Any] = {
        "schema_version": TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION,
        "runtime_version": TABLE_RECORD_PROJECTION_RUNTIME_VERSION,
        "source_ir_id": source_ir.source_ir_id,
        "source_sha256": source_ir.source_sha256,
        "source_ir_canonical_sha256": source_ir_canonical_sha256,
        "structure_snapshot_id": expected_structure_snapshot_id,
        "entries": entries,
    }
    payload["canonical_sha256"] = _canonical_json_sha256(payload)
    return payload


def validated_table_row_group_review_entry(
    value: Any,
    *,
    automatic_projection: TableRecordProjectionV1,
) -> dict[str, Any] | None:
    """Return one exact-bound entry or reject the durable index as a whole."""

    index = value if isinstance(value, dict) else {}
    if (
        set(index) != _INDEX_KEYS
        or index.get("schema_version")
        != TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION
        or index.get("runtime_version")
        != TABLE_RECORD_PROJECTION_RUNTIME_VERSION
        or _SHA256_PATTERN.fullmatch(
            str(index.get("canonical_sha256") or "")
        )
        is None
    ):
        return None
    unsigned = {
        key: item for key, item in index.items() if key != "canonical_sha256"
    }
    try:
        if _canonical_json_sha256(unsigned) != index["canonical_sha256"]:
            return None
    except (TypeError, ValueError):
        return None

    fact = automatic_projection.table_fact
    if (
        index.get("source_ir_id") != fact.source_ir_id
        or index.get("source_sha256") != fact.source_sha256
        or index.get("source_ir_canonical_sha256")
        != fact.source_ir_canonical_sha256
        or index.get("structure_snapshot_id")
        != fact.structure_snapshot_id
        or not isinstance(index.get("entries"), list)
    ):
        return None

    target: dict[str, Any] | None = None
    logical_table_ids: set[str] = set()
    for raw_entry in index["entries"]:
        if not isinstance(raw_entry, dict) or set(raw_entry) != _ENTRY_KEYS:
            return None
        logical_table_id = raw_entry.get("logical_table_id")
        if (
            not isinstance(logical_table_id, str)
            or not logical_table_id
            or logical_table_id in logical_table_ids
        ):
            return None
        logical_table_ids.add(logical_table_id)
        if logical_table_id == fact.logical_table_id:
            target = raw_entry
    if target is None:
        return None
    if (
        target.get("automatic_projection_id")
        != automatic_projection.projection_id
        or target.get("automatic_projection_sha256")
        != automatic_projection.projection_sha256
        or target.get("fact_binding_sha256") != fact.fact_binding_sha256
    ):
        return None

    part_order = {
        part.table_id: part_index
        for part_index, part in enumerate(fact.table_parts)
    }
    part_by_id = {part.table_id: part for part in fact.table_parts}
    eligible_rows = target.get("eligible_rows")
    suggested_groups = target.get("suggested_row_groups")
    if not isinstance(eligible_rows, list) or not isinstance(
        suggested_groups,
        list,
    ):
        return None
    coordinates: list[tuple[str, int]] = []
    for row in eligible_rows:
        coordinate = _validated_eligible_row(row, part_by_id=part_by_id)
        if coordinate is None:
            return None
        coordinates.append(coordinate)
    if len(coordinates) != len(set(coordinates)):
        return None
    if coordinates != sorted(
        coordinates,
        key=lambda item: (part_order[item[0]], item[1]),
    ):
        return None
    parsed_groups = _validated_groups(
        suggested_groups,
        part_by_id=part_by_id,
    )
    if parsed_groups is None or [
        coordinate
        for group in parsed_groups
        for coordinate in group
    ] != coordinates:
        return None
    editable = target.get("editable")
    read_only_reason = target.get("read_only_reason")
    if (
        not isinstance(editable, bool)
        or editable != bool(parsed_groups)
        or (editable and read_only_reason is not None)
        or (
            not editable
            and (
                not isinstance(read_only_reason, str)
                or not read_only_reason.strip()
            )
        )
    ):
        return None
    return target


def _source_ir_canonical_sha256(source_ir: SourceIRSnapshot) -> str:
    payload = source_ir.model_dump(mode="json", exclude={"created_at"})
    return _canonical_json_sha256(payload)


def _canonical_json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _serialize_eligible_row(
    table_catalog: dict[str, tuple[Any, Any]],
    coordinate: tuple[str, int],
) -> dict[str, Any]:
    table_id, row_index = coordinate
    table_item = table_catalog.get(table_id)
    values: list[str] = []
    page_number: int | None = None
    if table_item is not None:
        table, source_node = table_item
        page_number = source_node.native_locator.page_number
        for cell in sorted(
            (
                cell
                for cell in table.cells
                if cell.row <= row_index < cell.row + cell.row_span
            ),
            key=lambda cell: (cell.column, cell.row, cell.cell_id),
        ):
            value = (
                cell.display_value
                or cell.raw_text
                or cell.formula
                or ""
            ).strip()
            if value and value not in values:
                values.append(value)
    display_text = " · ".join(values)
    if len(display_text) > 240:
        display_text = display_text[:237].rstrip() + "..."
    return {
        "table_id": table_id,
        "row_index": row_index,
        "page_number": page_number,
        "display_text": display_text,
    }


def _serialize_groups(
    groups: tuple[tuple[tuple[str, int], ...], ...],
) -> list[dict[str, Any]]:
    return [
        {
            "rows": [
                {"table_id": table_id, "row_index": row_index}
                for table_id, row_index in group
            ]
        }
        for group in groups
    ]


def _validated_eligible_row(
    value: Any,
    *,
    part_by_id: dict[str, Any],
) -> tuple[str, int] | None:
    if not isinstance(value, dict) or set(value) != {
        "table_id",
        "row_index",
        "page_number",
        "display_text",
    }:
        return None
    table_id = value.get("table_id")
    row_index = value.get("row_index")
    page_number = value.get("page_number")
    display_text = value.get("display_text")
    part = part_by_id.get(table_id) if isinstance(table_id, str) else None
    if (
        part is None
        or not isinstance(row_index, int)
        or isinstance(row_index, bool)
        or row_index < 0
        or row_index >= part.row_count
        or (
            page_number is not None
            and (
                not isinstance(page_number, int)
                or isinstance(page_number, bool)
                or page_number < 1
            )
        )
        or not isinstance(display_text, str)
        or len(display_text) > 240
    ):
        return None
    return table_id, row_index


def _validated_groups(
    value: list[Any],
    *,
    part_by_id: dict[str, Any],
) -> tuple[tuple[tuple[str, int], ...], ...] | None:
    parsed: list[tuple[tuple[str, int], ...]] = []
    for raw_group in value:
        if (
            not isinstance(raw_group, dict)
            or set(raw_group) != {"rows"}
            or not isinstance(raw_group.get("rows"), list)
            or not raw_group["rows"]
        ):
            return None
        coordinates: list[tuple[str, int]] = []
        for raw_coordinate in raw_group["rows"]:
            if not isinstance(raw_coordinate, dict) or set(raw_coordinate) != {
                "table_id",
                "row_index",
            }:
                return None
            table_id = raw_coordinate.get("table_id")
            row_index = raw_coordinate.get("row_index")
            part = part_by_id.get(table_id) if isinstance(table_id, str) else None
            if (
                part is None
                or not isinstance(row_index, int)
                or isinstance(row_index, bool)
                or row_index < 0
                or row_index >= part.row_count
            ):
                return None
            coordinates.append((table_id, row_index))
        if len(coordinates) != len(set(coordinates)):
            return None
        parsed.append(tuple(coordinates))
    return tuple(parsed)


__all__ = [
    "TABLE_ROW_GROUP_REVIEW_INDEX_KEY",
    "TABLE_ROW_GROUP_REVIEW_INDEX_SCHEMA_VERSION",
    "build_table_row_group_review_index",
    "validated_table_row_group_review_entry",
]
