from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest

from pydantic import ValidationError

from app.application.extraction.table_evidence_projection import (
    build_table_evidence_bindings,
)
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from app.domain.extraction.table_records import (
    TableFactPartV1,
    TableRecordColumnBindingV1,
    TableRecordHeaderAxis,
    TableRecordHeaderBindingV1,
    TableRecordKind,
    TableRecordNoteV1,
    TableRecordProjectionDecisionOrigin,
    TableRecordProjectionDecisionV1,
    TableRecordProjectionMode,
    TableRecordRowBindingV1,
    TableRecordSharedMergedValueV1,
    TableRecordVisualRefV1,
    build_table_fact_binding,
    build_table_record,
    build_table_record_projection,
    verify_table_fact_binding_sha256,
    verify_table_record_content_sha256,
    verify_table_record_projection_sha256,
)


SOURCE_SHA256 = "a" * 64
SOURCE_IR_CANONICAL_SHA256 = "b" * 64
OWNER_ID = "structure-node-" + "c" * 32
STRUCTURE_SNAPSHOT_ID = "effective-structure-" + "d" * 32


def _node_ref(index: int) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.SOURCE_NODE,
        source_id=f"node-{index:032x}",
    )


def _cell_ref(index: int) -> StructureSourceRef:
    return StructureSourceRef(
        kind=StructureSourceRefKind.TABLE_CELL,
        source_id=f"cell-{index:032x}",
    )


def _cell_id(index: int) -> str:
    return f"cell-{index:032x}"


def _part(
    *,
    table_id: str,
    source_order: int,
    cell_offset: int,
) -> TableFactPartV1:
    return TableFactPartV1(
        table_id=table_id,
        source_order=source_order,
        row_count=4,
        column_count=2,
        cell_ids=tuple(_cell_id(cell_offset + index) for index in range(8)),
        table_content_sha256=f"{source_order + 1:064x}",
        source_refs=(_node_ref(source_order + 1),),
        visual_asset_refs=(f"asset-{table_id}",),
    )


def _fact(*, reverse_parts: bool = False):
    parts = (
        _part(table_id="table-a", source_order=0, cell_offset=1),
        _part(table_id="table-b", source_order=1, cell_offset=101),
    )
    if reverse_parts:
        parts = tuple(reversed(parts))
    return build_table_fact_binding(
        source_ir_id="source-ir-fixture",
        source_sha256=SOURCE_SHA256,
        source_ir_canonical_sha256=SOURCE_IR_CANONICAL_SHA256,
        structure_snapshot_id=STRUCTURE_SNAPSHOT_ID,
        logical_table_id="logical-table-fixture",
        owner_structure_node_id=OWNER_ID,
        table_parts=parts,
        whole_table_source_refs=(_node_ref(2), _node_ref(1)),
    )


def _column_header(
    *,
    text: str = "技术要求",
    table_id: str = "table-a",
    cell_id: str | None = None,
) -> TableRecordHeaderBindingV1:
    resolved_cell_id = cell_id or _cell_id(2)
    return TableRecordHeaderBindingV1(
        table_id=table_id,
        axis=TableRecordHeaderAxis.COLUMN,
        level=0,
        cell_ids=(resolved_cell_id,),
        text=text,
        source_refs=(_cell_ref(int(resolved_cell_id.removeprefix("cell-"), 16)),),
    )


def _row_record(
    *,
    fact=None,
    row_index: int = 1,
    source_order: int = 10,
):
    fact = fact or _fact()
    first_cell = _cell_id(1 + row_index * 2)
    second_cell = _cell_id(2 + row_index * 2)
    return build_table_record(
        table_fact=fact,
        kind=TableRecordKind.ROW,
        source_order=source_order,
        row_bindings=(
            TableRecordRowBindingV1(
                table_id="table-a",
                row_index=row_index,
                cell_ids=(first_cell, second_cell),
            ),
        ),
        column_bindings=(
            TableRecordColumnBindingV1(
                table_id="table-a",
                column_index=1,
                cell_ids=(second_cell,),
            ),
            TableRecordColumnBindingV1(
                table_id="table-a",
                column_index=0,
                cell_ids=(first_cell,),
            ),
        ),
        header_bindings=(_column_header(),),
        shared_merged_values=(
            TableRecordSharedMergedValueV1(
                table_id="table-a",
                cell_id=_cell_id(1),
                row=0,
                column=0,
                column_span=2,
                text="前悬架",
                source_refs=(_cell_ref(1),),
            ),
        ),
        notes=(
            TableRecordNoteV1(
                table_id="table-a",
                source_order=0,
                text="试验条件见附录。",
                source_refs=(_node_ref(1),),
            ),
        ),
        image_refs=(
            TableRecordVisualRefV1(
                asset_id="asset-table-a",
                table_id="table-a",
                cell_id=second_cell,
                source_refs=(_cell_ref(int(second_cell.removeprefix("cell-"), 16)),),
            ),
        ),
        source_refs=(
            _cell_ref(int(second_cell.removeprefix("cell-"), 16)),
            _cell_ref(int(first_cell.removeprefix("cell-"), 16)),
        ),
    )


class TableRecordProjectionContractTests(unittest.TestCase):
    def test_record_evidence_deduplicates_only_the_same_visual_placement(
        self,
    ) -> None:
        fact = _fact()
        base_record = _row_record(fact=fact)
        first_cell, second_cell = base_record.row_bindings[0].cell_ids
        repeated_visual = TableRecordVisualRefV1(
            asset_id="asset-table-a",
            table_id="table-a",
            cell_id=first_cell,
            source_refs=(
                _cell_ref(int(first_cell.removeprefix("cell-"), 16)),
            ),
        )
        second_placement = TableRecordVisualRefV1(
            asset_id="asset-table-a",
            table_id="table-a",
            cell_id=second_cell,
            source_refs=(
                _cell_ref(int(second_cell.removeprefix("cell-"), 16)),
            ),
        )
        record = build_table_record(
            table_fact=fact,
            kind=base_record.kind,
            source_order=base_record.source_order,
            row_bindings=base_record.row_bindings,
            column_bindings=base_record.column_bindings,
            header_bindings=base_record.header_bindings,
            shared_merged_values=base_record.shared_merged_values,
            notes=base_record.notes,
            figure_refs=(repeated_visual,),
            image_refs=(repeated_visual, second_placement),
            source_refs=base_record.source_refs,
        )
        projection = build_table_record_projection(
            table_fact=fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(record,),
        )

        bindings = build_table_evidence_bindings(
            table_record_projections=(projection,),
            tables=(
                {"id": "table-a", "cells": []},
                {"id": "table-b", "cells": []},
            ),
        )
        record_binding = next(item for item in bindings if item.record_id)

        self.assertEqual(len(record_binding.image_refs), 2)
        self.assertEqual(
            len({item.reference_id for item in record_binding.image_refs}),
            2,
        )
        self.assertEqual(
            len({item.content_sha256 for item in record_binding.image_refs}),
            2,
        )

    def test_fact_record_and_projection_hashes_are_stable_after_normalization(
        self,
    ) -> None:
        first_fact = _fact()
        second_fact = _fact(reverse_parts=True)
        self.assertEqual(first_fact.table_ids, ("table-a", "table-b"))
        self.assertEqual(first_fact, second_fact)
        self.assertTrue(verify_table_fact_binding_sha256(first_fact))

        first_record = _row_record(fact=first_fact)
        second_record = build_table_record(
            table_fact=second_fact,
            kind=TableRecordKind.ROW,
            source_order=10,
            row_bindings=first_record.row_bindings,
            column_bindings=tuple(reversed(first_record.column_bindings)),
            header_bindings=first_record.header_bindings,
            shared_merged_values=first_record.shared_merged_values,
            notes=first_record.notes,
            image_refs=first_record.image_refs,
            source_refs=tuple(reversed(first_record.source_refs)),
        )
        self.assertEqual(first_record, second_record)
        self.assertTrue(verify_table_record_content_sha256(first_record))

        later_record = _row_record(
            fact=first_fact,
            row_index=2,
            source_order=20,
        )
        first_projection = build_table_record_projection(
            table_fact=first_fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(later_record, first_record),
        )
        second_projection = build_table_record_projection(
            table_fact=second_fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(first_record, later_record),
        )
        self.assertEqual(
            tuple(record.source_order for record in first_projection.records),
            (10, 20),
        )
        self.assertEqual(first_projection, second_projection)
        self.assertTrue(
            verify_table_record_projection_sha256(first_projection)
        )

        forged = first_projection.model_copy(
            update={"projection_sha256": "f" * 64}
        )
        self.assertFalse(verify_table_record_projection_sha256(forged))

    def test_optional_decision_explanation_is_backward_compatible_and_unhashed(
        self,
    ) -> None:
        fact = _fact()
        record = _row_record(fact=fact)
        legacy = build_table_record_projection(
            table_fact=fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(record,),
        )
        explained = build_table_record_projection(
            table_fact=fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(record,),
            decision=TableRecordProjectionDecisionV1(
                origin=TableRecordProjectionDecisionOrigin.AUTO,
                mode=TableRecordProjectionMode.RECORDS,
                reason_code="AUTO_STABLE_KEY_SCHEMA",
                message="检测到明确表头和稳定记录键，已按记录行拆分。",
                evidence=("明确表头与稳定记录键：1 行",),
            ),
        )

        self.assertEqual(legacy.projection_sha256, explained.projection_sha256)
        self.assertEqual(legacy.projection_id, explained.projection_id)
        self.assertTrue(verify_table_record_projection_sha256(explained))

        old_payload = explained.model_dump(mode="json")
        old_payload.pop("decision")
        restored = explained.__class__.model_validate(old_payload)
        self.assertIsNone(restored.decision)
        self.assertEqual(restored.projection_sha256, explained.projection_sha256)

        with self.assertRaisesRegex(
            ValidationError,
            "decision mode must match",
        ):
            build_table_record_projection(
                table_fact=fact,
                mode=TableRecordProjectionMode.RECORDS,
                projection_revision=1,
                records=(record,),
                decision=TableRecordProjectionDecisionV1(
                    origin=TableRecordProjectionDecisionOrigin.MANUAL,
                    mode=TableRecordProjectionMode.WHOLE_TABLE,
                    reason_code="MANUAL_KEEP_WHOLE_TABLE",
                    message="人工保留整表。",
                ),
            )

    def test_whole_table_mode_preserves_fact_without_inventing_records(
        self,
    ) -> None:
        projection = build_table_record_projection(
            table_fact=_fact(),
            mode=TableRecordProjectionMode.WHOLE_TABLE,
            projection_revision=0,
        )
        self.assertEqual(projection.records, ())
        self.assertTrue(verify_table_record_projection_sha256(projection))

        with self.assertRaisesRegex(
            ValidationError,
            "WHOLE_TABLE mode must not contain derived records",
        ):
            build_table_record_projection(
                table_fact=_fact(),
                mode=TableRecordProjectionMode.WHOLE_TABLE,
                projection_revision=1,
                records=(_row_record(),),
            )

    def test_row_record_is_structural_and_retains_complete_context(self) -> None:
        record = _row_record()
        self.assertFalse(record.is_requirement)
        self.assertEqual(record.kind, TableRecordKind.ROW)
        self.assertEqual(record.table_ids, ("table-a",))
        self.assertEqual(record.shared_merged_values[0].text, "前悬架")
        self.assertEqual(record.notes[0].text, "试验条件见附录。")
        self.assertEqual(record.image_refs[0].asset_id, "asset-table-a")

        payload = record.model_dump(mode="json")
        payload["is_requirement"] = True
        with self.assertRaises(ValidationError):
            record.__class__.model_validate(payload)

    def test_matrix_intersection_requires_both_axis_headers(self) -> None:
        fact = _fact()
        matrix_record = build_table_record(
            table_fact=fact,
            kind=TableRecordKind.MATRIX_INTERSECTION,
            source_order=30,
            row_bindings=(
                TableRecordRowBindingV1(
                    table_id="table-a",
                    row_index=1,
                    cell_ids=(_cell_id(3), _cell_id(4)),
                ),
            ),
            column_bindings=(
                TableRecordColumnBindingV1(
                    table_id="table-a",
                    column_index=1,
                    cell_ids=(_cell_id(2), _cell_id(4)),
                ),
            ),
            header_bindings=(
                TableRecordHeaderBindingV1(
                    table_id="table-a",
                    axis=TableRecordHeaderAxis.ROW,
                    level=0,
                    cell_ids=(_cell_id(3),),
                    text="前轴",
                    source_refs=(_cell_ref(3),),
                ),
                _column_header(text="阻尼力"),
            ),
            source_refs=(_cell_ref(4),),
        )
        projection = build_table_record_projection(
            table_fact=fact,
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=2,
            records=(matrix_record,),
        )
        self.assertEqual(
            projection.records[0].kind,
            TableRecordKind.MATRIX_INTERSECTION,
        )

        with self.assertRaisesRegex(
            ValidationError,
            "requires row and column headers",
        ):
            build_table_record(
                table_fact=fact,
                kind=TableRecordKind.MATRIX_INTERSECTION,
                source_order=30,
                row_bindings=matrix_record.row_bindings,
                column_bindings=matrix_record.column_bindings,
                header_bindings=(_column_header(text="阻尼力"),),
                source_refs=(_cell_ref(4),),
            )

    def test_invalid_duplicates_bounds_and_empty_headers_are_rejected(
        self,
    ) -> None:
        duplicate_part = _part(
            table_id="table-a",
            source_order=1,
            cell_offset=101,
        )
        with self.assertRaisesRegex(
            ValidationError,
            "unique table IDs",
        ):
            build_table_fact_binding(
                source_ir_id="source-ir-fixture",
                source_sha256=SOURCE_SHA256,
                source_ir_canonical_sha256=SOURCE_IR_CANONICAL_SHA256,
                structure_snapshot_id=STRUCTURE_SNAPSHOT_ID,
                logical_table_id="logical-table-fixture",
                owner_structure_node_id=OWNER_ID,
                table_parts=(
                    _part(
                        table_id="table-a",
                        source_order=0,
                        cell_offset=1,
                    ),
                    duplicate_part,
                ),
                whole_table_source_refs=(_node_ref(1),),
            )

        with self.assertRaises(ValidationError):
            _column_header(text="   ")

        out_of_bounds = _row_record(row_index=4)
        with self.assertRaisesRegex(
            ValidationError,
            "row binding exceeds table bounds",
        ):
            build_table_record_projection(
                table_fact=_fact(),
                mode=TableRecordProjectionMode.RECORDS,
                projection_revision=1,
                records=(out_of_bounds,),
            )

        first = _row_record(row_index=1, source_order=10)
        second = _row_record(row_index=2, source_order=10)
        with self.assertRaisesRegex(
            ValidationError,
            "unique source order",
        ):
            build_table_record_projection(
                table_fact=_fact(),
                mode=TableRecordProjectionMode.RECORDS,
                projection_revision=1,
                records=(first, second),
            )

    def test_transport_batching_cannot_enter_the_domain_contract(self) -> None:
        projection = build_table_record_projection(
            table_fact=_fact(),
            mode=TableRecordProjectionMode.RECORDS,
            projection_revision=1,
            records=(_row_record(),),
        )
        payload = projection.model_dump(mode="json")
        payload["batch_index"] = 0
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            projection.__class__.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
