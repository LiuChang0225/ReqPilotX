from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
from datetime import datetime, timezone

from app.application.knowledge_ingestion import source_ir_text_projection
from app.domain.extraction.source_ir import (
    MergedRegion,
    NativeDocumentFormat,
    NativeLocator,
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
from app.services.ai_copilot import build_knowledge_chunks


SOURCE_SHA256 = "a" * 64
ADAPTER_ID = "synthetic-knowledge-table"
ADAPTER_VERSION = "1.0.0"


class KnowledgeIngestionProjectionTests(unittest.TestCase):
    def test_pdf_table_is_projected_and_recognized_by_markdown_chunker(self) -> None:
        projection = source_ir_text_projection(
            _snapshot(NativeDocumentFormat.PDF)
        )

        self.assertIn(
            "format=PDF page=3 table_index=1]",
            projection,
        )
        self.assertIn(r"Shared \| condition<br>continued", projection)
        self.assertEqual(projection.count("Shared"), 1)

        chunks = build_knowledge_chunks(
            "project-1",
            {
                "id": "knowledge-1",
                "file_name": "source.pdf",
                "doc_version": "1",
                "extracted_text": projection,
            },
        )
        table_chunks = [
            chunk for chunk in chunks if chunk["content_kind"] == "table_slice"
        ]
        self.assertEqual(len(table_chunks), 1)
        self.assertFalse(
            [
                chunk
                for chunk in chunks
                if chunk["content_kind"] == "paragraph"
            ]
        )
        self.assertIn(
            "format=PDF page=3 table_index=1]",
            table_chunks[0]["content"],
        )
        self.assertIn("Threshold", table_chunks[0]["content"])
        self.assertEqual(table_chunks[0]["meta"]["native_format"], "PDF")
        self.assertEqual(table_chunks[0]["meta"]["native_page"], "3")
        self.assertEqual(table_chunks[0]["meta"]["native_table_index"], "1")
        self.assertTrue(table_chunks[0]["meta"]["table_id"])

    def test_docx_locator_does_not_invent_page_and_keeps_empty_rows(self) -> None:
        projection = source_ir_text_projection(
            _snapshot(NativeDocumentFormat.DOCX)
        )
        lines = projection.splitlines()

        self.assertTrue(lines[0].startswith("| [source_locator table_id="))
        self.assertIn(
            "format=DOCX body_element_index=7 table_index=2] |  |  |",
            lines[0],
        )
        self.assertNotIn("page=", lines[0])
        self.assertEqual(lines[-1], "|  |  |  |")
        self.assertEqual(len(lines), 5)

    def test_xlsx_projection_is_coordinate_stable_for_unordered_cells(self) -> None:
        normal = source_ir_text_projection(
            _snapshot(NativeDocumentFormat.XLSX)
        )
        reversed_cells = source_ir_text_projection(
            _snapshot(NativeDocumentFormat.XLSX, reverse_cells=True)
        )

        self.assertEqual(normal, reversed_cells)
        self.assertIn(
            r'format=XLSX workbook="Specs.xlsx" '
            r'sheet="Limits \| Main" range=B2:D4]',
            normal,
        )
        self.assertNotIn("page=", normal.splitlines()[0])
        self.assertEqual(normal.count("Shared"), 1)


def _snapshot(
    native_format: NativeDocumentFormat,
    *,
    reverse_cells: bool = False,
) -> SourceIRSnapshot:
    table_locator = _table_locator(native_format)
    table_id = stable_table_id(
        source_sha256=SOURCE_SHA256,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=table_locator,
    )
    cells = [
        _cell(
            table_id,
            table_locator,
            row=0,
            column=0,
            raw_text="Shared | condition\ncontinued",
            column_span=2,
        ),
        _cell(table_id, table_locator, row=0, column=2),
        _cell(
            table_id,
            table_locator,
            row=1,
            column=0,
            raw_text="Threshold",
        ),
        _cell(
            table_id,
            table_locator,
            row=1,
            column=1,
            display_value="100 ms",
        ),
        _cell(table_id, table_locator, row=1, column=2),
    ]
    if reverse_cells:
        cells.reverse()
    merged = MergedRegion(row=0, column=0, column_span=2)
    table = TableIR(
        table_id=table_id,
        row_count=3,
        column_count=3,
        header_regions=(merged,),
        cells=tuple(cells),
        merged_regions=(merged,),
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
    document_id = stable_document_id(SOURCE_SHA256)
    return SourceIRSnapshot(
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
        status=SourceIRStatus.COMPLETE,
        nodes=(table_node,),
        created_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
    )


def _table_locator(native_format: NativeDocumentFormat) -> NativeLocator:
    if native_format is NativeDocumentFormat.PDF:
        return NativeLocator(
            format=native_format,
            page_number=3,
            table_index=1,
        )
    if native_format is NativeDocumentFormat.DOCX:
        return NativeLocator(
            format=native_format,
            body_element_index=7,
            table_index=2,
        )
    if native_format is NativeDocumentFormat.XLSX:
        return NativeLocator(
            format=native_format,
            workbook="Specs.xlsx",
            sheet="Limits | Main",
            cell_range="B2:D4",
        )
    raise AssertionError(f"unsupported synthetic format: {native_format}")


def _cell(
    table_id: str,
    table_locator: NativeLocator,
    *,
    row: int,
    column: int,
    raw_text: str = "",
    display_value: str | None = None,
    column_span: int = 1,
) -> TableCellIR:
    locator = _cell_locator(table_locator, row=row, column=column)
    return TableCellIR(
        cell_id=stable_cell_id(table_id, row, column),
        row=row,
        column=column,
        column_span=column_span,
        raw_text=raw_text,
        display_value=display_value,
        native_locator=locator,
    )


def _cell_locator(
    table_locator: NativeLocator,
    *,
    row: int,
    column: int,
) -> NativeLocator:
    if table_locator.format is NativeDocumentFormat.PDF:
        return NativeLocator(
            format=table_locator.format,
            page_number=table_locator.page_number,
            table_index=table_locator.table_index,
            row_index=row,
            column_index=column,
        )
    if table_locator.format is NativeDocumentFormat.DOCX:
        return NativeLocator(
            format=table_locator.format,
            body_element_index=table_locator.body_element_index,
            table_index=table_locator.table_index,
            row_index=row,
            column_index=column,
        )
    columns = "BCD"
    return NativeLocator(
        format=table_locator.format,
        workbook=table_locator.workbook,
        sheet=table_locator.sheet,
        cell_range=f"{columns[column]}{row + 2}",
    )


if __name__ == "__main__":
    unittest.main()
