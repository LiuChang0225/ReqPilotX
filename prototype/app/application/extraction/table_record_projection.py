from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass

from app.application.extraction.resolve_structure import (
    visual_text_authority_pages,
)
from app.domain.extraction.source_ir import (
    MergedRegion,
    SourceAssetKind,
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
)
from app.domain.extraction.structure import (
    StructureBoundaryBasis,
    StructureNode,
    StructureNodeKind,
    StructureRelation,
    StructureRelationKind,
    StructureSourceRef,
    StructureSourceRefKind,
    StructureTreeSnapshot,
)
from app.domain.extraction.structure_revision import (
    EffectiveStructureSnapshotV1,
    canonical_model_sha256,
)
from app.domain.extraction.table_records import (
    TableFactBindingV1,
    TableFactPartV1,
    TableRecordColumnBindingV1,
    TableRecordHeaderAxis,
    TableRecordHeaderBindingV1,
    TableRecordKind,
    TableRecordNoteV1,
    TableRecordProjectionDecisionOrigin,
    TableRecordProjectionDecisionV1,
    TableRecordProjectionMode,
    TableRecordProjectionV1,
    TableRecordRowBindingV1,
    TableRecordSharedMergedValueV1,
    TableRecordV1,
    TableRecordVisualRefV1,
    build_table_fact_binding,
    build_table_record,
    build_table_record_projection,
    normalize_table_record_source_refs,
)


TABLE_RECORD_PROJECTION_RUNTIME_VERSION = "1.4.3"

StructureSnapshot = StructureTreeSnapshot | EffectiveStructureSnapshotV1


@dataclass(frozen=True, slots=True)
class ManualMatrixIntersection:
    """One user-confirmed matrix cell and its explicit axis headers.

    Matrix topology is never inferred by the automatic projection.  A caller
    must provide both axis header identities before an intersection can become
    a structural record.
    """

    table_id: str
    row_index: int
    column_index: int
    row_header_cell_ids: tuple[str, ...]
    column_header_cell_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ManualRowGroup:
    """One user-selected structural record made from native table rows.

    Coordinates may span physical table parts in the same logical
    continuation chain.  Grouping changes only the derived projection; the
    immutable ``TableIR`` facts remain untouched.
    """

    row_coordinates: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class _PhysicalTablePart:
    table_structure_node: StructureNode
    source_node: SourceNode
    source_table: TableIR
    table: TableIR


@dataclass(frozen=True, slots=True)
class _LogicalTable:
    table_nodes: tuple[StructureNode, ...]
    parts: tuple[_PhysicalTablePart, ...]
    fact: TableFactBindingV1


@dataclass(frozen=True, slots=True)
class _SchemaEvent:
    part: _PhysicalTablePart
    start_row: int
    end_row: int
    key_column: int
    headers: tuple[TableRecordHeaderBindingV1, ...]


@dataclass(frozen=True, slots=True)
class _AutomaticRowGroupPlan:
    row_groups: tuple[tuple[tuple[str, int], ...], ...]
    headers_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordHeaderBindingV1, ...],
    ]
    reason_codes: tuple[str, ...]
    evidence: tuple[str, ...]
    inherited_shared_values_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordSharedMergedValueV1, ...],
    ]
    whole_table_reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class _FactCoverageFailure:
    unconsumed_business_cell_ids: tuple[str, ...]
    multiply_consumed_business_cell_ids: tuple[str, ...]
    multiply_consumed_business_rows: tuple[tuple[str, int], ...]
    unconsumed_visual_placement_ids: tuple[str, ...]
    multiply_consumed_visual_placement_ids: tuple[str, ...]

    @property
    def evidence(self) -> tuple[str, ...]:
        result = [
            "自动记录完整覆盖校验失败："
            f"未归属业务单元格 {len(self.unconsumed_business_cell_ids)} 个，"
            "重复归属业务单元格 "
            f"{len(self.multiply_consumed_business_cell_ids)} 个，"
            "重复归属业务行 "
            f"{len(self.multiply_consumed_business_rows)} 个",
        ]
        if (
            self.unconsumed_visual_placement_ids
            or self.multiply_consumed_visual_placement_ids
        ):
            result.append(
                "图片/图形 placement 覆盖失败："
                "未归属 "
                f"{len(self.unconsumed_visual_placement_ids)} 个，"
                "重复归属 "
                f"{len(self.multiply_consumed_visual_placement_ids)} 个"
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class _HierarchicalLeafTopology:
    header_rows: tuple[int, ...]
    leaf_column: int


_STUB_HEADER_LABELS = frozenset(
    {
        "序号",
        "编号",
        "项次",
        "项号",
        "条款号",
        "要求编号",
        "需求编号",
        "项目编号",
        "no",
        "number",
        "id",
        "itemno",
        "itemnumber",
        "position",
        "pos",
    }
)
_ORDINAL_CELL_PATTERN = re.compile(r"^\s*(\d+)\s*[.．、]?\s*$")
_PLACEHOLDER_KEY_PATTERN = re.compile(
    r"^[\s.…⋯·•．。—–_\-/\\]+$"
)
_ELLIPSIS_PLACEHOLDER_PATTERN = re.compile(
    r"^(?:\.{3,}|…+|⋯+|。{3,})$"
)
_FULL_WIDTH_NUMBERED_BOUNDARY = re.compile(
    r"^\s*[1-9][0-9]{0,2}"
    r"(?:\s*[.．]\s*(?:0|[1-9][0-9]{0,2})){0,7}"
    r"\s*[)）]\s*\S"
)
_APPENDIX_BOUNDARY = re.compile(
    r"^\s*(?:附录|附件)\s*[A-Z0-9一二三四五六七八九十]{0,3}"
    r"(?:\s*[（(]?(?:规范性|资料性)[）)]?)?"
    r"(?:\s*[-—:：]?\s*\S{0,100})?\s*$",
    re.IGNORECASE,
)
_ENGLISH_APPENDIX_BOUNDARY = re.compile(
    r"^\s*(?:appendix|annex)\s+[A-Z0-9]{1,3}"
    r"(?:\s*[-—:：]?\s*\S{0,100})?\s*$",
    re.IGNORECASE,
)


def build_adopted_table_projection_catalog(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
) -> dict[str, TableIR]:
    """Return table topology with only the adopted text channel exposed.

    SourceIR remains immutable and continues to bind every table fact/hash to
    the native payload.  This projection-only view changes text fields on OCR
    authority pages: an exact visual-cell binding supplies ``raw_text`` (an
    explicit visual blank stays blank), while an unbound cell exposes no text
    at all.  Native ``display_value`` and ``formula`` are never fallback text
    on those pages.  Tables on native-authority pages are returned unchanged.
    """

    _validate_inputs(source_ir, structure)
    authority_pages = visual_text_authority_pages(source_ir)
    if not authority_pages:
        return {
            node.table_payload.table_id: node.table_payload
            for node in source_ir.nodes
            if node.table_payload is not None
        }

    visual_cells_by_id = {
        cell.visual_table_cell_id: cell
        for snapshot in source_ir.visual_table_snapshots
        for cell in snapshot.cells
    }
    adopted_visual_text_by_native_cell_id = {
        binding.table_cell_id: visual_cells_by_id[
            binding.visual_table_cell_id
        ].raw_text
        for binding in structure.visual_table_cell_text_bindings
        if binding.visual_table_cell_id in visual_cells_by_id
    }
    catalog: dict[str, TableIR] = {}
    for node in source_ir.nodes:
        source_table = node.table_payload
        if source_table is None:
            continue
        projected_cells: list[TableCellIR] = []
        has_authority_cell = False
        for cell in source_table.cells:
            page_number = (
                cell.native_locator.page_number
                or node.native_locator.page_number
            )
            if page_number not in authority_pages:
                projected_cells.append(cell)
                continue
            has_authority_cell = True
            projected_cells.append(
                cell.model_copy(
                    update={
                        "raw_text": adopted_visual_text_by_native_cell_id.get(
                            cell.cell_id,
                            "",
                        ),
                        "display_value": None,
                        "formula": None,
                    }
                )
            )
        table_page_is_authority = (
            node.native_locator.page_number in authority_pages
        )
        if not has_authority_cell and not table_page_is_authority:
            catalog[source_table.table_id] = source_table
            continue
        catalog[source_table.table_id] = source_table.model_copy(
            update={
                "cells": tuple(projected_cells),
                # TableIR notes have no exact visual-cell identity.  Keeping
                # them would be the same forbidden native-text fallback.
                "notes": (),
            }
        )
    return catalog


def build_table_record_projections(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
) -> tuple[TableRecordProjectionV1, ...]:
    """Build deterministic, non-semantic record projections for all tables.

    The default is deliberately ``WHOLE_TABLE``.  Automatic row records are
    exposed only when either the Structure layer already proves an explicit
    numbered boundary below a table row, or the source carries declared column
    headers and one stable non-empty key column across every data row.
    """

    _validate_inputs(source_ir, structure)
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    children_by_parent = _children_by_parent(structure.nodes)
    logical_tables = _build_logical_tables(
        source_ir=source_ir,
        structure=structure,
        source_nodes_by_id=source_nodes_by_id,
    )

    projections = tuple(
        _build_automatic_projection(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            source_nodes_by_id=source_nodes_by_id,
            children_by_parent=children_by_parent,
        )
        for logical_table in logical_tables
    )
    return tuple(
        sorted(
            projections,
            key=lambda item: (
                item.table_fact.table_parts[0].source_order,
                item.table_fact.logical_table_id,
            ),
        )
    )


def build_manual_matrix_intersection_projection(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    table_structure_node_id: str,
    intersections: tuple[ManualMatrixIntersection, ...],
    projection_revision: int | None = None,
) -> TableRecordProjectionV1:
    """Build an explicit matrix projection without guessing matrix semantics."""

    if not intersections:
        raise ValueError("manual matrix projection requires at least one intersection")
    _validate_inputs(source_ir, structure)
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    logical_tables = _build_logical_tables(
        source_ir=source_ir,
        structure=structure,
        source_nodes_by_id=source_nodes_by_id,
    )
    logical_table = next(
        (
            item
            for item in logical_tables
            if any(
                node.structure_node_id == table_structure_node_id
                for node in item.table_nodes
            )
        ),
        None,
    )
    if logical_table is None:
        raise ValueError("table_structure_node_id does not identify a source table")

    part_by_table_id = {
        part.table.table_id: part for part in logical_table.parts
    }
    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    records: list[TableRecordV1] = []
    for source_order, intersection in enumerate(intersections):
        part = part_by_table_id.get(intersection.table_id)
        if part is None:
            raise ValueError("manual intersection references another logical table")
        if (
            intersection.row_index < 0
            or intersection.row_index >= part.table.row_count
            or intersection.column_index < 0
            or intersection.column_index >= part.table.column_count
        ):
            raise ValueError("manual intersection falls outside the table")

        target_cell = _cell_covering(
            part.table,
            intersection.row_index,
            intersection.column_index,
        )
        if target_cell is None:
            raise ValueError("manual intersection requires a native source cell")
        row_headers = _manual_header_bindings(
            part=part,
            cell_ids=intersection.row_header_cell_ids,
            axis=TableRecordHeaderAxis.ROW,
        )
        column_headers = _manual_header_bindings(
            part=part,
            cell_ids=intersection.column_header_cell_ids,
            axis=TableRecordHeaderAxis.COLUMN,
        )
        if not row_headers or not column_headers:
            raise ValueError(
                "manual matrix intersections require both explicit header axes"
            )

        row_cells = _cells_covering_row(part.table, intersection.row_index)
        row_node = row_node_by_coordinate.get(
            (part.table.table_id, intersection.row_index)
        )
        shared_values = _shared_values_for_row(
            structure=structure,
            logical_table=logical_table,
            row_node=row_node,
        )
        notes = _notes_for_row(
            structure=structure,
            logical_table=logical_table,
            row_node=row_node,
        )
        figure_refs, image_refs = _visual_refs_for_row(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            part=part,
            row_index=intersection.row_index,
            row_node=row_node,
            source_nodes_by_id=source_nodes_by_id,
        )
        source_refs = _record_source_refs(
            part=part,
            row_node=row_node,
            row_cells=row_cells,
            headers=(*row_headers, *column_headers),
            shared_values=shared_values,
            notes=notes,
            figure_refs=figure_refs,
            image_refs=image_refs,
        )
        records.append(
            build_table_record(
                table_fact=logical_table.fact,
                kind=TableRecordKind.MATRIX_INTERSECTION,
                source_order=source_order,
                row_bindings=(
                    TableRecordRowBindingV1(
                        table_id=part.table.table_id,
                        row_index=intersection.row_index,
                        cell_ids=tuple(cell.cell_id for cell in row_cells),
                    ),
                ),
                column_bindings=(
                    TableRecordColumnBindingV1(
                        table_id=part.table.table_id,
                        column_index=intersection.column_index,
                        cell_ids=(target_cell.cell_id,),
                    ),
                ),
                header_bindings=(*row_headers, *column_headers),
                shared_merged_values=shared_values,
                notes=notes,
                figure_refs=figure_refs,
                image_refs=image_refs,
                source_refs=source_refs,
            )
        )

    return build_table_record_projection(
        table_fact=logical_table.fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=(
            _projection_revision(structure)
            if projection_revision is None
            else projection_revision
        ),
        records=tuple(records),
        decision=_projection_decision(
            origin=TableRecordProjectionDecisionOrigin.MANUAL,
            mode=TableRecordProjectionMode.RECORDS,
            reason_code="MANUAL_MATRIX_INTERSECTIONS",
            message="已按人工确认的行列交点拆分表格。",
            evidence=(
                f"已确认 {len(records)} 个矩阵交点",
                "每个交点均绑定行表头和列表头",
            ),
        ),
    )


def build_manual_row_group_projection(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    table_structure_node_id: str,
    row_groups: tuple[ManualRowGroup, ...],
    projection_revision: int | None = None,
    decision_origin: TableRecordProjectionDecisionOrigin = (
        TableRecordProjectionDecisionOrigin.MANUAL
    ),
    decision_evidence: tuple[str, ...] = (),
) -> TableRecordProjectionV1:
    """Build an explicit row-group projection over one logical table.

    An empty ``row_groups`` tuple is the keep-whole operation: the source
    table remains complete and exposes no derived records.  Non-empty groups
    may combine adjacent rows from one part or cross-page rows from a verified
    continuation chain. ``AGENT`` origin is accepted only for already-validated
    source-bound parent groups; this function never infers those boundaries.
    """

    _validate_inputs(source_ir, structure)
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    logical_table = _logical_table_for_structure_node(
        source_ir=source_ir,
        structure=structure,
        source_nodes_by_id=source_nodes_by_id,
        table_structure_node_id=table_structure_node_id,
    )
    resolved_revision = (
        _projection_revision(structure)
        if projection_revision is None
        else projection_revision
    )
    if not row_groups:
        return build_table_record_projection(
            table_fact=logical_table.fact,
            mode=TableRecordProjectionMode.WHOLE_TABLE,
            projection_revision=resolved_revision,
            decision=_projection_decision(
                origin=decision_origin,
                mode=TableRecordProjectionMode.WHOLE_TABLE,
                reason_code=(
                    "AGENT_KEEP_WHOLE_TABLE"
                    if decision_origin is TableRecordProjectionDecisionOrigin.AGENT
                    else "MANUAL_KEEP_WHOLE_TABLE"
                ),
                message=(
                    "已按 Agent 识别并验证的父边界保留完整表格。"
                    if decision_origin is TableRecordProjectionDecisionOrigin.AGENT
                    else "已按人工选择合并为完整表格。"
                ),
                evidence=("未生成独立行记录", *decision_evidence),
            ),
        )

    normalized_groups = _normalize_manual_row_groups(
        logical_table=logical_table,
        row_groups=row_groups,
    )
    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    _validate_manual_row_groups_are_records(
        logical_table=logical_table,
        structure=structure,
        row_groups=normalized_groups,
        row_node_by_coordinate=row_node_by_coordinate,
    )
    headers_by_table = _manual_header_catalog(logical_table)
    eligible_rows = _manual_eligible_rows(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
        source_nodes_by_id=source_nodes_by_id,
    )
    eligible_coordinates = tuple(
        (part.table.table_id, row_index)
        for part, row_index in eligible_rows
    )
    flattened_coordinates = tuple(
        coordinate
        for group in normalized_groups
        for coordinate in group
    )
    if flattened_coordinates != eligible_coordinates:
        raise ValueError(
            "manual row groups must form one complete, ordered and contiguous "
            "partition of every eligible source row"
        )
    first_headers = next(
        (headers for headers in headers_by_table.values() if headers),
        (),
    )
    headers_by_coordinate = {
        coordinate: _headers_for_row_group(
            coordinates=(coordinate,),
            headers_by_table=headers_by_table,
            first_headers=first_headers,
        )
        for coordinate in eligible_coordinates
    }
    coverage_failure = _fact_coverage_failure(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        row_groups=normalized_groups,
        header_rows_by_part=_header_rows_by_part(logical_table),
        row_node_by_coordinate=row_node_by_coordinate,
        headers_by_coordinate=headers_by_coordinate,
        inherited_shared_values_by_coordinate={},
        source_nodes_by_id=source_nodes_by_id,
    )
    if coverage_failure is not None:
        raise ValueError(
            "manual row groups do not exactly cover the table facts: "
            + "; ".join(coverage_failure.evidence)
        )
    records = tuple(
        _build_row_group_record(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            coordinates=coordinates,
            headers=_headers_for_row_group(
                coordinates=coordinates,
                headers_by_table=headers_by_table,
                first_headers=first_headers,
            ),
            source_order=source_order,
            source_nodes_by_id=source_nodes_by_id,
            row_node_by_coordinate=row_node_by_coordinate,
        )
        for source_order, coordinates in enumerate(normalized_groups)
    )
    return build_table_record_projection(
        table_fact=logical_table.fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=resolved_revision,
        records=records,
        decision=_projection_decision(
            origin=decision_origin,
            mode=TableRecordProjectionMode.RECORDS,
            reason_code=(
                "AGENT_PARENT_ROW_GROUPS"
                if decision_origin is TableRecordProjectionDecisionOrigin.AGENT
                else "MANUAL_ROW_GROUPS"
            ),
            message=(
                "已按 Agent 识别并验证的父条款行组拆分表格。"
                if decision_origin is TableRecordProjectionDecisionOrigin.AGENT
                else "已按人工确认的行组拆分表格。"
            ),
            evidence=(
                f"已确认 {len(records)} 个行组",
                f"共绑定 {sum(len(group) for group in normalized_groups)} 个原表行",
                *decision_evidence,
            ),
        ),
    )


def propose_manual_row_groups(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    table_structure_node_id: str,
) -> tuple[ManualRowGroup, ...]:
    """Propose topology-only row groups for the manual correction surface.

    Proven automatic groups are returned unchanged.  When automatic proof is
    intentionally insufficient, the fallback proposes one visible business
    row per group and attaches a blank-key continuation row to the preceding
    group.  It never classifies a row as a requirement.
    """

    _validate_inputs(source_ir, structure)
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    children_by_parent = _children_by_parent(structure.nodes)
    logical_table = _logical_table_for_structure_node(
        source_ir=source_ir,
        structure=structure,
        source_nodes_by_id=source_nodes_by_id,
        table_structure_node_id=table_structure_node_id,
    )
    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    data_rows = _manual_eligible_rows(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
        source_nodes_by_id=source_nodes_by_id,
    )
    eligible_coordinates = tuple(
        (part.table.table_id, row_index)
        for part, row_index in data_rows
    )
    automatic = _automatic_row_group_plan(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        children_by_parent=children_by_parent,
        source_nodes_by_id=source_nodes_by_id,
    )
    if (
        automatic.row_groups
        and tuple(
            coordinate
            for group in automatic.row_groups
            for coordinate in group
        )
        == eligible_coordinates
    ):
        return tuple(
            ManualRowGroup(row_coordinates=group)
            for group in automatic.row_groups
        )
    groups = _fallback_manual_row_groups(
        logical_table=logical_table,
        data_rows=data_rows,
    )
    return tuple(
        ManualRowGroup(row_coordinates=group)
        for group in groups
    )


def manual_row_group_eligible_coordinates(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    *,
    table_structure_node_id: str,
) -> tuple[tuple[str, int], ...]:
    """Expose the immutable row domain consumed by an explicit partition.

    This helper does not infer record or parent boundaries.  It only returns
    the physical business-row coordinates that the existing deterministic
    materializer requires an explicit partition to cover.  Callers such as an
    Agent-boundary adapter can therefore validate exact coverage without
    treating the automatic/fallback grouping proposal as authoritative.
    """

    _validate_inputs(source_ir, structure)
    source_nodes_by_id = {
        node.source_node_id: node for node in source_ir.nodes
    }
    logical_table = _logical_table_for_structure_node(
        source_ir=source_ir,
        structure=structure,
        source_nodes_by_id=source_nodes_by_id,
        table_structure_node_id=table_structure_node_id,
    )
    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    return tuple(
        (part.table.table_id, row_index)
        for part, row_index in _manual_eligible_rows(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            row_node_by_coordinate=row_node_by_coordinate,
            source_nodes_by_id=source_nodes_by_id,
        )
    )


def _projection_decision(
    *,
    origin: TableRecordProjectionDecisionOrigin,
    mode: TableRecordProjectionMode,
    reason_code: str,
    message: str,
    evidence: tuple[str, ...] = (),
) -> TableRecordProjectionDecisionV1:
    return TableRecordProjectionDecisionV1(
        origin=origin,
        mode=mode,
        reason_code=reason_code,
        message=message,
        evidence=evidence,
    )


def _logical_table_for_structure_node(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    source_nodes_by_id: dict[str, SourceNode],
    table_structure_node_id: str,
) -> _LogicalTable:
    logical_table = next(
        (
            item
            for item in _build_logical_tables(
                source_ir=source_ir,
                structure=structure,
                source_nodes_by_id=source_nodes_by_id,
            )
            if any(
                node.structure_node_id == table_structure_node_id
                for node in item.table_nodes
            )
        ),
        None,
    )
    if logical_table is None:
        raise ValueError("table_structure_node_id does not identify a source table")
    return logical_table


def _normalize_manual_row_groups(
    *,
    logical_table: _LogicalTable,
    row_groups: tuple[ManualRowGroup, ...],
) -> tuple[tuple[tuple[str, int], ...], ...]:
    part_by_table_id = {
        part.table.table_id: part for part in logical_table.parts
    }
    seen: set[tuple[str, int]] = set()
    normalized: list[tuple[tuple[str, int], ...]] = []
    for group in row_groups:
        if not group.row_coordinates:
            raise ValueError("manual row groups must not be empty")
        if len(group.row_coordinates) != len(set(group.row_coordinates)):
            raise ValueError("manual row groups must not repeat a source row")
        coordinates: list[tuple[str, int]] = []
        for table_id, row_index in group.row_coordinates:
            part = part_by_table_id.get(table_id)
            if part is None:
                raise ValueError("manual row group references another logical table")
            if (
                isinstance(row_index, bool)
                or not isinstance(row_index, int)
                or row_index < 0
                or row_index >= part.table.row_count
            ):
                raise ValueError("manual row group falls outside the table")
            coordinate = (table_id, row_index)
            if coordinate in seen:
                raise ValueError(
                    "a source row may belong to only one manual row group"
                )
            seen.add(coordinate)
            coordinates.append(coordinate)
        normalized.append(tuple(coordinates))
    return tuple(normalized)


def _validate_manual_row_groups_are_records(
    *,
    logical_table: _LogicalTable,
    structure: StructureSnapshot,
    row_groups: tuple[tuple[tuple[str, int], ...], ...],
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
) -> None:
    header_rows_by_part = _header_rows_by_part(logical_table)
    note_row_ids = _note_row_ids(
        structure=structure,
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
    )
    for group in row_groups:
        for table_id, row_index in group:
            if row_index in header_rows_by_part.get(table_id, ()):
                raise ValueError(
                    "manual row groups cannot turn a table header into a record"
                )
            row_node = row_node_by_coordinate.get((table_id, row_index))
            if (
                row_node is not None
                and row_node.structure_node_id in note_row_ids
            ):
                raise ValueError(
                    "manual row groups cannot turn a table note into a record"
                )


def _manual_header_catalog(
    logical_table: _LogicalTable,
) -> dict[str, tuple[TableRecordHeaderBindingV1, ...]]:
    return _header_bindings_by_part(
        logical_table,
        _header_rows_by_part(logical_table),
    )


def _manual_eligible_rows(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[tuple[_PhysicalTablePart, int], ...]:
    """Return the exact physical row domain that a manual partition must cover.

    Header and table-note rows remain shared context. Every other row carrying
    source text or a visual placement is a business fact and must occur in
    exactly one row group. Proven ellipsis placeholders are not business facts.
    """

    header_rows_by_part = _header_rows_by_part(logical_table)
    note_row_ids = _note_row_ids(
        structure=structure,
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
    )
    eligible: list[tuple[_PhysicalTablePart, int]] = []
    for part in logical_table.parts:
        header_rows = set(
            header_rows_by_part.get(part.table.table_id, ())
        )
        for row_index in range(part.table.row_count):
            if row_index in header_rows:
                continue
            row_node = row_node_by_coordinate.get(
                (part.table.table_id, row_index)
            )
            if (
                row_node is not None
                and row_node.structure_node_id in note_row_ids
            ):
                continue
            if _is_proven_ellipsis_placeholder_row(part.table, row_index):
                continue
            has_source_text = any(
                _cell_text(cell).strip()
                for cell in _cells_covering_row(part.table, row_index)
            )
            figures, images = _visual_refs_for_row(
                source_ir=source_ir,
                structure=structure,
                logical_table=logical_table,
                part=part,
                row_index=row_index,
                row_node=row_node,
                source_nodes_by_id=source_nodes_by_id,
            )
            if has_source_text or figures or images:
                eligible.append((part, row_index))
    return tuple(eligible)


def _headers_for_row_group(
    *,
    coordinates: tuple[tuple[str, int], ...],
    headers_by_table: dict[str, tuple[TableRecordHeaderBindingV1, ...]],
    first_headers: tuple[TableRecordHeaderBindingV1, ...],
) -> tuple[TableRecordHeaderBindingV1, ...]:
    headers: list[TableRecordHeaderBindingV1] = []
    for table_id, _ in coordinates:
        local = headers_by_table.get(table_id, ())
        headers.extend(local or first_headers)
    return _unique_header_bindings(tuple(headers))


def _fallback_manual_row_groups(
    *,
    logical_table: _LogicalTable,
    data_rows: tuple[tuple[_PhysicalTablePart, int], ...],
) -> tuple[tuple[tuple[str, int], ...], ...]:
    part_index = {
        part.table.table_id: index
        for index, part in enumerate(logical_table.parts)
    }
    groups: list[list[tuple[str, int]]] = []
    for part, row_index in data_rows:
        coordinate = (part.table.table_id, row_index)
        key = _normalized_key_text(
            _cell_text(_cell_covering(part.table, row_index, 0))
        )
        if not key and part_index[part.table.table_id] > 0 and groups:
            groups[-1].append(coordinate)
        else:
            groups.append([coordinate])
    return tuple(tuple(group) for group in groups)


def _validate_inputs(
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
) -> None:
    if source_ir.source_ir_id != structure.source_ir_id:
        raise ValueError("SourceIR and Structure snapshots must share source_ir_id")
    if isinstance(structure, EffectiveStructureSnapshotV1):
        if source_ir.source_sha256 != structure.source_sha256:
            raise ValueError("effective Structure source_sha256 does not match SourceIR")
        if (
            _source_ir_canonical_sha256(source_ir)
            != structure.source_ir_canonical_sha256
        ):
            raise ValueError(
                "effective Structure source canonical hash does not match SourceIR"
            )


def _build_logical_tables(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[_LogicalTable, ...]:
    table_nodes = tuple(
        sorted(
            (
                node
                for node in structure.nodes
                if node.kind is StructureNodeKind.TABLE
            ),
            key=lambda node: (node.source_order, node.structure_node_id),
        )
    )
    groups = _unique_continuation_groups(table_nodes, structure.relations)
    snapshot_id = _structure_snapshot_id(structure)
    source_canonical_sha256 = _source_ir_canonical_sha256(source_ir)
    projected_tables_by_id = build_adopted_table_projection_catalog(
        source_ir,
        structure,
    )
    result: list[_LogicalTable] = []
    for group in groups:
        parts: list[_PhysicalTablePart] = []
        used_source_ids: set[str] = set()
        for table_node in group:
            source_table_nodes = tuple(
                sorted(
                    (
                        source_nodes_by_id[ref.source_id]
                        for ref in table_node.source_refs
                        if ref.kind is StructureSourceRefKind.SOURCE_NODE
                        and ref.source_id in source_nodes_by_id
                        and source_nodes_by_id[ref.source_id].kind
                        is SourceNodeKind.TABLE
                    ),
                    key=lambda node: (node.source_order, node.source_node_id),
                )
            )
            if not source_table_nodes:
                raise ValueError("Structure TABLE node has no SourceIR table fact")
            for source_node in source_table_nodes:
                if source_node.source_node_id in used_source_ids:
                    continue
                used_source_ids.add(source_node.source_node_id)
                if source_node.table_payload is None:
                    raise ValueError("SourceIR table node has no TableIR payload")
                parts.append(
                    _PhysicalTablePart(
                        table_structure_node=table_node,
                        source_node=source_node,
                        source_table=source_node.table_payload,
                        table=projected_tables_by_id[
                            source_node.table_payload.table_id
                        ],
                    )
                )
        parts.sort(
            key=lambda item: (
                item.source_node.source_order,
                item.table.table_id,
            )
        )
        if not parts:
            raise ValueError("logical table chain has no immutable table part")

        fact_parts = tuple(
            _build_fact_part(
                source_ir=source_ir,
                structure=structure,
                logical_group=group,
                part=part,
                source_nodes_by_id=source_nodes_by_id,
            )
            for part in parts
        )
        whole_refs = normalize_table_record_source_refs(
            tuple(
                ref
                for fact_part in fact_parts
                for ref in fact_part.source_refs
            )
        )
        owner = group[0]
        fact = build_table_fact_binding(
            source_ir_id=source_ir.source_ir_id,
            source_sha256=source_ir.source_sha256,
            source_ir_canonical_sha256=source_canonical_sha256,
            structure_snapshot_id=snapshot_id,
            logical_table_id=owner.structure_node_id,
            owner_structure_node_id=owner.structure_node_id,
            table_parts=fact_parts,
            whole_table_source_refs=whole_refs,
        )
        result.append(
            _LogicalTable(
                table_nodes=group,
                parts=tuple(parts),
                fact=fact,
            )
        )
    return tuple(result)


def _build_fact_part(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_group: tuple[StructureNode, ...],
    part: _PhysicalTablePart,
    source_nodes_by_id: dict[str, SourceNode],
) -> TableFactPartV1:
    cells = tuple(
        sorted(
            part.source_table.cells,
            key=lambda cell: (cell.row, cell.column, cell.cell_id),
        )
    )
    source_refs = normalize_table_record_source_refs(
        (
            _source_node_ref(part.source_node.source_node_id),
            *tuple(_table_cell_ref(cell.cell_id) for cell in cells),
        )
    )
    visual_asset_refs = _table_visual_asset_ids(
        source_ir=source_ir,
        structure=structure,
        logical_group=logical_group,
        part=part,
        source_nodes_by_id=source_nodes_by_id,
    )
    return TableFactPartV1(
        table_id=part.table.table_id,
        source_order=part.source_node.source_order,
        row_count=part.table.row_count,
        column_count=part.table.column_count,
        cell_ids=tuple(cell.cell_id for cell in cells),
        table_content_sha256=canonical_model_sha256(part.source_table),
        source_refs=source_refs,
        visual_asset_refs=visual_asset_refs,
    )


def _build_automatic_projection(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    source_nodes_by_id: dict[str, SourceNode],
    children_by_parent: dict[str, tuple[StructureNode, ...]],
) -> TableRecordProjectionV1:
    plan = _automatic_row_group_plan(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        children_by_parent=children_by_parent,
        source_nodes_by_id=source_nodes_by_id,
    )
    if not plan.row_groups:
        reason_code = (
            plan.whole_table_reason_code
            or "AUTO_NO_PROVEN_RECORD_TOPOLOGY"
        )
        return build_table_record_projection(
            table_fact=logical_table.fact,
            mode=TableRecordProjectionMode.WHOLE_TABLE,
            projection_revision=_projection_revision(structure),
            decision=_projection_decision(
                origin=TableRecordProjectionDecisionOrigin.AUTO,
                mode=TableRecordProjectionMode.WHOLE_TABLE,
                reason_code=reason_code,
                message=(
                    "检测到由行轴与多级列轴共同定义的矩阵，自动投影保留整表。"
                    if reason_code == "AUTO_CROSS_AXIS_MATRIX_KEPT_WHOLE"
                    else "续页分类上下文存在歧义，自动投影保守保留整表。"
                    if reason_code
                    == "AUTO_AMBIGUOUS_CONTINUED_CLASSIFICATION_KEPT_WHOLE"
                    else "自动拆分无法完整且唯一覆盖表格事实，已保守保留整表。"
                    if reason_code
                    == "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE"
                    else "未发现可稳定证明的逐行记录结构，默认保留整表。"
                ),
                evidence=plan.evidence
                or ("未检测到明确表头、连续序号或稳定业务行边界",),
            ),
        )

    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    records = tuple(
        _build_row_group_record(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            coordinates=coordinates,
            headers=_unique_header_bindings(
                tuple(
                    header
                    for coordinate in coordinates
                    for header in plan.headers_by_coordinate.get(coordinate, ())
                )
            ),
            source_order=source_order,
            source_nodes_by_id=source_nodes_by_id,
            row_node_by_coordinate=row_node_by_coordinate,
            inherited_shared_values_by_coordinate=(
                plan.inherited_shared_values_by_coordinate
            ),
        )
        for source_order, coordinates in enumerate(plan.row_groups)
    )
    primary_reason = _primary_automatic_reason(plan.reason_codes)
    return build_table_record_projection(
        table_fact=logical_table.fact,
        mode=TableRecordProjectionMode.RECORDS,
        projection_revision=_projection_revision(structure),
        records=records,
        decision=_projection_decision(
            origin=TableRecordProjectionDecisionOrigin.AUTO,
            mode=TableRecordProjectionMode.RECORDS,
            reason_code=primary_reason,
            message=_automatic_reason_message(primary_reason),
            evidence=plan.evidence,
        ),
    )


def _automatic_row_group_plan(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    children_by_parent: dict[str, tuple[StructureNode, ...]],
    source_nodes_by_id: dict[str, SourceNode],
) -> _AutomaticRowGroupPlan:
    if logical_table.parts:
        cross_axis_header_rows = _cross_axis_matrix_header_rows(
            logical_table.parts[0].table
        )
        if cross_axis_header_rows:
            return _AutomaticRowGroupPlan(
                row_groups=(),
                headers_by_coordinate={},
                reason_codes=(),
                evidence=(
                    "多级列轴覆盖同一组行项目，交叉点不能自动降格为逐行记录",
                ),
                inherited_shared_values_by_coordinate={},
                whole_table_reason_code=(
                    "AUTO_CROSS_AXIS_MATRIX_KEPT_WHOLE"
                ),
            )

    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    note_row_ids = _note_row_ids(
        structure=structure,
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
    )
    header_rows_by_part = _header_rows_by_part(logical_table)
    data_rows = _data_rows(
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
        note_row_ids=note_row_ids,
        header_rows_by_part=header_rows_by_part,
    )

    schema_coordinates, schema_headers_by_coordinate = _schema_record_coordinates(
        logical_table=logical_table,
        row_node_by_coordinate=row_node_by_coordinate,
        note_row_ids=note_row_ids,
        header_rows_by_part=header_rows_by_part,
    )
    part_by_table_id = {
        part.table.table_id: part for part in logical_table.parts
    }
    explicit_coordinates = {
        coordinate
        for coordinate, row_node in row_node_by_coordinate.items()
        if coordinate[1] not in header_rows_by_part.get(coordinate[0], ())
        if row_node.structure_node_id not in note_row_ids
        if not _is_structural_boundary_row(
            part_by_table_id[coordinate[0]].table,
            coordinate[1],
        )
        if any(
            child.kind in {StructureNodeKind.SECTION, StructureNodeKind.CLAUSE}
            and child.boundary_basis is StructureBoundaryBasis.EXPLICIT_NUMBER
            for child in children_by_parent.get(row_node.structure_node_id, ())
        )
    }
    ordinal_coordinates, ordinal_context_rows = _ordinal_record_coordinates(
        logical_table=logical_table,
        data_rows=data_rows,
    )
    for table_id, row_indexes in ordinal_context_rows.items():
        if header_rows_by_part.get(table_id):
            continue
        header_rows_by_part[table_id] = row_indexes

    header_catalog = _header_bindings_by_part(
        logical_table,
        header_rows_by_part,
    )

    two_level_coordinates, two_level_key_columns = (
        _two_level_parameter_record_coordinates(
            logical_table=logical_table,
            header_rows_by_part=header_rows_by_part,
        )
    )
    hierarchical_coordinates, hierarchical_key_columns = (
        _hierarchical_leaf_record_coordinates(
            logical_table=logical_table,
            header_rows_by_part=header_rows_by_part,
        )
    )
    visual_coordinates, visual_key_columns = _visual_header_record_coordinates(
        logical_table=logical_table,
        header_rows_by_part=header_rows_by_part,
    )
    (
        merged_label_coordinates,
        merged_label_key_columns,
        inherited_merged_values,
        ambiguous_continued_classification,
    ) = (
        _merged_label_description_record_coordinates(
            logical_table=logical_table,
            header_rows_by_part=header_rows_by_part,
        )
    )

    if ambiguous_continued_classification:
        return _AutomaticRowGroupPlan(
            row_groups=(),
            headers_by_coordinate={},
            reason_codes=(),
            evidence=(
                "续页出现无法由本页拓扑完整证明的新分类边界，保守保留整表",
            ),
            inherited_shared_values_by_coordinate={},
            whole_table_reason_code=(
                "AUTO_AMBIGUOUS_CONTINUED_CLASSIFICATION_KEPT_WHOLE"
            ),
        )

    selected_coordinates = {
        *explicit_coordinates,
        *schema_coordinates,
        *ordinal_coordinates,
        *two_level_coordinates,
        *hierarchical_coordinates,
        *visual_coordinates,
        *merged_label_coordinates,
    }
    first_headers = next(
        (headers for headers in header_catalog.values() if headers),
        (),
    )
    headers_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordHeaderBindingV1, ...],
    ] = {}
    for coordinate in selected_coordinates:
        table_id, _ = coordinate
        if coordinate in schema_headers_by_coordinate:
            headers = schema_headers_by_coordinate[coordinate]
        elif (
            coordinate in explicit_coordinates
            and coordinate not in ordinal_coordinates
            and coordinate not in two_level_coordinates
            and coordinate not in hierarchical_coordinates
            and coordinate not in visual_coordinates
        ):
            # Explicit peers prove the local row boundary, not an unrelated
            # schema.  Do not invent a header for them.
            headers = ()
        elif coordinate in merged_label_coordinates:
            part = part_by_table_id[table_id]
            headers = (
                header_catalog.get(table_id, ())
                if _ordinal_safe_leading_header_rows(
                    part.table,
                    _declared_leading_header_rows(part.table),
                )
                else ()
            )
        else:
            headers = header_catalog.get(table_id, ()) or first_headers
        headers_by_coordinate[coordinate] = headers

    table_order = {
        part.table.table_id: index
        for index, part in enumerate(logical_table.parts)
    }
    ordered_coordinates = tuple(
        sorted(
            selected_coordinates,
            key=lambda item: (table_order[item[0]], item[1]),
        )
    )
    groups: list[list[tuple[str, int]]] = [
        [coordinate] for coordinate in ordered_coordinates
    ]

    key_columns = {
        **_schema_key_columns(
            logical_table,
            header_rows_by_part=header_rows_by_part,
        ),
        **{table_id: 0 for table_id, _ in ordinal_coordinates},
        **two_level_key_columns,
        **hierarchical_key_columns,
        **visual_key_columns,
        **merged_label_key_columns,
    }
    continuation_count = _attach_blank_continuation_rows(
        logical_table=logical_table,
        groups=groups,
        selected_coordinates=selected_coordinates,
        headers_by_coordinate=headers_by_coordinate,
        header_catalog=header_catalog,
        key_columns=key_columns,
        row_node_by_coordinate=row_node_by_coordinate,
        note_row_ids=note_row_ids,
        header_rows_by_part=header_rows_by_part,
    )

    reason_codes: list[str] = []
    evidence: list[str] = []
    if schema_coordinates:
        reason_codes.append("AUTO_STABLE_KEY_SCHEMA")
        evidence.append(f"明确表头与稳定记录键：{len(schema_coordinates)} 行")
    if ordinal_coordinates:
        reason_codes.append("AUTO_CONTINUOUS_ORDINAL_ROWS")
        evidence.append(f"连续序号：{len(ordinal_coordinates)} 行")
    if explicit_coordinates:
        reason_codes.append("AUTO_EXPLICIT_NUMBERED_ROWS")
        evidence.append(f"显式编号边界：{len(explicit_coordinates)} 行")
    if merged_label_coordinates:
        reason_codes.append("AUTO_MERGED_LABEL_DESCRIPTION_ROWS")
        evidence.append(
            f"合并分类单元格下的名称—说明行：{len(merged_label_coordinates)} 行"
        )
    if two_level_coordinates:
        reason_codes.append("AUTO_TWO_LEVEL_PARAMETER_ROWS")
        evidence.append(
            f"两级表头下的参数行：{len(two_level_coordinates)} 行"
        )
    if hierarchical_coordinates:
        reason_codes.append("AUTO_HIERARCHICAL_LEAF_ROWS")
        evidence.append(
            "合并主分组下具有独立叶锚点的业务行："
            f"{len(hierarchical_coordinates)} 行"
        )
    if visual_coordinates:
        reason_codes.append("AUTO_VISUAL_HEADER_PARAMETER_ROWS")
        evidence.append(
            f"视觉表头下的稳定业务行：{len(visual_coordinates)} 行"
        )
    if continuation_count:
        evidence.append(f"续页空键内容并入上一记录：{continuation_count} 行")

    normalized_groups = tuple(tuple(group) for group in groups)
    if normalized_groups:
        coverage_failure = _fact_coverage_failure(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            row_groups=normalized_groups,
            header_rows_by_part=header_rows_by_part,
            row_node_by_coordinate=row_node_by_coordinate,
            headers_by_coordinate=headers_by_coordinate,
            inherited_shared_values_by_coordinate=inherited_merged_values,
            source_nodes_by_id=source_nodes_by_id,
        )
        if coverage_failure is not None:
            return _AutomaticRowGroupPlan(
                row_groups=(),
                headers_by_coordinate={},
                reason_codes=(),
                evidence=coverage_failure.evidence,
                inherited_shared_values_by_coordinate={},
                whole_table_reason_code=(
                    "AUTO_INCOMPLETE_FACT_COVERAGE_KEPT_WHOLE"
                ),
            )

    return _AutomaticRowGroupPlan(
        row_groups=normalized_groups,
        headers_by_coordinate=headers_by_coordinate,
        reason_codes=tuple(reason_codes),
        evidence=tuple(dict.fromkeys(evidence)),
        inherited_shared_values_by_coordinate=inherited_merged_values,
    )


def _fact_coverage_failure(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    row_groups: tuple[tuple[tuple[str, int], ...], ...],
    header_rows_by_part: dict[str, tuple[int, ...]],
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    headers_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordHeaderBindingV1, ...],
    ],
    inherited_shared_values_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordSharedMergedValueV1, ...],
    ],
    source_nodes_by_id: dict[str, SourceNode],
) -> _FactCoverageFailure | None:
    """Require every RECORDS projection to be an exact cover of table facts.

    The invariant is fact-based rather than ``one physical row = one record``:
    several source rows may belong to one record, and a vertically merged fact
    may be shared by several records when Structure has proved that role.  What
    is forbidden is silently dropping an anchored business cell or consuming a
    business row/cell through more than one primary record.
    """

    part_by_table_id = {
        part.table.table_id: part for part in logical_table.parts
    }
    group_indexes_by_coordinate: dict[tuple[str, int], set[int]] = {}
    coordinate_occurrences: dict[tuple[str, int], int] = {}
    for group_index, group in enumerate(row_groups):
        for coordinate in group:
            coordinate_occurrences[coordinate] = (
                coordinate_occurrences.get(coordinate, 0) + 1
            )
            group_indexes_by_coordinate.setdefault(coordinate, set()).add(
                group_index
            )

    trusted_header_cell_ids = _trusted_header_cell_ids(
        logical_table=logical_table,
        header_rows_by_part=header_rows_by_part,
    )
    bound_trusted_header_cell_ids: set[str] = set()
    note_cell_ids: set[str] = set()
    shared_group_indexes_by_cell_id: dict[str, set[int]] = {}
    visual_group_indexes_by_placement_id: dict[str, set[int]] = {}
    for group_index, group in enumerate(row_groups):
        group_headers = _unique_header_bindings(
            tuple(
                header
                for coordinate in group
                for header in headers_by_coordinate.get(coordinate, ())
            )
        )
        bound_trusted_header_cell_ids.update(
            ref.source_id
            for header in group_headers
            for ref in header.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in trusted_header_cell_ids
        )
        for coordinate in group:
            part = part_by_table_id.get(coordinate[0])
            if part is None:
                continue
            row_node = row_node_by_coordinate.get(coordinate)
            row_shared = _unique_shared_values(
                (
                    *_shared_values_for_row(
                        structure=structure,
                        logical_table=logical_table,
                        row_node=row_node,
                    ),
                    *inherited_shared_values_by_coordinate.get(
                        coordinate,
                        (),
                    ),
                )
            )
            for value in row_shared:
                shared_group_indexes_by_cell_id.setdefault(
                    value.cell_id,
                    set(),
                ).add(group_index)
            note_cell_ids.update(
                ref.source_id
                for note in _notes_for_row(
                    structure=structure,
                    logical_table=logical_table,
                    row_node=row_node,
                )
                for ref in note.source_refs
                if ref.kind is StructureSourceRefKind.TABLE_CELL
            )
            row_figures, row_images = _visual_refs_for_row(
                source_ir=source_ir,
                structure=structure,
                logical_table=logical_table,
                part=part,
                row_index=coordinate[1],
                row_node=row_node,
                source_nodes_by_id=source_nodes_by_id,
            )
            for visual in (*row_figures, *row_images):
                for ref in visual.source_refs:
                    if ref.kind is not StructureSourceRefKind.SOURCE_NODE:
                        continue
                    visual_group_indexes_by_placement_id.setdefault(
                        ref.source_id,
                        set(),
                    ).add(group_index)

    placeholder_cell_ids = {
        cell.cell_id
        for part in logical_table.parts
        for row_index in range(part.table.row_count)
        if _is_proven_ellipsis_placeholder_row(part.table, row_index)
        for cell in part.table.cells
        if cell.row == row_index and _cell_text(cell)
    }
    non_primary_cell_ids = {
        *bound_trusted_header_cell_ids,
        *note_cell_ids,
        *placeholder_cell_ids,
    }

    unconsumed: list[str] = []
    multiply_consumed: list[str] = []
    business_cell_ids_by_coordinate: dict[tuple[str, int], set[str]] = {}
    for part in logical_table.parts:
        table = part.table
        for cell in table.cells:
            if not _cell_text(cell) or cell.cell_id in non_primary_cell_ids:
                continue
            owner_groups = {
                group_index
                for row_index in range(cell.row, cell.row + cell.row_span)
                for group_index in group_indexes_by_coordinate.get(
                    (table.table_id, row_index),
                    (),
                )
            }
            for row_index in range(cell.row, cell.row + cell.row_span):
                business_cell_ids_by_coordinate.setdefault(
                    (table.table_id, row_index),
                    set(),
                ).add(cell.cell_id)
            shared_owner_groups = shared_group_indexes_by_cell_id.get(
                cell.cell_id,
                set(),
            )
            if shared_owner_groups:
                if owner_groups.issubset(shared_owner_groups):
                    continue
                multiply_consumed.append(cell.cell_id)
            elif not owner_groups:
                unconsumed.append(cell.cell_id)
            elif len(owner_groups) > 1:
                multiply_consumed.append(cell.cell_id)

    duplicate_rows = tuple(
        sorted(
            (
                coordinate
                for coordinate, count in coordinate_occurrences.items()
                if count > 1
                and business_cell_ids_by_coordinate.get(coordinate)
            ),
            key=lambda item: (
                logical_table.fact.table_ids.index(item[0]),
                item[1],
            ),
        )
    )
    expected_visual_placement_ids = _table_visual_placement_source_node_ids(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        source_nodes_by_id=source_nodes_by_id,
    )
    unconsumed_visuals = tuple(
        sorted(
            placement_id
            for placement_id in expected_visual_placement_ids
            if not visual_group_indexes_by_placement_id.get(placement_id)
        )
    )
    multiply_consumed_visuals = tuple(
        sorted(
            placement_id
            for placement_id in expected_visual_placement_ids
            if len(
                visual_group_indexes_by_placement_id.get(placement_id, ())
            )
            > 1
        )
    )
    if (
        not unconsumed
        and not multiply_consumed
        and not duplicate_rows
        and not unconsumed_visuals
        and not multiply_consumed_visuals
    ):
        return None
    return _FactCoverageFailure(
        unconsumed_business_cell_ids=tuple(sorted(unconsumed)),
        multiply_consumed_business_cell_ids=tuple(
            sorted(multiply_consumed)
        ),
        multiply_consumed_business_rows=duplicate_rows,
        unconsumed_visual_placement_ids=unconsumed_visuals,
        multiply_consumed_visual_placement_ids=(
            multiply_consumed_visuals
        ),
    )


def _trusted_header_cell_ids(
    *,
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> set[str]:
    """Return cells wholly contained by the proven local header prefix.

    Per-record header bindings may inherit context from another physical part.
    They therefore cannot by themselves exempt a local business cell from the
    exact-cover gate.  Only the locally proven and ordinal-sanitized header
    region may do so.  A cell spanning out of that region remains a business
    fact that a record/shared-value binding must consume.
    """

    result: set[str] = set()
    for part in logical_table.parts:
        header_rows = set(
            header_rows_by_part.get(part.table.table_id, ())
        )
        if not header_rows:
            continue
        for cell in part.table.cells:
            covered_rows = set(range(cell.row, cell.row + cell.row_span))
            if covered_rows and covered_rows.issubset(header_rows):
                result.add(cell.cell_id)
    return result


def _note_row_ids(
    *,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
) -> set[str]:
    coordinate_by_row_node_id = {
        node.structure_node_id: coordinate
        for coordinate, node in row_node_by_coordinate.items()
    }
    node_by_id = {node.structure_node_id: node for node in structure.nodes}
    cell_catalog = {
        cell.cell_id: (part, cell)
        for part in logical_table.parts
        for cell in part.table.cells
    }
    note_ids: set[str] = set()
    for relation in structure.relations:
        if relation.kind is not StructureRelationKind.APPLIES_TO:
            continue
        target_coordinate = coordinate_by_row_node_id.get(
            relation.target_structure_node_id
        )
        if target_coordinate is None:
            continue
        source_coordinate = coordinate_by_row_node_id.get(
            relation.source_structure_node_id
        )
        if source_coordinate is not None:
            note_ids.add(relation.source_structure_node_id)
            continue

        source_node = node_by_id.get(relation.source_structure_node_id)
        if (
            source_node is None
            or source_node.kind is not StructureNodeKind.CONTENT
            or source_node.boundary_basis is not StructureBoundaryBasis.MERGED_RANGE
        ):
            continue
        for ref in source_node.source_refs:
            if ref.kind is not StructureSourceRefKind.TABLE_CELL:
                continue
            catalog_item = cell_catalog.get(ref.source_id)
            if catalog_item is None:
                continue
            part, cell = catalog_item
            candidate_coordinate = (part.table.table_id, cell.row)
            candidate_row = row_node_by_coordinate.get(candidate_coordinate)
            if (
                candidate_row is None
                or not _is_effectively_full_width_row(part.table, cell.row)
                or not _coordinate_follows(
                    logical_table,
                    candidate_coordinate,
                    target_coordinate,
                )
            ):
                continue
            note_ids.add(candidate_row.structure_node_id)
    return note_ids


def _coordinate_follows(
    logical_table: _LogicalTable,
    candidate: tuple[str, int],
    target: tuple[str, int],
) -> bool:
    part_order = {
        part.table.table_id: index
        for index, part in enumerate(logical_table.parts)
    }
    if candidate[0] not in part_order or target[0] not in part_order:
        return False
    return (part_order[candidate[0]], candidate[1]) > (
        part_order[target[0]],
        target[1],
    )


def _proven_ordinal_data_start_row(table: TableIR) -> int | None:
    """Return the first row of a complete leading ``1, 2, ...`` data run.

    A geometric or parser-declared multi-row header is not trusted when one
    of its rows is also the first member of a contiguous ordinal business
    region.  The proof deliberately reuses the normal ordinal topology and
    additionally requires payload outside the ordinal cell on every row.
    """

    candidate_rows = tuple(
        row_index
        for row_index in range(table.row_count)
        if _row_has_substantive_text(table, row_index)
        and not _is_effectively_full_width_row(table, row_index)
        and not _is_structural_boundary_row(table, row_index)
    )
    candidate = _ordinal_suffix(
        table,
        candidate_rows,
        require_start_one=True,
        expected_next=None,
        allow_single=False,
        allow_gaps=False,
    )
    if candidate is None:
        return None
    record_rows, _ = candidate
    if not all(
        _row_has_payload_after_column(
            table,
            row_index=row_index,
            column_index=0,
        )
        for row_index in record_rows
    ):
        return None
    return record_rows[0]


def _ordinal_safe_leading_header_rows(
    table: TableIR,
    header_rows: tuple[int, ...],
) -> tuple[int, ...]:
    if not header_rows:
        return ()
    data_start = _proven_ordinal_data_start_row(table)
    if data_start is None:
        return header_rows
    return tuple(row_index for row_index in header_rows if row_index < data_start)


def _header_rows_by_part(
    logical_table: _LogicalTable,
) -> dict[str, tuple[int, ...]]:
    """Return locally present header rows for every physical table part.

    SourceIR deliberately does not invent semantic PDF header regions.  This
    catalog therefore combines parser-declared/stub headers with conservative
    topology-only inference.  A continuation part is assigned local rows only
    when that part physically repeats the header; otherwise its records inherit
    the first proven header bindings without pretending those cells exist.
    """

    result = {
        part.table.table_id: _ordinal_safe_leading_header_rows(
            part.table,
            _leading_header_rows(part.table),
        )
        for part in logical_table.parts
    }
    if not logical_table.parts:
        return result

    first = logical_table.parts[0]
    first_id = first.table.table_id
    if not result[first_id]:
        two_level = _two_level_parameter_header_rows(first.table)
        if two_level:
            result[first_id] = _ordinal_safe_leading_header_rows(
                first.table,
                two_level,
            )
        else:
            hierarchical = _hierarchical_leaf_topology(first.table)
            if hierarchical is not None:
                result[first_id] = _ordinal_safe_leading_header_rows(
                    first.table,
                    hierarchical.header_rows,
                )
            else:
                visual = _visual_parameter_header_rows(
                    first.table,
                    has_continuation=len(logical_table.parts) > 1,
                )
                if visual:
                    result[first_id] = _ordinal_safe_leading_header_rows(
                        first.table,
                        visual,
                    )

    if not result[first_id]:
        for table_id, rows in _ordinal_header_context_by_part(
            logical_table,
            result,
        ).items():
            if not result.get(table_id):
                part = next(
                    item
                    for item in logical_table.parts
                    if item.table.table_id == table_id
                )
                result[table_id] = _ordinal_safe_leading_header_rows(
                    part.table,
                    rows,
                )

    first_rows = result[first_id]
    if first_rows:
        reference_signature = _header_signature(first.table, first_rows)
        for part in logical_table.parts[1:]:
            table_id = part.table.table_id
            if result[table_id]:
                continue
            exact_rows = _matching_header_prefix_rows(
                part.table,
                reference_signature,
            )
            if exact_rows:
                result[table_id] = _ordinal_safe_leading_header_rows(
                    part.table,
                    exact_rows,
                )
            elif (
                len(first_rows) == 1
                and _compatible_repeated_header_row(
                    first.table,
                    first_rows[0],
                    part.table,
                    0,
                )
                and _rows_after_header_have_stable_keys(part.table, (0,))
            ):
                result[table_id] = _ordinal_safe_leading_header_rows(
                    part.table,
                    (0,),
                )
        return result

    inferred_blank_key_parts = _blank_key_repeated_header_parts(logical_table)
    if inferred_blank_key_parts:
        result[first_id] = _ordinal_safe_leading_header_rows(
            first.table,
            (0,),
        )
        for table_id in inferred_blank_key_parts:
            part = next(
                item
                for item in logical_table.parts
                if item.table.table_id == table_id
            )
            result[table_id] = _ordinal_safe_leading_header_rows(
                part.table,
                (0,),
            )
    return result


def _ordinal_header_context_by_part(
    logical_table: _LogicalTable,
    known_header_rows: dict[str, tuple[int, ...]],
) -> dict[str, tuple[int, ...]]:
    candidate_rows = tuple(
        (part, row_index)
        for part in logical_table.parts
        for row_index in range(part.table.row_count)
        if row_index not in known_header_rows.get(part.table.table_id, ())
        and _row_has_substantive_text(part.table, row_index)
        and not _is_effectively_full_width_row(part.table, row_index)
        and not _is_structural_boundary_row(part.table, row_index)
    )
    _, context_rows = _ordinal_record_coordinates(
        logical_table=logical_table,
        data_rows=candidate_rows,
    )
    return context_rows


def _header_bindings_by_part(
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> dict[str, tuple[TableRecordHeaderBindingV1, ...]]:
    local = {
        part.table.table_id: _header_bindings_for_rows(
            part,
            header_rows_by_part.get(part.table.table_id, ()),
        )
        for part in logical_table.parts
    }
    inherited = (
        local[logical_table.parts[0].table.table_id]
        if logical_table.parts
        else ()
    )
    return {
        part.table.table_id: (
            local[part.table.table_id]
            or (inherited if part_index > 0 else ())
        )
        for part_index, part in enumerate(logical_table.parts)
    }


def _blank_key_repeated_header_parts(
    logical_table: _LogicalTable,
) -> set[str]:
    if len(logical_table.parts) < 2:
        return set()
    first = logical_table.parts[0]
    if (
        first.table.row_count < 2
        or _normalized_key_text(
            _cell_text(_cell_covering(first.table, 0, 0))
        )
        or not _rows_after_header_have_stable_keys(first.table, (0,))
    ):
        return set()
    matches = {
        part.table.table_id
        for part in logical_table.parts[1:]
        if part.table.row_count >= 2
        and not _normalized_key_text(
            _cell_text(_cell_covering(part.table, 0, 0))
        )
        and _compatible_repeated_header_row(
            first.table,
            0,
            part.table,
            0,
        )
        and _rows_after_header_have_stable_keys(part.table, (0,))
    }
    return matches


def _compatible_repeated_header_row(
    reference_table: TableIR,
    reference_row: int,
    candidate_table: TableIR,
    candidate_row: int,
) -> bool:
    if (
        reference_table.column_count != candidate_table.column_count
        or reference_table.column_count < 2
        or _is_effectively_full_width_row(reference_table, reference_row)
        or _is_effectively_full_width_row(candidate_table, candidate_row)
    ):
        return False
    reference = _row_signature(reference_table, reference_row)
    candidate = _row_signature(candidate_table, candidate_row)
    if bool(reference[0]) != bool(candidate[0]):
        return False
    pairs = tuple(zip(reference[1:], candidate[1:]))
    if not pairs or any(not left or not right for left, right in pairs):
        return False
    matching = sum(left == right for left, right in pairs)
    differing = len(pairs) - matching
    return matching > differing


def _rows_after_header_have_stable_keys(
    table: TableIR,
    header_rows: tuple[int, ...],
) -> bool:
    if not header_rows:
        return False
    start_row = max(header_rows) + 1
    rows = tuple(
        row_index
        for row_index in range(start_row, table.row_count)
        if _row_has_substantive_text(table, row_index)
        and not _is_effectively_full_width_row(table, row_index)
        and not _is_structural_boundary_row(table, row_index)
    )
    keys = tuple(
        _normalized_key_text(
            _cell_text(_cell_covering(table, row_index, 0))
        )
        for row_index in rows
    )
    return bool(keys) and all(keys) and len(keys) == len(set(keys))


def _cross_axis_matrix_header_rows(table: TableIR) -> tuple[int, ...]:
    """Prove a row-axis by multi-level column-axis matrix topology.

    A single dominant top header must cover all columns after one or more
    vertically spanning row-axis stubs.  At least three peer headers partition
    that span on the next row; the terminal axis may either repeat per peer or
    be expressed as one shared group-wide header.  A preceding title row does
    not change this topology.  Such intersections are not independent row
    records and therefore require an explicit matrix projection.
    """

    if table.row_count < 5 or table.column_count < 4:
        return ()
    for header_row in range(table.row_count - 4):
        top_cells = tuple(
            cell
            for cell in table.cells
            if cell.row == header_row and _cell_text(cell)
        )
        for dominant in top_cells:
            if (
                dominant.column == 0
                or dominant.row_span != 1
                or dominant.column_span < 3
                or dominant.column + dominant.column_span
                != table.column_count
            ):
                continue
            row_axis_columns = range(dominant.column)
            if not all(
                (
                    (
                        stub := _cell_covering(
                            table,
                            header_row,
                            column_index,
                        )
                    )
                    is not None
                    and bool(_cell_text(stub))
                    and stub.row == header_row
                    and stub.row_span >= 3
                )
                for column_index in row_axis_columns
            ):
                continue
            peer_cells = _anchored_cells_partitioning_span(
                table,
                row_index=header_row + 1,
                start_column=dominant.column,
                column_span=dominant.column_span,
            )
            terminal_cells = _anchored_cells_partitioning_span(
                table,
                row_index=header_row + 2,
                start_column=dominant.column,
                column_span=dominant.column_span,
            )
            terminal_axis_is_proven = len(terminal_cells) >= 3 or (
                len(terminal_cells) == 1
                and terminal_cells[0].column == dominant.column
                and terminal_cells[0].column_span == dominant.column_span
            )
            if len(peer_cells) >= 3 and terminal_axis_is_proven:
                return (header_row, header_row + 1, header_row + 2)
    return ()


def _anchored_cells_partitioning_span(
    table: TableIR,
    *,
    row_index: int,
    start_column: int,
    column_span: int,
) -> tuple[TableCellIR, ...]:
    end_column = start_column + column_span
    cells = tuple(
        sorted(
            (
                cell
                for cell in table.cells
                if cell.row == row_index
                and cell.column >= start_column
                and cell.column + cell.column_span <= end_column
                and _cell_text(cell)
            ),
            key=lambda cell: (cell.column, cell.cell_id),
        )
    )
    covered_columns = {
        column_index
        for cell in cells
        for column_index in range(
            cell.column,
            cell.column + cell.column_span,
        )
    }
    return (
        cells
        if covered_columns == set(range(start_column, end_column))
        else ()
    )


def _hierarchical_leaf_topology(
    table: TableIR,
) -> _HierarchicalLeafTopology | None:
    """Recognize grouped rows with an independently anchored leaf column."""

    if table.row_count < 4 or table.column_count < 3:
        return None
    top_cells = tuple(
        cell
        for cell in table.cells
        if cell.row == 0 and _cell_text(cell)
    )
    vertically_stable_headers = tuple(
        cell for cell in top_cells if cell.row_span >= 2
    )
    grouped_headers = tuple(
        cell
        for cell in top_cells
        if cell.row_span == 1 and cell.column_span > 1
    )
    if not vertically_stable_headers or not any(
        len(
            _anchored_cells_partitioning_span(
                table,
                row_index=1,
                start_column=group.column,
                column_span=group.column_span,
            )
        )
        >= 2
        for group in grouped_headers
    ):
        return None

    data_rows = tuple(
        row_index
        for row_index in range(2, table.row_count)
        if _row_has_substantive_text(table, row_index)
        and not _is_effectively_full_width_row(table, row_index)
        and not _is_structural_boundary_row(table, row_index)
    )
    if len(data_rows) < 2:
        return None

    for leaf_column in range(1, table.column_count - 1):
        if not _has_grouping_merge_before_column(
            table,
            data_rows=data_rows,
            column_index=leaf_column,
        ):
            continue
        if all(
            _is_independent_leaf_anchor(
                table,
                row_index=row_index,
                column_index=leaf_column,
            )
            and _row_has_payload_after_column(
                table,
                row_index=row_index,
                column_index=leaf_column,
            )
            for row_index in data_rows
        ):
            return _HierarchicalLeafTopology(
                header_rows=(0, 1),
                leaf_column=leaf_column,
            )
    return None


def _has_grouping_merge_before_column(
    table: TableIR,
    *,
    data_rows: tuple[int, ...],
    column_index: int,
) -> bool:
    return any(
        cell.row >= data_rows[0]
        and cell.row <= data_rows[-1]
        and cell.column < column_index
        and cell.row_span > 1
        and _cell_text(cell)
        for cell in table.cells
    )


def _is_independent_leaf_anchor(
    table: TableIR,
    *,
    row_index: int,
    column_index: int,
) -> bool:
    cell = _cell_covering(table, row_index, column_index)
    return bool(
        cell is not None
        and cell.row == row_index
        and cell.column == column_index
        and cell.row_span == 1
        and cell.column_span == 1
        and _normalized_key_text(_cell_text(cell))
    )


def _row_has_payload_after_column(
    table: TableIR,
    *,
    row_index: int,
    column_index: int,
) -> bool:
    return any(
        cell.column > column_index and bool(_normalized_key_text(_cell_text(cell)))
        for cell in _cells_covering_row(table, row_index)
    )


def _two_level_parameter_header_rows(table: TableIR) -> tuple[int, ...]:
    if table.column_count < 2 or table.row_count < 4:
        return ()
    top_cells = tuple(
        cell
        for cell in table.cells
        if cell.row == 0 and _cell_text(cell)
    )
    if (
        len(top_cells) < 2
        or not any(cell.column_span > 1 for cell in top_cells)
        or any(cell.column_span == table.column_count for cell in top_cells)
    ):
        return ()
    if not all(
        _cell_text(_cell_covering(table, row_index, column_index))
        for row_index in (0, 1)
        for column_index in range(table.column_count)
    ):
        return ()
    data_rows = tuple(
        row_index
        for row_index in range(2, table.row_count)
        if _row_has_substantive_text(table, row_index)
        and _normalized_key_text(
            _cell_text(_cell_covering(table, row_index, 0))
        )
    )
    if len(data_rows) < 2:
        return ()
    keys = tuple(
        _normalized_key_text(
            _cell_text(_cell_covering(table, row_index, 0))
        )
        for row_index in data_rows
    )
    return (0, 1) if len(keys) == len(set(keys)) else ()


def _visual_parameter_header_rows(
    table: TableIR,
    *,
    has_continuation: bool,
) -> tuple[int, ...]:
    if table.column_count < 3 or table.row_count < 3:
        return ()
    if _is_effectively_full_width_row(table, 0):
        return ()
    if not all(
        _normalized_key_text(
            _cell_text(_cell_covering(table, 0, column_index))
        )
        for column_index in range(table.column_count)
    ):
        return ()
    trailing_full_width = any(
        _is_effectively_full_width_row(table, row_index)
        for row_index in range(1, table.row_count)
    )
    if not (has_continuation or trailing_full_width):
        return ()
    data_rows = tuple(
        row_index
        for row_index in range(1, table.row_count)
        if not _is_effectively_full_width_row(table, row_index)
        and _row_has_substantive_text(table, row_index)
    )
    keys = tuple(
        _normalized_key_text(
            _cell_text(_cell_covering(table, row_index, 0))
        )
        for row_index in data_rows
    )
    if len(keys) < 2 or not all(keys) or len(keys) != len(set(keys)):
        return ()
    return (0,)


def _two_level_parameter_record_coordinates(
    *,
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    if not logical_table.parts:
        return set(), {}
    first = logical_table.parts[0]
    if (
        header_rows_by_part.get(first.table.table_id) != (0, 1)
        or _two_level_parameter_header_rows(first.table) != (0, 1)
    ):
        return set(), {}
    selected: set[tuple[str, int]] = set()
    key_columns: dict[str, int] = {}
    for part in logical_table.parts:
        local_headers = header_rows_by_part.get(part.table.table_id, ())
        start_row = max(local_headers) + 1 if local_headers else 0
        candidate_rows = tuple(
            row_index
            for row_index in range(start_row, part.table.row_count)
            if _row_has_substantive_text(part.table, row_index)
            and _normalized_key_text(
                _cell_text(_cell_covering(part.table, row_index, 0))
            )
        )
        if candidate_rows:
            selected.update(
                (part.table.table_id, row_index)
                for row_index in candidate_rows
            )
            key_columns[part.table.table_id] = 0
    return selected, key_columns


def _hierarchical_leaf_record_coordinates(
    *,
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    if not logical_table.parts:
        return set(), {}
    topology = _hierarchical_leaf_topology(logical_table.parts[0].table)
    if topology is None:
        return set(), {}

    selected: set[tuple[str, int]] = set()
    key_columns: dict[str, int] = {}
    for part in logical_table.parts:
        table = part.table
        if topology.leaf_column >= table.column_count:
            continue
        local_headers = header_rows_by_part.get(table.table_id, ())
        start_row = max(local_headers) + 1 if local_headers else 0
        for row_index in range(start_row, table.row_count):
            if (
                _is_effectively_full_width_row(table, row_index)
                or _is_structural_boundary_row(table, row_index)
                or not _is_independent_leaf_anchor(
                    table,
                    row_index=row_index,
                    column_index=topology.leaf_column,
                )
                or not _row_has_payload_after_column(
                    table,
                    row_index=row_index,
                    column_index=topology.leaf_column,
                )
            ):
                continue
            selected.add((table.table_id, row_index))
        key_columns[table.table_id] = topology.leaf_column
    return selected, key_columns


def _visual_header_record_coordinates(
    *,
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[set[tuple[str, int]], dict[str, int]]:
    if not logical_table.parts:
        return set(), {}
    first = logical_table.parts[0]
    visual_header_rows = _visual_parameter_header_rows(
        first.table,
        has_continuation=len(logical_table.parts) > 1,
    )
    repeated_blank_key_parts = _blank_key_repeated_header_parts(logical_table)
    if (
        header_rows_by_part.get(first.table.table_id) != (0,)
        or (
            visual_header_rows != (0,)
            and not repeated_blank_key_parts
        )
    ):
        return set(), {}
    selected: set[tuple[str, int]] = set()
    key_columns: dict[str, int] = {}
    for part in logical_table.parts:
        local_headers = header_rows_by_part.get(part.table.table_id, ())
        start_row = max(local_headers) + 1 if local_headers else 0
        keyed_rows = tuple(
            row_index
            for row_index in range(start_row, part.table.row_count)
            if not _is_effectively_full_width_row(part.table, row_index)
            and _row_has_substantive_text(part.table, row_index)
            and _normalized_key_text(
                _cell_text(_cell_covering(part.table, row_index, 0))
            )
        )
        if keyed_rows:
            selected.update(
                (part.table.table_id, row_index)
                for row_index in keyed_rows
            )
        key_columns[part.table.table_id] = 0
    return selected, key_columns


def _merged_label_description_record_coordinates(
    *,
    logical_table: _LogicalTable,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[
    set[tuple[str, int]],
    dict[str, int],
    dict[
        tuple[str, int],
        tuple[TableRecordSharedMergedValueV1, ...],
    ],
    bool,
]:
    selected: set[tuple[str, int]] = set()
    key_columns: dict[str, int] = {}
    inherited_values: dict[
        tuple[str, int],
        tuple[TableRecordSharedMergedValueV1, ...],
    ] = {}
    active_topology: tuple[
        int,
        int,
        tuple[TableRecordSharedMergedValueV1, ...],
    ] | None = None
    ambiguous_continuation = False
    first_part = logical_table.parts[0] if logical_table.parts else None
    if (
        first_part is not None
        and _hierarchical_leaf_topology(first_part.table) is not None
    ):
        # The hierarchical detector owns this stronger multi-level schema and
        # already carries its header bindings across parts.  A merged stub is
        # data within that schema, not a competing label-description topology.
        return selected, key_columns, inherited_values, False
    first_headers = (
        header_rows_by_part.get(first_part.table.table_id, ())
        if first_part is not None
        else ()
    )
    for part_index, part in enumerate(logical_table.parts):
        table = part.table
        local_headers = header_rows_by_part.get(table.table_id, ())
        start_row = max(local_headers) + 1 if local_headers else 0
        if table.column_count < 2:
            active_topology = None
            continue
        if (
            part_index > 0
            and local_headers
            and not _continued_part_repeats_reference_header(
                reference_table=first_part.table if first_part is not None else None,
                reference_rows=first_headers,
                candidate_table=table,
                candidate_rows=local_headers,
            )
        ):
            if active_topology is not None:
                ambiguous_continuation = True
            active_topology = None
            continue
        if table.row_count < 2 and active_topology is None:
            continue
        description_column = table.column_count - 1
        merged_label_regions = tuple(
            region
            for region in table.merged_regions
            if region.column < description_column
            and region.column + region.column_span <= description_column
            and region.row + region.row_span > start_row
            and (region.row_span > 1 or region.column_span > 1)
            and _normalized_key_text(
                _cell_text(
                    _cell_covering(table, region.row, region.column)
                )
            )
        )
        candidates: list[tuple[int, int, str]] = []
        if merged_label_regions:
            for row_index in range(start_row, table.row_count):
                if (
                    _is_effectively_full_width_row(table, row_index)
                    or not _normalized_key_text(
                        _cell_text(
                            _cell_covering(
                                table,
                                row_index,
                                description_column,
                            )
                        )
                    )
                ):
                    continue
                label = next(
                    (
                        (
                            column_index,
                            _normalized_key_text(
                                _cell_text(
                                    _cell_covering(
                                        table,
                                        row_index,
                                        column_index,
                                    )
                                )
                            ),
                        )
                        for column_index in range(
                            description_column - 1,
                            -1,
                            -1,
                        )
                        if _normalized_key_text(
                            _cell_text(
                                _cell_covering(
                                    table,
                                    row_index,
                                    column_index,
                                )
                            )
                        )
                    ),
                    None,
                )
                if label is not None:
                    candidates.append((row_index, label[0], label[1]))
        labels = tuple(label for _, _, label in candidates)
        if len(candidates) >= 2 and len(labels) == len(set(labels)):
            key_column = max(column for _, column, _ in candidates)
            selected.update(
                (table.table_id, row_index)
                for row_index, _, _ in candidates
            )
            key_columns[table.table_id] = key_column
            active_topology = _trailing_merged_label_topology(
                table=table,
                merged_label_regions=merged_label_regions,
                key_column=key_column,
                description_column=description_column,
            )
            continue

        if active_topology is None or part_index == 0:
            active_topology = None
            continue
        key_column, inherited_description_column, shared_values = active_topology
        if (
            inherited_description_column != description_column
            or key_column >= description_column
            or merged_label_regions
        ):
            active_topology = None
            if any(
                _row_has_substantive_text(table, row_index)
                for row_index in range(start_row, table.row_count)
            ):
                ambiguous_continuation = True
            continue
        continuation_rows: list[int] = []
        for row_index in range(start_row, table.row_count):
            if not _row_has_substantive_text(table, row_index):
                # An empty physical row closes the inherited run.  Never
                # resume an old classification later in the same part.
                if any(
                    _row_has_substantive_text(table, later)
                    for later in range(row_index + 1, table.row_count)
                ):
                    ambiguous_continuation = True
                break
            if (
                _is_effectively_full_width_row(table, row_index)
                or _is_structural_boundary_row(table, row_index)
            ):
                if any(
                    _row_has_substantive_text(table, later)
                    for later in range(row_index + 1, table.row_count)
                ):
                    ambiguous_continuation = True
                break
            prefix_has_text = any(
                _normalized_key_text(
                    _cell_text(_cell_covering(table, row_index, column_index))
                )
                for column_index in range(key_column)
            )
            if prefix_has_text:
                # A visible classification starts a new local group.  Without
                # its own merged/schema topology, carrying the previous page's
                # value into later blank rows would be a silent semantic guess.
                ambiguous_continuation = True
                break
            key_text = _normalized_key_text(
                _cell_text(_cell_covering(table, row_index, key_column))
            )
            has_payload = _row_has_payload_after_column(
                table,
                row_index=row_index,
                column_index=key_column,
            )
            if key_text and has_payload:
                continuation_rows.append(row_index)
                continue
            if not key_text and has_payload:
                # This may be a wrapped row and is attached only by the
                # contiguous blank-row state machine below.
                continue
            ambiguous_continuation = True
            break
        continuation_labels = tuple(
            _normalized_key_text(
                _cell_text(_cell_covering(table, row_index, key_column))
            )
            for row_index in continuation_rows
        )
        if len(continuation_labels) != len(set(continuation_labels)):
            ambiguous_continuation = True
            active_topology = None
            continue
        if not continuation_rows:
            active_topology = None
            continue
        for row_index in continuation_rows:
            coordinate = (table.table_id, row_index)
            selected.add(coordinate)
            inherited_values[coordinate] = shared_values
        key_columns[table.table_id] = key_column
    return selected, key_columns, inherited_values, ambiguous_continuation


def _continued_part_repeats_reference_header(
    *,
    reference_table: TableIR | None,
    reference_rows: tuple[int, ...],
    candidate_table: TableIR,
    candidate_rows: tuple[int, ...],
) -> bool:
    """Accept only a proven page-top repetition as a transparent header."""

    if reference_table is None or not reference_rows or not candidate_rows:
        return False
    reference_signature = _header_signature(reference_table, reference_rows)
    if _header_signature(candidate_table, candidate_rows) == reference_signature:
        return True
    return bool(
        len(reference_rows) == len(candidate_rows) == 1
        and candidate_rows == (0,)
        and _compatible_repeated_header_row(
            reference_table,
            reference_rows[0],
            candidate_table,
            candidate_rows[0],
        )
    )


def _trailing_merged_label_topology(
    *,
    table: TableIR,
    merged_label_regions: tuple[MergedRegion, ...],
    key_column: int,
    description_column: int,
) -> tuple[
    int,
    int,
    tuple[TableRecordSharedMergedValueV1, ...],
] | None:
    """Return the shared stub values that visibly continue past a page break."""

    values: list[TableRecordSharedMergedValueV1] = []
    for region in merged_label_regions:
        if (
            region.column >= key_column
            or region.row_span <= 1
            or region.row + region.row_span != table.row_count
        ):
            continue
        cell = _cell_covering(table, region.row, region.column)
        text = _cell_text(cell)
        if cell is None or not text:
            continue
        values.append(
            TableRecordSharedMergedValueV1(
                table_id=table.table_id,
                cell_id=cell.cell_id,
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
                text=text,
                source_refs=(_table_cell_ref(cell.cell_id),),
            )
        )
    if not values:
        return None
    return (
        key_column,
        description_column,
        tuple(sorted(values, key=lambda value: (value.column, value.row))),
    )


def _schema_key_columns(
    logical_table: _LogicalTable,
    *,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for event in _schema_events(
        logical_table,
        header_rows_by_part=header_rows_by_part,
    ):
        result[event.part.table.table_id] = event.key_column
    return result


def _attach_blank_continuation_rows(
    *,
    logical_table: _LogicalTable,
    groups: list[list[tuple[str, int]]],
    selected_coordinates: set[tuple[str, int]],
    headers_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordHeaderBindingV1, ...],
    ],
    header_catalog: dict[str, tuple[TableRecordHeaderBindingV1, ...]],
    key_columns: dict[str, int],
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    note_row_ids: set[str],
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> int:
    if len(logical_table.parts) < 2 or not groups:
        return 0
    group_index_by_coordinate = {
        coordinate: group_index
        for group_index, group in enumerate(groups)
        for coordinate in group
    }
    appended = 0
    inherited_key_column: int | None = None
    previous_coordinate: tuple[str, int] | None = None
    reference_part = logical_table.parts[0]
    reference_header_rows = header_rows_by_part.get(
        reference_part.table.table_id,
        (),
    )
    for part_index, part in enumerate(logical_table.parts):
        local_header_rows = header_rows_by_part.get(part.table.table_id, ())
        if (
            part_index > 0
            and local_header_rows
            and (
                not reference_header_rows
                or _header_signature(part.table, local_header_rows)
                != _header_signature(
                    reference_part.table,
                    reference_header_rows,
                )
            )
        ):
            # A parser-declared or merely majority-compatible header is not an
            # exact repeated page header.  Any text change closes the prior
            # record before blank-key attachment; the local detector may still
            # build independent records below that header.
            previous_coordinate = None
        key_column = key_columns.get(part.table.table_id, inherited_key_column)
        if key_column is not None:
            inherited_key_column = key_column
        if key_column is None:
            previous_coordinate = None
            continue
        for row_index in range(part.table.row_count):
            coordinate = (part.table.table_id, row_index)
            if row_index in local_header_rows:
                # Only a header compatible with the reference schema preserves
                # cross-part attachment; mismatches reset above.
                continue
            if coordinate in selected_coordinates:
                previous_coordinate = coordinate
                continue
            row_node = row_node_by_coordinate.get(coordinate)
            if (
                row_node is not None
                and row_node.structure_node_id in note_row_ids
            ):
                previous_coordinate = None
                continue
            if (
                _is_effectively_full_width_row(part.table, row_index)
                or _is_structural_boundary_row(part.table, row_index)
                or not _row_has_substantive_text(part.table, row_index)
            ):
                previous_coordinate = None
                continue
            key = _normalized_key_text(
                _cell_text(
                    _cell_covering(part.table, row_index, key_column)
                )
            )
            if key:
                # An unselected explicit key is an unproven business boundary;
                # later blank rows must never jump across it.
                previous_coordinate = None
                continue
            if any(
                _normalized_key_text(
                    _cell_text(_cell_covering(part.table, row_index, column))
                )
                for column in range(key_column)
            ):
                # A populated category/stub prefix with an empty business key
                # is a new boundary, not a wrapped continuation row.
                previous_coordinate = None
                continue
            if previous_coordinate is None:
                continue
            group_index = group_index_by_coordinate[previous_coordinate]
            groups[group_index].append(coordinate)
            group_index_by_coordinate[coordinate] = group_index
            selected_coordinates.add(coordinate)
            inherited_headers = (
                header_catalog.get(part.table.table_id, ())
                or headers_by_coordinate.get(previous_coordinate, ())
            )
            headers_by_coordinate[coordinate] = inherited_headers
            previous_coordinate = coordinate
            appended += 1
    return appended


def _primary_automatic_reason(reason_codes: tuple[str, ...]) -> str:
    priority = (
        "AUTO_HIERARCHICAL_LEAF_ROWS",
        "AUTO_MERGED_LABEL_DESCRIPTION_ROWS",
        "AUTO_TWO_LEVEL_PARAMETER_ROWS",
        "AUTO_VISUAL_HEADER_PARAMETER_ROWS",
        "AUTO_STABLE_KEY_SCHEMA",
        "AUTO_CONTINUOUS_ORDINAL_ROWS",
        "AUTO_EXPLICIT_NUMBERED_ROWS",
    )
    return next(
        (reason for reason in priority if reason in reason_codes),
        "AUTO_PROVEN_RECORD_TOPOLOGY",
    )


def _automatic_reason_message(reason_code: str) -> str:
    return {
        "AUTO_HIERARCHICAL_LEAF_ROWS": (
            "检测到合并主分组和独立叶锚点，已按叶业务行拆分。"
        ),
        "AUTO_MERGED_LABEL_DESCRIPTION_ROWS": (
            "检测到合并分类单元格下稳定的名称—说明行，已按业务行拆分。"
        ),
        "AUTO_TWO_LEVEL_PARAMETER_ROWS": (
            "检测到两级表头和稳定参数行，已按参数行拆分。"
        ),
        "AUTO_VISUAL_HEADER_PARAMETER_ROWS": (
            "检测到视觉表头和稳定业务行，已按业务行拆分。"
        ),
        "AUTO_STABLE_KEY_SCHEMA": (
            "检测到明确表头和稳定记录键，已按记录行拆分。"
        ),
        "AUTO_CONTINUOUS_ORDINAL_ROWS": (
            "检测到连续序号，已按序号行拆分。"
        ),
        "AUTO_EXPLICIT_NUMBERED_ROWS": (
            "检测到显式编号边界，已按编号行拆分。"
        ),
    }.get(reason_code, "检测到可稳定证明的记录结构，已按业务行拆分。")


def _build_row_group_record(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    coordinates: tuple[tuple[str, int], ...],
    headers: tuple[TableRecordHeaderBindingV1, ...],
    source_order: int,
    source_nodes_by_id: dict[str, SourceNode],
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    inherited_shared_values_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordSharedMergedValueV1, ...],
    ] | None = None,
) -> TableRecordV1:
    if not coordinates:
        raise ValueError("a table row record requires at least one source row")
    part_by_table_id = {
        part.table.table_id: part for part in logical_table.parts
    }
    row_bindings: list[TableRecordRowBindingV1] = []
    column_cells: dict[tuple[str, int], list[str]] = {}
    shared_values: dict[
        tuple[str, str],
        TableRecordSharedMergedValueV1,
    ] = {}
    notes: dict[tuple[str, int, str], TableRecordNoteV1] = {}
    figures: list[TableRecordVisualRefV1] = []
    images: list[TableRecordVisualRefV1] = []
    source_refs: list[StructureSourceRef] = []

    for table_id, row_index in coordinates:
        part = part_by_table_id.get(table_id)
        if part is None or not 0 <= row_index < part.table.row_count:
            raise ValueError("table row record references a row outside its fact")
        row_cells = _cells_covering_row(part.table, row_index)
        if not row_cells:
            raise ValueError("a table record row requires at least one native cell")
        row_node = row_node_by_coordinate.get((table_id, row_index))
        row_bindings.append(
            TableRecordRowBindingV1(
                table_id=table_id,
                row_index=row_index,
                cell_ids=tuple(cell.cell_id for cell in row_cells),
            )
        )
        for column_index in range(part.table.column_count):
            cell = _cell_covering(part.table, row_index, column_index)
            if cell is None:
                continue
            ids = column_cells.setdefault((table_id, column_index), [])
            if cell.cell_id not in ids:
                ids.append(cell.cell_id)

        row_shared = _unique_shared_values(
            (
                *_shared_values_for_row(
                    structure=structure,
                    logical_table=logical_table,
                    row_node=row_node,
                ),
                *(inherited_shared_values_by_coordinate or {}).get(
                    (table_id, row_index),
                    (),
                ),
            )
        )
        shared_values.update(
            {
                (value.table_id, value.cell_id): value
                for value in row_shared
            }
        )
        row_notes = _notes_for_row(
            structure=structure,
            logical_table=logical_table,
            row_node=row_node,
        )
        notes.update(
            {
                (note.table_id, note.source_order, note.text): note
                for note in row_notes
            }
        )
        row_figures, row_images = _visual_refs_for_row(
            source_ir=source_ir,
            structure=structure,
            logical_table=logical_table,
            part=part,
            row_index=row_index,
            row_node=row_node,
            source_nodes_by_id=source_nodes_by_id,
        )
        figures.extend(row_figures)
        images.extend(row_images)
        source_refs.extend(
            _record_source_refs(
                part=part,
                row_node=row_node,
                row_cells=row_cells,
                headers=headers,
                shared_values=row_shared,
                notes=row_notes,
                figure_refs=row_figures,
                image_refs=row_images,
            )
        )

    column_bindings = tuple(
        TableRecordColumnBindingV1(
            table_id=table_id,
            column_index=column_index,
            cell_ids=tuple(cell_ids),
        )
        for (table_id, column_index), cell_ids in column_cells.items()
    )
    return build_table_record(
        table_fact=logical_table.fact,
        kind=TableRecordKind.ROW,
        source_order=source_order,
        row_bindings=tuple(row_bindings),
        column_bindings=column_bindings,
        header_bindings=_unique_header_bindings(headers),
        shared_merged_values=tuple(shared_values.values()),
        notes=tuple(notes.values()),
        figure_refs=_unique_visual_refs(tuple(figures)),
        image_refs=_unique_visual_refs(tuple(images)),
        source_refs=normalize_table_record_source_refs(tuple(source_refs)),
    )


def _unique_shared_values(
    values: tuple[TableRecordSharedMergedValueV1, ...],
) -> tuple[TableRecordSharedMergedValueV1, ...]:
    return tuple(
        {
            (value.table_id, value.cell_id): value
            for value in values
        }.values()
    )


def _unique_header_bindings(
    bindings: tuple[TableRecordHeaderBindingV1, ...],
) -> tuple[TableRecordHeaderBindingV1, ...]:
    return tuple(
        {
            (
                binding.table_id,
                binding.axis,
                binding.level,
                binding.cell_ids,
            ): binding
            for binding in bindings
        }.values()
    )


def _row_has_substantive_text(table: TableIR, row_index: int) -> bool:
    return any(
        _normalized_key_text(_cell_text(cell))
        for cell in _cells_covering_row(table, row_index)
    )


def _build_row_record(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    part: _PhysicalTablePart,
    row_index: int,
    row_node: StructureNode | None,
    headers: tuple[TableRecordHeaderBindingV1, ...],
    source_order: int,
    source_nodes_by_id: dict[str, SourceNode],
) -> TableRecordV1:
    row_cells = _cells_covering_row(part.table, row_index)
    if not row_cells:
        raise ValueError("a table record row requires at least one native cell")
    columns = tuple(
        TableRecordColumnBindingV1(
            table_id=part.table.table_id,
            column_index=column_index,
            cell_ids=(cell.cell_id,),
        )
        for column_index in range(part.table.column_count)
        if (cell := _cell_covering(part.table, row_index, column_index)) is not None
    )
    shared_values = _shared_values_for_row(
        structure=structure,
        logical_table=logical_table,
        row_node=row_node,
    )
    notes = _notes_for_row(
        structure=structure,
        logical_table=logical_table,
        row_node=row_node,
    )
    figure_refs, image_refs = _visual_refs_for_row(
        source_ir=source_ir,
        structure=structure,
        logical_table=logical_table,
        part=part,
        row_index=row_index,
        row_node=row_node,
        source_nodes_by_id=source_nodes_by_id,
    )
    source_refs = _record_source_refs(
        part=part,
        row_node=row_node,
        row_cells=row_cells,
        headers=headers,
        shared_values=shared_values,
        notes=notes,
        figure_refs=figure_refs,
        image_refs=image_refs,
    )
    return build_table_record(
        table_fact=logical_table.fact,
        kind=TableRecordKind.ROW,
        source_order=source_order,
        row_bindings=(
            TableRecordRowBindingV1(
                table_id=part.table.table_id,
                row_index=row_index,
                cell_ids=tuple(cell.cell_id for cell in row_cells),
            ),
        ),
        column_bindings=columns,
        header_bindings=headers,
        shared_merged_values=shared_values,
        notes=notes,
        figure_refs=figure_refs,
        image_refs=image_refs,
        source_refs=source_refs,
    )


def _unique_continuation_groups(
    table_nodes: tuple[StructureNode, ...],
    relations: tuple[StructureRelation, ...],
) -> tuple[tuple[StructureNode, ...], ...]:
    by_id = {node.structure_node_id: node for node in table_nodes}
    outgoing: dict[str, list[str]] = {}
    incoming: dict[str, list[str]] = {}
    for relation in relations:
        if (
            relation.kind is not StructureRelationKind.CONTINUES
            or relation.source_structure_node_id not in by_id
            or relation.target_structure_node_id not in by_id
        ):
            continue
        outgoing.setdefault(relation.source_structure_node_id, []).append(
            relation.target_structure_node_id
        )
        incoming.setdefault(relation.target_structure_node_id, []).append(
            relation.source_structure_node_id
        )
    successor = {
        source_id: targets[0]
        for source_id, targets in outgoing.items()
        if len(set(targets)) == 1
        and len(set(incoming.get(targets[0], ()))) == 1
    }
    predecessor = {target_id: source_id for source_id, target_id in successor.items()}

    groups: list[tuple[StructureNode, ...]] = []
    visited: set[str] = set()
    for node in table_nodes:
        node_id = node.structure_node_id
        if node_id in visited or node_id in predecessor:
            continue
        chain: list[StructureNode] = []
        cursor = node_id
        local: set[str] = set()
        while cursor in by_id and cursor not in local and cursor not in visited:
            local.add(cursor)
            visited.add(cursor)
            chain.append(by_id[cursor])
            cursor = successor.get(cursor, "")
        groups.append(
            tuple(
                sorted(
                    chain,
                    key=lambda item: (item.source_order, item.structure_node_id),
                )
            )
        )
    for node in table_nodes:
        if node.structure_node_id not in visited:
            groups.append((node,))
    return tuple(
        sorted(
            groups,
            key=lambda group: (
                group[0].source_order,
                group[0].structure_node_id,
            ),
        )
    )


def _children_by_parent(
    nodes: tuple[StructureNode, ...],
) -> dict[str, tuple[StructureNode, ...]]:
    collected: dict[str, list[StructureNode]] = {}
    for node in nodes:
        if node.parent_structure_node_id is not None:
            collected.setdefault(node.parent_structure_node_id, []).append(node)
    return {
        parent_id: tuple(
            sorted(children, key=lambda item: (item.source_order, item.structure_node_id))
        )
        for parent_id, children in collected.items()
    }


def _row_nodes_for_logical_table(
    logical_table: _LogicalTable,
    structure: StructureSnapshot,
) -> dict[tuple[str, int], StructureNode]:
    table_id_by_structure_id: dict[str, str] = {}
    for table_node in logical_table.table_nodes:
        primary = next(
            (
                part
                for part in logical_table.parts
                if part.table_structure_node.structure_node_id
                == table_node.structure_node_id
            ),
            None,
        )
        if primary is not None:
            table_id_by_structure_id[table_node.structure_node_id] = (
                primary.table.table_id
            )
    return {
        (table_id_by_structure_id[node.parent_structure_node_id], node.table_row_index): node
        for node in structure.nodes
        if node.kind is StructureNodeKind.TABLE_ROW
        and node.parent_structure_node_id in table_id_by_structure_id
        and node.table_row_index is not None
    }


def _header_bindings_for_rows(
    part: _PhysicalTablePart,
    header_rows: tuple[int, ...],
) -> tuple[TableRecordHeaderBindingV1, ...]:
    if not header_rows:
        return ()
    bindings: list[TableRecordHeaderBindingV1] = []
    for cell in sorted(
        (
            cell
            for cell in part.table.cells
            if any(
                cell.row <= row_index < cell.row + cell.row_span
                for row_index in header_rows
            )
        ),
        key=lambda item: (item.row, item.column, item.cell_id),
    ):
        text = _cell_text(cell)
        if not text:
            continue
        bindings.append(
            TableRecordHeaderBindingV1(
                table_id=part.table.table_id,
                axis=TableRecordHeaderAxis.COLUMN,
                level=cell.row,
                cell_ids=(cell.cell_id,),
                text=text,
                source_refs=(_table_cell_ref(cell.cell_id),),
            )
        )
    return tuple(bindings)


def _leading_header_rows(table: TableIR) -> tuple[int, ...]:
    declared = _declared_leading_header_rows(table)
    if declared:
        return declared
    stub = _stub_header_coordinate(table)
    if stub is None:
        return ()
    header_row, _ = stub
    header_end = max(
        (
            cell.row + cell.row_span - 1
            for cell in table.cells
            if cell.row == header_row
        ),
        default=header_row,
    )
    rows = list(range(header_row, min(header_end, table.row_count - 1) + 1))
    # A title is inherited only when its topology explicitly spans the whole
    # table and it is directly adjacent to the schema.  Earlier page content
    # belongs to the parent clause, not to every row's column headers.
    if header_row > 0 and _is_full_width_title_row(table, header_row - 1):
        rows.insert(0, header_row - 1)
    return tuple(rows)


def _declared_leading_header_rows(table: TableIR) -> tuple[int, ...]:
    if not table.header_regions:
        return ()
    header_coverage = {
        (row, column)
        for region in table.header_regions
        for row in range(region.row, region.row + region.row_span)
        for column in range(region.column, region.column + region.column_span)
    }
    rows: list[int] = []
    for row_index in range(table.row_count):
        row_cells = _cells_covering_row(table, row_index)
        non_empty_cells = tuple(cell for cell in row_cells if _cell_text(cell))
        if not any(row == row_index for row, _ in header_coverage):
            break
        if any(
            not all(
                (row_index, column) in header_coverage
                for column in range(cell.column, cell.column + cell.column_span)
            )
            for cell in non_empty_cells
        ):
            break
        rows.append(row_index)
    return tuple(rows)


def _stub_header_coordinate(table: TableIR) -> tuple[int, int] | None:
    matches = _stub_header_coordinates(table)
    # Multiple candidate rows mean the physical table itself contains repeated
    # schemas.  A leading-header helper cannot choose one, but the segment
    # scanner consumes every locally proven schema in source order.
    return matches[0] if len(matches) == 1 else None


def _stub_header_coordinates(table: TableIR) -> tuple[tuple[int, int], ...]:
    declared_header_signatures = {
        _row_signature(table, row_index)
        for row_index in _ordinal_safe_leading_header_rows(
            table,
            _declared_leading_header_rows(table),
        )
    }
    proven_signatures = set(declared_header_signatures)
    matches: list[tuple[int, int]] = []
    for cell in sorted(
        table.cells,
        key=lambda item: (item.row, item.column, item.cell_id),
    ):
        if not (
            _normalized_stub_header_aliases(_cell_text(cell))
            & _STUB_HEADER_LABELS
        ):
            continue
        if any(
            _cell_text(_cell_covering(table, cell.row, earlier_column))
            for earlier_column in range(cell.column)
        ):
            continue
        signature = _row_signature(table, cell.row)
        is_leading_schema = all(
            not any(
                _cell_text(row_cell)
                for row_cell in _cells_covering_row(table, earlier_row)
            )
            or _is_effectively_full_width_row(table, earlier_row)
            for earlier_row in range(cell.row)
        )
        follows_structural_boundary = (
            cell.row > 0
            and _is_structural_boundary_row(table, cell.row - 1)
        )
        repeats_proven_schema = signature in proven_signatures
        if not (
            is_leading_schema
            or follows_structural_boundary
            or repeats_proven_schema
        ):
            continue
        matches.append((cell.row, cell.column))
        proven_signatures.add(signature)
    return tuple(matches)


def _header_key_column(
    table: TableIR,
    header_rows: tuple[int, ...],
) -> int:
    stub = _stub_header_coordinate(table)
    if stub is not None and stub[0] in header_rows:
        return stub[1]
    # A parser-declared schema is authoritative enough to expose its leading
    # populated column as the record/stub key.
    for column_index in range(table.column_count):
        if any(
            _cell_text(_cell_covering(table, row_index, column_index))
            for row_index in header_rows
        ):
            return column_index
    return 0


def _schema_record_coordinates(
    *,
    logical_table: _LogicalTable,
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    note_row_ids: set[str],
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[
    set[tuple[str, int]],
    dict[tuple[str, int], tuple[TableRecordHeaderBindingV1, ...]],
]:
    """Project only continuous rows inside a locally proven table schema.

    One physical TableIR may be a whole-page layout grid containing several
    document sections.  A schema starts at parser-declared headers or an exact
    stub header such as ``序号``/``编号``.  It ends at a blank/full-width
    structural row or the next schema.  This changes only the derived record
    view; the immutable whole-table fact remains untouched.
    """

    events = {
        (event.part.table.table_id, event.start_row): event
        for event in _schema_events(
            logical_table,
            header_rows_by_part=header_rows_by_part,
        )
    }
    selected: set[tuple[str, int]] = set()
    headers_by_coordinate: dict[
        tuple[str, int],
        tuple[TableRecordHeaderBindingV1, ...],
    ] = {}
    active: _SchemaEvent | None = None
    pending: list[tuple[_PhysicalTablePart, int]] = []

    def finalize() -> None:
        nonlocal pending
        if active is not None and _rows_have_stable_key(
            tuple(pending),
            active.key_column,
        ):
            for row_part, row_index in pending:
                coordinate = (row_part.table.table_id, row_index)
                selected.add(coordinate)
                headers_by_coordinate[coordinate] = active.headers
        pending = []

    for part in logical_table.parts:
        row_index = 0
        while row_index < part.table.row_count:
            coordinate = (part.table.table_id, row_index)
            event = events.get(coordinate)
            if event is not None:
                finalize()
                active = event
                row_index = event.end_row + 1
                continue

            row_node = row_node_by_coordinate.get(coordinate)
            is_note = (
                row_node is not None
                and row_node.structure_node_id in note_row_ids
            )
            if (
                is_note
                or _is_structural_boundary_row(part.table, row_index)
                or _is_effectively_full_width_row(part.table, row_index)
            ):
                finalize()
                active = None
                row_index += 1
                continue

            populated = tuple(
                cell
                for cell in _cells_covering_row(part.table, row_index)
                if _cell_text(cell)
            )
            if not populated:
                finalize()
                active = None
                row_index += 1
                continue
            if active is not None:
                key_text = _normalized_key_text(
                    _cell_text(
                        _cell_covering(
                            part.table,
                            row_index,
                            active.key_column,
                        )
                    )
                )
                if not key_text:
                    finalize()
                    active = None
                else:
                    pending.append((part, row_index))
            row_index += 1
    finalize()
    return selected, headers_by_coordinate


def _schema_events(
    logical_table: _LogicalTable,
    *,
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[_SchemaEvent, ...]:
    events: dict[tuple[str, int], _SchemaEvent] = {}
    for part in logical_table.parts:
        declared_rows = _ordinal_safe_leading_header_rows(
            part.table,
            _declared_leading_header_rows(part.table),
        )
        if declared_rows:
            event = _schema_event(
                part=part,
                start_row=declared_rows[0],
                end_row=declared_rows[-1],
                binding_rows=declared_rows,
                key_column=_header_key_column(part.table, declared_rows),
                require_multiple_headers=False,
            )
            if event is not None:
                events[(part.table.table_id, event.start_row)] = event

        catalog_rows = header_rows_by_part.get(part.table.table_id, ())
        if catalog_rows and (part.table.table_id, catalog_rows[0]) not in events:
            event = _schema_event(
                part=part,
                start_row=catalog_rows[0],
                end_row=catalog_rows[-1],
                binding_rows=catalog_rows,
                key_column=_header_key_column(part.table, catalog_rows),
                require_multiple_headers=False,
            )
            if event is not None:
                events[(part.table.table_id, event.start_row)] = event

        for header_row, key_column in _stub_header_coordinates(part.table):
            header_end = max(
                (
                    cell.row + cell.row_span - 1
                    for cell in part.table.cells
                    if cell.row == header_row
                ),
                default=header_row,
            )
            binding_rows = list(
                range(
                    header_row,
                    min(header_end, part.table.row_count - 1) + 1,
                )
            )
            if (
                header_row > 0
                and _is_effectively_full_width_row(part.table, header_row - 1)
                and not _is_structural_boundary_row(part.table, header_row - 1)
            ):
                binding_rows.insert(0, header_row - 1)
            event = _schema_event(
                part=part,
                start_row=header_row,
                end_row=min(header_end, part.table.row_count - 1),
                binding_rows=tuple(binding_rows),
                key_column=key_column,
                require_multiple_headers=True,
            )
            if event is not None:
                events[(part.table.table_id, event.start_row)] = event
    return tuple(
        sorted(
            events.values(),
            key=lambda event: (
                event.part.source_node.source_order,
                event.start_row,
            ),
        )
    )


def _schema_event(
    *,
    part: _PhysicalTablePart,
    start_row: int,
    end_row: int,
    binding_rows: tuple[int, ...],
    key_column: int,
    require_multiple_headers: bool,
) -> _SchemaEvent | None:
    headers = _header_bindings_for_rows(part, binding_rows)
    header_row_values = tuple(
        _cell_text(_cell_covering(part.table, start_row, column_index))
        for column_index in range(part.table.column_count)
    )
    if (
        not headers
        or key_column >= part.table.column_count
        or not header_row_values[key_column]
        or (
            require_multiple_headers
            and len(tuple(value for value in header_row_values if value)) < 2
        )
    ):
        return None
    return _SchemaEvent(
        part=part,
        start_row=start_row,
        end_row=end_row,
        key_column=key_column,
        headers=headers,
    )


def _rows_have_stable_key(
    rows: tuple[tuple[_PhysicalTablePart, int], ...],
    column_index: int,
) -> bool:
    if not rows:
        return False
    values = tuple(
        _normalized_key_text(
            _cell_text(_cell_covering(part.table, row_index, column_index))
        )
        for part, row_index in rows
    )
    return all(values) and len(values) == len(set(values))


def _data_rows(
    *,
    logical_table: _LogicalTable,
    row_node_by_coordinate: dict[tuple[str, int], StructureNode],
    note_row_ids: set[str],
    header_rows_by_part: dict[str, tuple[int, ...]],
) -> tuple[tuple[_PhysicalTablePart, int], ...]:
    schema_rows_by_table: dict[str, set[int]] = {}
    for event in _schema_events(
        logical_table,
        header_rows_by_part=header_rows_by_part,
    ):
        schema_rows_by_table.setdefault(event.part.table.table_id, set()).update(
            range(event.start_row, event.end_row + 1)
        )
    result: list[tuple[_PhysicalTablePart, int]] = []
    for part in logical_table.parts:
        header_rows = set(header_rows_by_part.get(part.table.table_id, ()))
        header_rows.update(schema_rows_by_table.get(part.table.table_id, ()))
        for row_index in range(part.table.row_count):
            if (
                row_index in header_rows
                or _is_structural_boundary_row(part.table, row_index)
            ):
                continue
            row_node = row_node_by_coordinate.get((part.table.table_id, row_index))
            if row_node is not None and row_node.structure_node_id in note_row_ids:
                continue
            if any(_cell_text(cell) for cell in _cells_covering_row(part.table, row_index)):
                result.append((part, row_index))
    return tuple(result)


def _ordinal_record_coordinates(
    *,
    logical_table: _LogicalTable,
    data_rows: tuple[tuple[_PhysicalTablePart, int], ...],
) -> tuple[
    set[tuple[str, int]],
    dict[str, tuple[int, ...]],
]:
    """Recognize row topology, never requirement semantics.

    The first physical part must prove a complete ``1, 2, ...`` suffix with at
    least two rows.  Once that proof exists, a continuation part may continue
    with strictly increasing source ordinals or restart at one.  A source gap
    remains a gap: projection never fabricates a missing ordinal.  Non-numeric
    rows before the sequence are retained as inherited table context; numeric
    ambiguity keeps the table whole.
    """

    rows_by_table: dict[str, list[int]] = {}
    for part, row_index in data_rows:
        rows_by_table.setdefault(part.table.table_id, []).append(row_index)

    selected: set[tuple[str, int]] = set()
    context_rows: dict[str, tuple[int, ...]] = {}
    topology_proven = False
    expected_next: int | None = None
    for part in logical_table.parts:
        row_indexes = tuple(rows_by_table.get(part.table.table_id, ()))
        if not row_indexes:
            continue
        continuation_was_proven = topology_proven
        for run_index, row_run in enumerate(_contiguous_row_runs(row_indexes)):
            candidate = _ordinal_suffix(
                part.table,
                row_run,
                require_start_one=not topology_proven,
                expected_next=expected_next,
                allow_single=continuation_was_proven and run_index == 0,
                allow_gaps=topology_proven,
            )
            if candidate is None:
                continue
            record_rows, inherited_context = candidate
            if not topology_proven and len(record_rows) < 2:
                continue
            selected.update(
                (part.table.table_id, row_index) for row_index in record_rows
            )
            if inherited_context and part.table.table_id not in context_rows:
                context_rows[part.table.table_id] = inherited_context
            topology_proven = True
            last_value = _ordinal_value(
                _cell_text(_cell_covering(part.table, record_rows[-1], 0))
            )
            expected_next = None if last_value is None else last_value + 1
    return selected, context_rows


def _contiguous_row_runs(
    row_indexes: tuple[int, ...],
) -> tuple[tuple[int, ...], ...]:
    runs: list[list[int]] = []
    for row_index in row_indexes:
        if not runs or row_index != runs[-1][-1] + 1:
            runs.append([row_index])
        else:
            runs[-1].append(row_index)
    return tuple(tuple(run) for run in runs)


def _ordinal_suffix(
    table: TableIR,
    row_indexes: tuple[int, ...],
    *,
    require_start_one: bool,
    expected_next: int | None,
    allow_single: bool,
    allow_gaps: bool,
) -> tuple[tuple[int, ...], tuple[int, ...]] | None:
    for start_position, row_index in enumerate(row_indexes):
        values = tuple(
            _ordinal_value(_cell_text(_cell_covering(table, candidate_row, 0)))
            for candidate_row in row_indexes[start_position:]
        )
        if not values or any(value is None for value in values):
            continue
        integer_values = tuple(int(value) for value in values if value is not None)
        if len(integer_values) < (1 if allow_single else 2):
            continue
        if any(
            (
                later <= earlier
                if allow_gaps
                else later != earlier + 1
            )
            for earlier, later in zip(integer_values, integer_values[1:])
        ):
            continue
        if any(
            later_row != earlier_row + 1
            for earlier_row, later_row in zip(
                row_indexes[start_position:],
                row_indexes[start_position + 1 :],
            )
        ):
            continue
        first_value = integer_values[0]
        if require_start_one:
            if first_value != 1:
                continue
        elif first_value not in {1, expected_next}:
            continue
        earlier_rows = row_indexes[:start_position]
        if any(
            _ordinal_value(_cell_text(_cell_covering(table, context_row, 0)))
            is not None
            for context_row in earlier_rows
        ):
            continue
        context = (
            (earlier_rows[-1],)
            if earlier_rows
            and _is_row_header_context(table, earlier_rows[-1])
            else ()
        )
        return row_indexes[start_position:], context
    return None


def _ordinal_value(value: str) -> int | None:
    match = _ORDINAL_CELL_PATTERN.fullmatch(value)
    return int(match.group(1)) if match is not None else None


def _normalized_stub_header(value: str) -> str:
    return re.sub(r"[\s._．。:：\-—/\\]+", "", value).casefold()


def _normalized_stub_header_aliases(value: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value)
    aliases = {
        _normalized_stub_header(part)
        for part in re.split(r"[/\\|\r\n;；()（）]+", normalized)
        if part.strip()
    }
    whole = _normalized_stub_header(normalized)
    if whole:
        aliases.add(whole)
    return frozenset(alias for alias in aliases if alias)


def _is_full_width_title_row(table: TableIR, row_index: int) -> bool:
    anchors = tuple(
        cell
        for cell in table.cells
        if cell.row == row_index and _cell_text(cell)
    )
    return (
        len(anchors) == 1
        and anchors[0].column == 0
        and anchors[0].column_span == table.column_count
    )


def _is_effectively_full_width_row(table: TableIR, row_index: int) -> bool:
    populated = tuple(
        cell
        for cell in _cells_covering_row(table, row_index)
        if _cell_text(cell)
    )
    return (
        len(populated) == 1
        and populated[0].row == row_index
        and populated[0].column == 0
        and populated[0].column_span == table.column_count
    )


def _is_structural_boundary_row(table: TableIR, row_index: int) -> bool:
    populated = tuple(
        cell
        for cell in _cells_covering_row(table, row_index)
        if _cell_text(cell)
    )
    if not (
        len(populated) == 1
        and populated[0].row == row_index
        and populated[0].column == 0
        and populated[0].column_span == table.column_count
    ):
        return False
    text = _cell_text(populated[0])
    normalized = unicodedata.normalize("NFKC", text).strip()
    return bool(
        _FULL_WIDTH_NUMBERED_BOUNDARY.match(normalized)
        or _is_appendix_boundary(normalized)
    )


def _is_appendix_boundary(value: str) -> bool:
    if not value or value.startswith(("本附录", "本附件")):
        return False
    chinese = _APPENDIX_BOUNDARY.fullmatch(value)
    english = _ENGLISH_APPENDIX_BOUNDARY.fullmatch(value)
    if chinese is None and english is None:
        return False
    label = re.match(
        r"^\s*(?:附录|附件)\s*[A-Z0-9一二三四五六七八九十]{0,3}",
        value,
        re.IGNORECASE,
    )
    if label is None:
        label = re.match(
            r"^\s*(?:appendix|annex)\s+[A-Z0-9]{1,3}",
            value,
            re.IGNORECASE,
        )
    tail = value[label.end() :].lstrip(" -—:：（(").strip() if label else ""
    tail = re.sub(
        r"^(?:规范性|资料性|normative|informative)[）)]?\s*",
        "",
        tail,
        flags=re.IGNORECASE,
    ).lstrip(" -—:：")
    if re.match(
        r"^(?:中|内|的|所述|所示|规定|要求|见|详见|参见|"
        r"of\b|is\b|are\b|see\b|refer\b)",
        tail,
        re.IGNORECASE,
    ):
        return False
    return len(tail) <= 100 and not re.search(r"[。！？!?;；]$", tail)


def _is_row_header_context(table: TableIR, row_index: int) -> bool:
    populated = tuple(
        cell
        for cell in _cells_covering_row(table, row_index)
        if _cell_text(cell)
    )
    return len(populated) >= 2 or _is_full_width_title_row(table, row_index)


def _shared_values_for_row(
    *,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    row_node: StructureNode | None,
) -> tuple[TableRecordSharedMergedValueV1, ...]:
    if row_node is None:
        return ()
    node_by_id = {node.structure_node_id: node for node in structure.nodes}
    row_node_by_coordinate = _row_nodes_for_logical_table(
        logical_table,
        structure,
    )
    coordinate_by_row_node_id = {
        node.structure_node_id: coordinate
        for coordinate, node in row_node_by_coordinate.items()
    }
    target_coordinate = coordinate_by_row_node_id.get(
        row_node.structure_node_id
    )
    cell_catalog = {
        cell.cell_id: (part, cell)
        for part in logical_table.parts
        for cell in part.table.cells
    }
    values: list[TableRecordSharedMergedValueV1] = []
    for relation in structure.relations:
        if (
            relation.kind is not StructureRelationKind.APPLIES_TO
            or relation.target_structure_node_id != row_node.structure_node_id
        ):
            continue
        source_node = node_by_id.get(relation.source_structure_node_id)
        if (
            source_node is None
            or source_node.boundary_basis
            is not StructureBoundaryBasis.MERGED_RANGE
        ):
            continue
        for ref in source_node.source_refs:
            if ref.kind is not StructureSourceRefKind.TABLE_CELL:
                continue
            catalog_item = cell_catalog.get(ref.source_id)
            if catalog_item is None:
                continue
            part, cell = catalog_item
            text = _cell_text(cell)
            if not text or (cell.row_span == 1 and cell.column_span == 1):
                continue
            if (
                target_coordinate is not None
                and _is_effectively_full_width_row(part.table, cell.row)
                and _coordinate_follows(
                    logical_table,
                    (part.table.table_id, cell.row),
                    target_coordinate,
                )
            ):
                # A trailing full-width merged row is a table note, not a
                # shared value.  _notes_for_row consumes the same grounded
                # APPLIES_TO relation without copying the note into a record.
                continue
            values.append(
                TableRecordSharedMergedValueV1(
                    table_id=part.table.table_id,
                    cell_id=cell.cell_id,
                    row=cell.row,
                    column=cell.column,
                    row_span=cell.row_span,
                    column_span=cell.column_span,
                    text=text,
                    source_refs=(_table_cell_ref(cell.cell_id),),
                )
            )
    return tuple(
        sorted(
            {
                (value.table_id, value.cell_id): value for value in values
            }.values(),
            key=lambda value: (value.table_id, value.row, value.column),
        )
    )


def _notes_for_row(
    *,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    row_node: StructureNode | None,
) -> tuple[TableRecordNoteV1, ...]:
    notes: list[TableRecordNoteV1] = []
    for part in logical_table.parts:
        for index, text in enumerate(part.table.notes):
            if text.strip():
                notes.append(
                    TableRecordNoteV1(
                        table_id=part.table.table_id,
                        source_order=part.source_node.source_order * 1_000_000 + index,
                        text=text.strip(),
                        source_refs=(
                            _source_node_ref(part.source_node.source_node_id),
                        ),
                    )
                )
    if row_node is not None:
        node_by_id = {node.structure_node_id: node for node in structure.nodes}
        row_node_by_coordinate = _row_nodes_for_logical_table(
            logical_table,
            structure,
        )
        coordinate_by_row_node_id = {
            node.structure_node_id: coordinate
            for coordinate, node in row_node_by_coordinate.items()
        }
        target_coordinate = coordinate_by_row_node_id.get(
            row_node.structure_node_id
        )
        part_by_structure_id: dict[str, _PhysicalTablePart] = {}
        for part in logical_table.parts:
            part_by_structure_id.setdefault(
                part.table_structure_node.structure_node_id,
                part,
            )
        cell_catalog = {
            cell.cell_id: (part, cell)
            for part in logical_table.parts
            for cell in part.table.cells
        }
        for relation in structure.relations:
            if (
                relation.kind is not StructureRelationKind.APPLIES_TO
                or relation.target_structure_node_id != row_node.structure_node_id
            ):
                continue
            source_node = node_by_id.get(relation.source_structure_node_id)
            if source_node is None:
                continue
            if source_node.kind is StructureNodeKind.TABLE_ROW:
                part = part_by_structure_id.get(
                    source_node.parent_structure_node_id or ""
                )
                if part is None or source_node.table_row_index is None:
                    continue
                text = " | ".join(
                    value
                    for cell in _cells_covering_row(
                        part.table,
                        source_node.table_row_index,
                    )
                    if (value := _cell_text(cell))
                )
                if text:
                    notes.append(
                        TableRecordNoteV1(
                            table_id=part.table.table_id,
                            source_order=source_node.source_order,
                            text=text,
                            source_refs=normalize_table_record_source_refs(
                                source_node.source_refs
                            ),
                        )
                    )
                continue
            if (
                source_node.kind is not StructureNodeKind.CONTENT
                or source_node.boundary_basis
                is not StructureBoundaryBasis.MERGED_RANGE
                or target_coordinate is None
            ):
                continue
            for ref in source_node.source_refs:
                if ref.kind is not StructureSourceRefKind.TABLE_CELL:
                    continue
                catalog_item = cell_catalog.get(ref.source_id)
                if catalog_item is None:
                    continue
                part, cell = catalog_item
                source_coordinate = (part.table.table_id, cell.row)
                text = _cell_text(cell)
                if (
                    not text
                    or not _is_effectively_full_width_row(part.table, cell.row)
                    or not _coordinate_follows(
                        logical_table,
                        source_coordinate,
                        target_coordinate,
                    )
                ):
                    continue
                notes.append(
                    TableRecordNoteV1(
                        table_id=part.table.table_id,
                        source_order=source_node.source_order,
                        text=text,
                        source_refs=normalize_table_record_source_refs(
                            source_node.source_refs
                        ),
                    )
                )
    return tuple(
        sorted(
            {
                (note.table_id, note.source_order, note.text): note for note in notes
            }.values(),
            key=lambda note: (note.source_order, note.table_id, note.text),
        )
    )


def _visual_refs_for_row(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    part: _PhysicalTablePart,
    row_index: int,
    row_node: StructureNode | None,
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[
    tuple[TableRecordVisualRefV1, ...],
    tuple[TableRecordVisualRefV1, ...],
]:
    asset_kind_by_id = {asset.asset_id: asset.kind for asset in source_ir.assets}
    row_cell_ids = {
        cell.cell_id for cell in _cells_covering_row(part.table, row_index)
    }
    candidates: list[tuple[SourceNode, str | None, tuple[StructureSourceRef, ...]]] = []

    if row_node is not None:
        for ref in row_node.source_refs:
            source_node = source_nodes_by_id.get(ref.source_id)
            if (
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_node is not None
                and source_node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
                and source_node.asset_ref is not None
            ):
                column_index = source_node.native_locator.column_index
                cell = (
                    _cell_covering(part.table, row_index, column_index)
                    if column_index is not None
                    else None
                )
                candidates.append(
                    (
                        source_node,
                        cell.cell_id if cell is not None else None,
                        normalize_table_record_source_refs(
                            (
                                ref,
                                *(
                                    (_table_cell_ref(cell.cell_id),)
                                    if cell is not None
                                    else ()
                                ),
                            )
                        ),
                    )
                )

    table_structure_ids = {
        node.structure_node_id for node in logical_table.table_nodes
    }
    for relation in structure.relations:
        if (
            relation.kind
            not in {
                StructureRelationKind.PLACED_IN_CELL,
                StructureRelationKind.PLACED_OVER_CELLS,
            }
            or relation.target_structure_node_id not in table_structure_ids
        ):
            continue
        matching_cells = tuple(
            ref.source_id
            for ref in relation.source_refs
            if ref.kind is StructureSourceRefKind.TABLE_CELL
            and ref.source_id in row_cell_ids
        )
        if not matching_cells:
            continue
        for ref in relation.source_refs:
            source_node = source_nodes_by_id.get(ref.source_id)
            if (
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_node is not None
                and source_node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
                and source_node.asset_ref is not None
            ):
                # One PLACED_OVER_CELLS relation represents one visual
                # placement, even when that placement covers several cells
                # in the current record row.  Preserve every covered cell in
                # source_refs, but bind the visual once to a deterministic
                # anchor cell so Review and Excel do not render duplicates.
                cell_order = {
                    cell.cell_id: (cell.row, cell.column, cell.cell_id)
                    for cell in _cells_covering_row(part.table, row_index)
                }
                anchor_cell_id = min(
                    matching_cells,
                    key=lambda cell_id: cell_order[cell_id],
                )
                candidates.append(
                    (
                        source_node,
                        anchor_cell_id,
                        normalize_table_record_source_refs(
                            relation.source_refs
                        ),
                    )
                )

    figures: list[TableRecordVisualRefV1] = []
    images: list[TableRecordVisualRefV1] = []
    for source_node, cell_id, refs in candidates:
        visual = TableRecordVisualRefV1(
            asset_id=source_node.asset_ref or "",
            table_id=part.table.table_id,
            cell_id=cell_id,
            source_refs=refs,
        )
        if (
            source_node.kind is SourceNodeKind.FIGURE
            or asset_kind_by_id.get(source_node.asset_ref) is SourceAssetKind.FIGURE
        ):
            figures.append(visual)
        else:
            images.append(visual)
    return (
        _unique_visual_refs(tuple(figures)),
        _unique_visual_refs(tuple(images)),
    )


def _record_source_refs(
    *,
    part: _PhysicalTablePart,
    row_node: StructureNode | None,
    row_cells: tuple[TableCellIR, ...],
    headers: tuple[TableRecordHeaderBindingV1, ...],
    shared_values: tuple[TableRecordSharedMergedValueV1, ...],
    notes: tuple[TableRecordNoteV1, ...],
    figure_refs: tuple[TableRecordVisualRefV1, ...],
    image_refs: tuple[TableRecordVisualRefV1, ...],
) -> tuple[StructureSourceRef, ...]:
    return normalize_table_record_source_refs(
        (
            _source_node_ref(part.source_node.source_node_id),
            *tuple(_table_cell_ref(cell.cell_id) for cell in row_cells),
            *(row_node.source_refs if row_node is not None else ()),
            *(ref for header in headers for ref in header.source_refs),
            *(ref for value in shared_values for ref in value.source_refs),
            *(ref for note in notes for ref in note.source_refs),
            *(ref for visual in (*figure_refs, *image_refs) for ref in visual.source_refs),
        )
    )


def _manual_header_bindings(
    *,
    part: _PhysicalTablePart,
    cell_ids: tuple[str, ...],
    axis: TableRecordHeaderAxis,
) -> tuple[TableRecordHeaderBindingV1, ...]:
    if not cell_ids or len(cell_ids) != len(set(cell_ids)):
        raise ValueError("manual header cell identities must be non-empty and unique")
    cell_by_id = {cell.cell_id: cell for cell in part.table.cells}
    bindings: list[TableRecordHeaderBindingV1] = []
    for level, cell_id in enumerate(cell_ids):
        cell = cell_by_id.get(cell_id)
        if cell is None:
            raise ValueError("manual header cell does not belong to the table")
        text = _cell_text(cell)
        if not text:
            raise ValueError("manual header cells require source text")
        bindings.append(
            TableRecordHeaderBindingV1(
                table_id=part.table.table_id,
                axis=axis,
                level=level,
                cell_ids=(cell.cell_id,),
                text=text,
                source_refs=(_table_cell_ref(cell.cell_id),),
            )
        )
    return tuple(bindings)


def _table_visual_placement_source_node_ids(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_table: _LogicalTable,
    source_nodes_by_id: dict[str, SourceNode],
) -> set[str]:
    """Return source identities for visuals physically placed in the table.

    ``TableIR.visual_asset_ref`` is a rendering of the table itself, not an
    embedded business placement.  Exact-cover therefore checks source visual
    nodes and grounded placement relations, both of which can be reproduced by
    the row-record visual binder.
    """

    result = {
        node.source_node_id
        for node in source_ir.nodes
        if node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        and node.asset_ref is not None
        and any(
            _has_source_ancestor(
                node=node,
                ancestor_id=part.source_node.source_node_id,
                source_nodes_by_id=source_nodes_by_id,
            )
            for part in logical_table.parts
        )
    }
    table_structure_ids = {
        node.structure_node_id for node in logical_table.table_nodes
    }
    table_cell_ids = {
        cell.cell_id
        for part in logical_table.parts
        for cell in part.table.cells
    }
    for relation in structure.relations:
        if (
            relation.kind
            not in {
                StructureRelationKind.PLACED_IN_CELL,
                StructureRelationKind.PLACED_OVER_CELLS,
            }
            or relation.target_structure_node_id not in table_structure_ids
            or not any(
                ref.kind is StructureSourceRefKind.TABLE_CELL
                and ref.source_id in table_cell_ids
                for ref in relation.source_refs
            )
        ):
            continue
        result.update(
            ref.source_id
            for ref in relation.source_refs
            if ref.kind is StructureSourceRefKind.SOURCE_NODE
            and ref.source_id in source_nodes_by_id
            and source_nodes_by_id[ref.source_id].kind
            in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
        )
    return result


def _table_visual_asset_ids(
    *,
    source_ir: SourceIRSnapshot,
    structure: StructureSnapshot,
    logical_group: tuple[StructureNode, ...],
    part: _PhysicalTablePart,
    source_nodes_by_id: dict[str, SourceNode],
) -> tuple[str, ...]:
    asset_ids: set[str] = set()
    if part.table.visual_asset_ref is not None:
        asset_ids.add(part.table.visual_asset_ref)

    for node in source_ir.nodes:
        if (
            node.kind in {SourceNodeKind.IMAGE, SourceNodeKind.FIGURE}
            and node.asset_ref is not None
            and _has_source_ancestor(
                node=node,
                ancestor_id=part.source_node.source_node_id,
                source_nodes_by_id=source_nodes_by_id,
            )
        ):
            asset_ids.add(node.asset_ref)

    table_structure_ids = {node.structure_node_id for node in logical_group}
    table_cell_ids = {cell.cell_id for cell in part.table.cells}
    for relation in structure.relations:
        if (
            relation.kind
            not in {
                StructureRelationKind.PLACED_IN_CELL,
                StructureRelationKind.PLACED_OVER_CELLS,
            }
            or relation.target_structure_node_id not in table_structure_ids
            or not any(
                ref.kind is StructureSourceRefKind.TABLE_CELL
                and ref.source_id in table_cell_ids
                for ref in relation.source_refs
            )
        ):
            continue
        for ref in relation.source_refs:
            source_node = source_nodes_by_id.get(ref.source_id)
            if (
                ref.kind is StructureSourceRefKind.SOURCE_NODE
                and source_node is not None
                and source_node.asset_ref is not None
            ):
                asset_ids.add(source_node.asset_ref)
    return tuple(sorted(asset_ids))


def _has_source_ancestor(
    *,
    node: SourceNode,
    ancestor_id: str,
    source_nodes_by_id: dict[str, SourceNode],
) -> bool:
    cursor = node.parent_source_node_id
    visited: set[str] = set()
    while cursor is not None and cursor not in visited:
        if cursor == ancestor_id:
            return True
        visited.add(cursor)
        parent = source_nodes_by_id.get(cursor)
        cursor = parent.parent_source_node_id if parent is not None else None
    return False


def _header_signature(
    table: TableIR,
    header_rows: tuple[int, ...],
) -> tuple[tuple[str, ...], ...]:
    return tuple(_row_signature(table, row_index) for row_index in header_rows)


def _matching_header_prefix_rows(
    table: TableIR,
    signature: tuple[tuple[str, ...], ...],
) -> tuple[int, ...]:
    if len(signature) > table.row_count:
        return ()
    candidate = tuple(
        _row_signature(table, row_index) for row_index in range(len(signature))
    )
    return tuple(range(len(signature))) if candidate == signature else ()


def _row_signature(table: TableIR, row_index: int) -> tuple[str, ...]:
    return tuple(
        _normalized_key_text(
            _cell_text(_cell_covering(table, row_index, column_index))
        )
        for column_index in range(table.column_count)
    )


def _cells_covering_row(table: TableIR, row_index: int) -> tuple[TableCellIR, ...]:
    return tuple(
        sorted(
            (
                cell
                for cell in table.cells
                if cell.row <= row_index < cell.row + cell.row_span
            ),
            key=lambda cell: (cell.column, cell.row, cell.cell_id),
        )
    )


def _cell_covering(
    table: TableIR,
    row_index: int,
    column_index: int,
) -> TableCellIR | None:
    return next(
        (
            cell
            for cell in table.cells
            if cell.row <= row_index < cell.row + cell.row_span
            and cell.column <= column_index < cell.column + cell.column_span
        ),
        None,
    )


def _cell_text(cell: TableCellIR | None) -> str:
    if cell is None:
        return ""
    return (cell.display_value or cell.raw_text or cell.formula or "").strip()


def _normalized_key_text(value: str) -> str:
    normalized = " ".join(
        unicodedata.normalize("NFKC", value).split()
    ).casefold()
    if not normalized or _PLACEHOLDER_KEY_PATTERN.fullmatch(normalized):
        return ""
    return normalized


def _is_proven_ellipsis_placeholder_row(
    table: TableIR,
    row_index: int,
) -> bool:
    values = tuple(
        re.sub(
            r"\s+",
            "",
            unicodedata.normalize("NFKC", _cell_text(cell)),
        )
        for cell in _cells_covering_row(table, row_index)
        if _cell_text(cell)
    )
    return bool(
        values
        and all(
            _ELLIPSIS_PLACEHOLDER_PATTERN.fullmatch(value)
            for value in values
        )
    )


def _unique_visual_refs(
    refs: tuple[TableRecordVisualRefV1, ...],
) -> tuple[TableRecordVisualRefV1, ...]:
    return tuple(
        sorted(
            {
                (ref.asset_id, ref.table_id, ref.cell_id): ref for ref in refs
            }.values(),
            key=lambda ref: (ref.table_id, ref.cell_id or "", ref.asset_id),
        )
    )


def _source_node_ref(source_node_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=source_node_id,
    )


def _table_cell_ref(cell_id: str) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.TABLE_CELL,
        source_id=cell_id,
    )


def _structure_snapshot_id(structure: StructureSnapshot) -> str:
    if isinstance(structure, EffectiveStructureSnapshotV1):
        return structure.effective_snapshot_id
    return structure.structure_snapshot_id


def _projection_revision(structure: StructureSnapshot) -> int:
    if isinstance(structure, EffectiveStructureSnapshotV1):
        return structure.revision_number
    return 0


def _source_ir_canonical_sha256(source_ir: SourceIRSnapshot) -> str:
    """Match Structure revision lineage without binding observation time."""

    payload = source_ir.model_dump(mode="json", exclude={"created_at"})
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


__all__ = [
    "ManualMatrixIntersection",
    "ManualRowGroup",
    "TABLE_RECORD_PROJECTION_RUNTIME_VERSION",
    "build_adopted_table_projection_catalog",
    "build_manual_matrix_intersection_projection",
    "build_manual_row_group_projection",
    "build_table_record_projections",
    "manual_row_group_eligible_coordinates",
    "propose_manual_row_groups",
]
