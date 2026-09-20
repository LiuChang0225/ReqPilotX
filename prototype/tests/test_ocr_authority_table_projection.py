from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app.application.extraction.processing_pipeline import (
    build_local_structure_document_result,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.application.extraction.revise_structure import get_base_effective_structure
from app.application.extraction.table_record_projection import (
    build_adopted_table_projection_catalog,
    build_table_record_projections,
)
from app.application.extraction.table_row_group_review_index import (
    TABLE_ROW_GROUP_REVIEW_INDEX_KEY,
    build_table_row_group_review_index,
)
from app.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceIRSnapshot,
    SourceIRStatus,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_document_id,
    stable_node_id,
    stable_source_ir_id,
    stable_table_id,
)
from app.domain.extraction.table_records import TableRecordProjectionMode
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)


SOURCE_SHA256 = "6" * 64
ADAPTER_ID = "ocr-authority-table-projection-test"
ADAPTER_VERSION = "1.0.0"
NATIVE_SENTINELS = (
    "NATIVE-H1",
    "NATIVE-H2",
    "NATIVE-H3",
    "NATIVE-K1",
    "NATIVE-A1",
    "NATIVE-BLANK-SENTINEL",
    "NATIVE-K2",
    "NATIVE-A2",
    "NATIVE-B2",
    "NATIVE-NOTE-SENTINEL",
)
VISUAL_TEXTS = (
    "视觉序号",
    "视觉参数",
    "视觉要求",
    "V-01",
    "视觉温度",
    "",
    "V-02",
    "视觉压力",
    "视觉值二",
)


class _NoLlmPlan:
    def model_dump(self, *, mode: str) -> dict[str, object]:
        assert mode == "json"
        return {
            "document": {"primary_parser": "pymupdf"},
            "ai": {"llm_provider": "none"},
        }


class OcrAuthorityTableProjectionTests(unittest.TestCase):
    def test_local_no_llm_result_clears_unbound_native_table_text(self) -> None:
        source = _table_source(authority=True)
        structure = build_structure_tree(source)

        projected_table = next(
            iter(build_adopted_table_projection_catalog(source, structure).values())
        )
        self.assertTrue(
            all(
                cell.raw_text == ""
                and cell.display_value is None
                and cell.formula is None
                for cell in projected_table.cells
            )
        )
        self.assertEqual(projected_table.notes, ())

        result = _local_result(source)

        downstream = {
            "table_record_projections": result["table_record_projections"],
            TABLE_ROW_GROUP_REVIEW_INDEX_KEY: result[
                TABLE_ROW_GROUP_REVIEW_INDEX_KEY
            ],
        }
        serialized = json.dumps(downstream, ensure_ascii=False, sort_keys=True)
        self.assertTrue(all(value not in serialized for value in NATIVE_SENTINELS))
        projection = result["table_record_projections"][0]
        self.assertEqual(projection["mode"], "WHOLE_TABLE")
        entry = result[TABLE_ROW_GROUP_REVIEW_INDEX_KEY]["entries"][0]
        self.assertEqual(entry["eligible_rows"], [])
        self.assertFalse(entry["editable"])

    def test_exact_visual_binding_drives_records_and_review_index(self) -> None:
        source = _table_source(authority=True, visual_texts=VISUAL_TEXTS)
        structure = build_structure_tree(source)
        effective = get_base_effective_structure(source, structure)
        projected_table = next(
            iter(
                build_adopted_table_projection_catalog(
                    source,
                    effective,
                ).values()
            )
        )

        projections = build_table_record_projections(source, effective)
        index = build_table_row_group_review_index(
            source,
            effective,
            automatic_projections=projections,
        )

        self.assertEqual(len(effective.visual_table_cell_text_bindings), 9)
        self.assertEqual(
            tuple(cell.raw_text for cell in projected_table.cells),
            VISUAL_TEXTS,
        )
        self.assertTrue(
            all(
                cell.display_value is None and cell.formula is None
                for cell in projected_table.cells
            )
        )
        self.assertEqual(projected_table.notes, ())
        self.assertEqual(projections[0].mode, TableRecordProjectionMode.RECORDS)
        record_payload = json.dumps(
            [item.model_dump(mode="json") for item in projections],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn("视觉序号", record_payload)
        self.assertIn("视觉要求", record_payload)
        self.assertTrue(
            all(value not in record_payload for value in NATIVE_SENTINELS)
        )
        display_texts = [
            row["display_text"]
            for row in index["entries"][0]["eligible_rows"]
        ]
        self.assertEqual(
            display_texts,
            ["V-01 · 视觉温度", "V-02 · 视觉压力 · 视觉值二"],
        )
        serialized_index = json.dumps(index, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("NATIVE-BLANK-SENTINEL", serialized_index)

        local_result = _local_result(source)
        local_downstream = json.dumps(
            {
                "table_record_projections": local_result[
                    "table_record_projections"
                ],
                TABLE_ROW_GROUP_REVIEW_INDEX_KEY: local_result[
                    TABLE_ROW_GROUP_REVIEW_INDEX_KEY
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn("视觉序号", local_downstream)
        self.assertIn("V-01 · 视觉温度", local_downstream)
        self.assertTrue(
            all(value not in local_downstream for value in NATIVE_SENTINELS)
        )

    def test_native_table_projection_remains_unchanged(self) -> None:
        source = _table_source(authority=False)
        structure = build_structure_tree(source)
        effective = get_base_effective_structure(source, structure)
        native_table = source.nodes[0].table_payload
        assert native_table is not None

        projected_table = next(
            iter(
                build_adopted_table_projection_catalog(
                    source,
                    effective,
                ).values()
            )
        )

        projections = build_table_record_projections(source, effective)
        index = build_table_row_group_review_index(
            source,
            effective,
            automatic_projections=projections,
        )

        self.assertEqual(projections[0].mode, TableRecordProjectionMode.RECORDS)
        self.assertEqual(projected_table, native_table)
        record_payload = json.dumps(
            [item.model_dump(mode="json") for item in projections],
            ensure_ascii=False,
            sort_keys=True,
        )
        self.assertIn("NATIVE-H1", record_payload)
        self.assertIn("NATIVE-H3", record_payload)
        self.assertEqual(
            [
                row["display_text"]
                for row in index["entries"][0]["eligible_rows"]
            ],
            [
                "NATIVE-K1-DISPLAY · NATIVE-A1-DISPLAY · "
                "NATIVE-BLANK-SENTINEL-DISPLAY",
                "NATIVE-K2-DISPLAY · NATIVE-A2-DISPLAY · NATIVE-B2-DISPLAY",
            ],
        )


def _local_result(source: SourceIRSnapshot) -> dict[str, object]:
    with patch(
        "app.application.extraction.processing_pipeline."
        "build_source_ir_and_receipts_for_plan",
        return_value=(source, ()),
    ):
        return build_local_structure_document_result(
            Path("unused-source.pdf"),
            plan=_NoLlmPlan(),  # type: ignore[arg-type]
            result_runtime_sha256="7" * 64,
        )


def _table_source(
    *,
    authority: bool,
    visual_texts: tuple[str, ...] | None = None,
) -> SourceIRSnapshot:
    document_id = stable_document_id(SOURCE_SHA256)
    page_bbox = (0.0, 0.0, 600.0, 800.0)
    table_bbox = (40.0, 100.0, 560.0, 310.0)
    table_locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=table_bbox,
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    native_rows = (
        NATIVE_SENTINELS[0:3],
        NATIVE_SENTINELS[3:6],
        NATIVE_SENTINELS[6:9],
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row_index, column_index),
            row=row_index,
            column=column_index,
            raw_text=value,
            formula=f"FORMULA-{row_index}-{column_index}",
            display_value=f"{value}-DISPLAY",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row_index,
                column_index=column_index,
                bbox=(
                    40.0 + column_index * 170.0,
                    100.0 + row_index * 60.0,
                    200.0 + column_index * 170.0,
                    150.0 + row_index * 60.0,
                ),
            ),
        )
        for row_index, row in enumerate(native_rows)
        for column_index, value in enumerate(row)
    )
    table = TableIR(
        table_id=table_id,
        row_count=3,
        column_count=3,
        header_regions=tuple(
            MergedRegion(row=0, column=column_index)
            for column_index in range(3)
        ),
        cells=cells,
        notes=(NATIVE_SENTINELS[9],),
    )
    table_node = SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_SHA256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=table_locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        native_locator=table_locator,
        table_payload=table,
    )
    diagnostics = (
        SourceDiagnostic(
            code="pdf_visual_text_user_forced",
            severity=SourceDiagnosticSeverity.INFO,
            message="The page was explicitly routed to visual text.",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                bbox=page_bbox,
            ),
        ),
    ) if authority else ()
    source = SourceIRSnapshot(
        source_ir_id=stable_source_ir_id(
            document_id=document_id,
            source_sha256=SOURCE_SHA256,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
        ),
        document_id=document_id,
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        status=SourceIRStatus.PARTIAL if authority else SourceIRStatus.COMPLETE,
        nodes=(table_node,),
        diagnostics=diagnostics,
        created_at=datetime.now(timezone.utc),
    )
    if visual_texts is None:
        return source

    visual_table = _visual_table_snapshot(
        source=source,
        table_node=table_node,
        page_bbox=page_bbox,
        table_bbox=table_bbox,
        texts=visual_texts,
    )
    return source.model_copy(
        update={
            "visual_table_page_numbers": (1,),
            "visual_table_snapshots": (visual_table,),
        }
    )


def _visual_table_snapshot(
    *,
    source: SourceIRSnapshot,
    table_node: SourceNode,
    page_bbox: tuple[float, float, float, float],
    table_bbox: tuple[float, float, float, float],
    texts: tuple[str, ...],
) -> VisualTableSnapshot:
    table = table_node.table_payload
    assert table is not None
    provider_id = "fixture-visual-table"
    provider_version = "1.0.0:model=fixture:parser=strict-grid/1"
    crop_sha256 = "8" * 64
    snapshot_id = stable_visual_table_snapshot_id(
        source_sha256=source.source_sha256,
        crop_image_sha256=crop_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=1,
        table_source_node_id=table_node.source_node_id,
        table_id=table.table_id,
        table_index=0,
        table_bbox=table_bbox,
        crop_image_width_px=1040,
        crop_image_height_px=420,
    )
    ordered_cells = tuple(
        sorted(table.cells, key=lambda cell: (cell.row, cell.column))
    )
    visual_cells = tuple(
        VisualTableCell(
            visual_table_cell_id=stable_visual_table_cell_id(
                visual_table_snapshot_id=snapshot_id,
                row=cell.row,
                column=cell.column,
                row_span=cell.row_span,
                column_span=cell.column_span,
            ),
            row=cell.row,
            column=cell.column,
            row_span=cell.row_span,
            column_span=cell.column_span,
            raw_text=text,
        )
        for cell, text in zip(ordered_cells, texts, strict=True)
    )
    return VisualTableSnapshot(
        visual_table_snapshot_id=snapshot_id,
        source_sha256=source.source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=1,
        page_bbox=page_bbox,
        table_source_node_id=table_node.source_node_id,
        table_id=table.table_id,
        table_index=0,
        table_bbox=table_bbox,
        crop_image_sha256=crop_sha256,
        crop_image_width_px=1040,
        crop_image_height_px=420,
        row_count=table.row_count,
        column_count=table.column_count,
        cells=visual_cells,
    )


if __name__ == "__main__":
    unittest.main()
