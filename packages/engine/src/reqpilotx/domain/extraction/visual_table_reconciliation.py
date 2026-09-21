from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from reqpilotx.domain.extraction.source_ir import (
    SourceIRSnapshot,
    SourceNode,
    SourceNodeKind,
)
from reqpilotx.domain.extraction.visual_table import VisualTableSnapshot


class VisualTableReconciliationCode(str, Enum):
    BINDING_MISMATCH = "binding_mismatch"
    TOPOLOGY_MISMATCH = "topology_mismatch"


class VisualTableReconciliationRejected(RuntimeError):
    def __init__(self, code: VisualTableReconciliationCode, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class VerifiedVisualTableCellFact:
    """Visual cell text accepted only after whole-table native topology proof."""

    table_source_node_id: str
    table_id: str
    native_cell_id: str
    visual_table_snapshot_id: str
    visual_table_cell_id: str
    provider_id: str
    provider_version: str
    crop_image_sha256: str
    row: int
    column: int
    row_span: int
    column_span: int
    raw_text: str


def visual_table_topology_matches_native(
    snapshot: VisualTableSnapshot,
    table_node: SourceNode,
) -> bool:
    """Return true only for one fully identical native/visual cell topology."""

    table = table_node.table_payload
    if table is None or table_node.kind is not SourceNodeKind.TABLE:
        return False
    native_topology = {
        (cell.row, cell.column, cell.row_span, cell.column_span)
        for cell in table.cells
    }
    visual_topology = {
        (cell.row, cell.column, cell.row_span, cell.column_span)
        for cell in snapshot.cells
    }
    return (
        snapshot.table_source_node_id == table_node.source_node_id
        and snapshot.table_id == table.table_id
        and snapshot.page_number == table_node.native_locator.page_number
        and snapshot.table_index == table_node.native_locator.table_index
        and snapshot.table_bbox == table_node.native_locator.bbox
        and snapshot.row_count == table.row_count
        and snapshot.column_count == table.column_count
        and visual_topology == native_topology
        and len(snapshot.cells) == len(table.cells)
    )


def reconcile_visual_table_snapshot(
    snapshot: VisualTableSnapshot,
    table_node: SourceNode,
) -> tuple[VerifiedVisualTableCellFact, ...]:
    """Return an all-or-nothing visual-text mapping onto native cell anchors."""

    table = table_node.table_payload
    if (
        table is None
        or table_node.kind is not SourceNodeKind.TABLE
        or snapshot.table_source_node_id != table_node.source_node_id
        or snapshot.table_id != table.table_id
    ):
        raise VisualTableReconciliationRejected(
            VisualTableReconciliationCode.BINDING_MISMATCH,
            "Visual table facts are not bound to the selected native table.",
        )
    if not visual_table_topology_matches_native(snapshot, table_node):
        raise VisualTableReconciliationRejected(
            VisualTableReconciliationCode.TOPOLOGY_MISMATCH,
            "Visual table topology does not exactly match the native table.",
        )
    native_by_anchor = {(cell.row, cell.column): cell for cell in table.cells}
    return tuple(
        VerifiedVisualTableCellFact(
            table_source_node_id=table_node.source_node_id,
            table_id=table.table_id,
            native_cell_id=native_by_anchor[(cell.row, cell.column)].cell_id,
            visual_table_snapshot_id=snapshot.visual_table_snapshot_id,
            visual_table_cell_id=cell.visual_table_cell_id,
            provider_id=snapshot.provider_id,
            provider_version=snapshot.provider_version,
            crop_image_sha256=snapshot.crop_image_sha256,
            row=cell.row,
            column=cell.column,
            row_span=cell.row_span,
            column_span=cell.column_span,
            raw_text=cell.raw_text,
        )
        for cell in snapshot.cells
    )


def verified_visual_table_cell_facts(
    source_ir: SourceIRSnapshot,
) -> tuple[VerifiedVisualTableCellFact, ...]:
    """Strict SourceIR accessor used by Structure and controlled A4 consumers.

    One mismatched sidecar rejects the whole accessor result; consumers cannot
    accidentally mix a partially trusted table with verified tables.
    """

    nodes = {node.source_node_id: node for node in source_ir.nodes}
    facts: list[VerifiedVisualTableCellFact] = []
    for snapshot in source_ir.visual_table_snapshots:
        table_node = nodes.get(snapshot.table_source_node_id)
        if table_node is None:
            raise VisualTableReconciliationRejected(
                VisualTableReconciliationCode.BINDING_MISMATCH,
                "Visual table facts reference a missing native table.",
            )
        facts.extend(reconcile_visual_table_snapshot(snapshot, table_node))
    return tuple(
        sorted(
            facts,
            key=lambda item: (
                item.table_source_node_id,
                item.row,
                item.column,
                item.visual_table_cell_id,
            ),
        )
    )


__all__ = [
    "VerifiedVisualTableCellFact",
    "VisualTableReconciliationCode",
    "VisualTableReconciliationRejected",
    "reconcile_visual_table_snapshot",
    "verified_visual_table_cell_facts",
    "visual_table_topology_matches_native",
]
