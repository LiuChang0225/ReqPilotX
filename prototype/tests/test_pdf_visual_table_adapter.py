from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import unittest

from app.adapters.documents.pdf_source_ir_adapter import (
    PdfSourceIRAdapter,
    _PdfResourceBudget,
    _visual_table_snapshot_from_native_table,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceNode,
    SourceNodeKind,
    TableCellIR,
    TableIR,
    stable_cell_id,
    stable_node_id,
    stable_table_id,
)
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.ports.visual_table_provider import (
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)


SOURCE_BYTES = b"approved PDF bytes"
SOURCE_HASH = hashlib.sha256(SOURCE_BYTES).hexdigest()
ADAPTER_ID = "test-pdf-adapter"
ADAPTER_VERSION = "1.0.0"


class PdfVisualTableAdapterTests(unittest.TestCase):
    def test_native_bbox_is_rendered_annotation_free_at_2x_and_bound_to_snapshot(
        self,
    ) -> None:
        table_node = _table_node()
        page = _Page()
        provider = _Provider()

        snapshot, diagnostics = _visual_table_snapshot_from_native_table(
            page=page,
            page_number=1,
            page_locator=_page_locator(),
            table_node=table_node,
            source_bytes=SOURCE_BYTES,
            source_sha256=SOURCE_HASH,
            provider=provider,
            resource_budget=_PdfResourceBudget(),
        )

        self.assertEqual(diagnostics, [])
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.table_source_node_id, table_node.source_node_id)
        self.assertEqual(snapshot.crop_image_sha256, provider.inputs[0].crop_image_sha256)
        self.assertEqual(provider.inputs[0].render_scale, 2.0)
        self.assertFalse(provider.inputs[0].annotations_included)
        self.assertEqual(len(page.calls), 1)
        call = page.calls[0]
        self.assertEqual((call["matrix"].a, call["matrix"].d), (2.0, 2.0))
        self.assertFalse(call["alpha"])
        self.assertFalse(call["annots"])
        self.assertEqual(tuple(call["clip"]), table_node.native_locator.bbox)

    def test_missing_provider_is_explicit_error_and_never_renders(self) -> None:
        page = _Page()

        snapshot, diagnostics = _visual_table_snapshot_from_native_table(
            page=page,
            page_number=1,
            page_locator=_page_locator(),
            table_node=_table_node(),
            source_bytes=SOURCE_BYTES,
            source_sha256=SOURCE_HASH,
            provider=None,
            resource_budget=_PdfResourceBudget(),
        )

        self.assertIsNone(snapshot)
        self.assertEqual(diagnostics[0].code, "pdf_visual_table_provider_not_configured")
        self.assertEqual(diagnostics[0].severity.value, "error")
        self.assertEqual(page.calls, [])

    def test_provider_multiple_table_rejection_stays_machine_readable(self) -> None:
        provider = _RejectingProvider()

        snapshot, diagnostics = _visual_table_snapshot_from_native_table(
            page=_Page(),
            page_number=1,
            page_locator=_page_locator(),
            table_node=_table_node(),
            source_bytes=SOURCE_BYTES,
            source_sha256=SOURCE_HASH,
            provider=provider,
            resource_budget=_PdfResourceBudget(),
        )

        self.assertIsNone(snapshot)
        self.assertEqual(diagnostics[0].code, "pdf_visual_table_multiple_tables")
        self.assertEqual(diagnostics[0].severity.value, "error")

    def test_table_provider_requires_explicit_authorized_page_scope(self) -> None:
        with self.assertRaisesRegex(TypeError, "explicit"):
            PdfSourceIRAdapter(
                visual_text_provider=_PageTextProvider(),
                visual_table_provider=_Provider(),
            )


class _Pixmap:
    width = 1000
    height = 400

    def tobytes(self, format_name: str) -> bytes:
        assert format_name == "png"
        return b"bounded annotation-free PNG"


class _Page:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_pixmap(self, **kwargs: object) -> _Pixmap:
        self.calls.append(kwargs)
        return _Pixmap()


class _PageTextProvider:
    provider_id = "fixture-remote"
    provider_version = "page-v1"


class _Provider:
    provider_id = "fixture-remote"
    provider_version = "table-v1"

    def __init__(self) -> None:
        self.inputs: list[object] = []

    def parse_table(self, crop_input: object) -> VisualTableSnapshot:
        self.inputs.append(crop_input)
        table_node = _table_node()
        table = table_node.table_payload
        assert table is not None
        snapshot_id = stable_visual_table_snapshot_id(
            source_sha256=crop_input.source_sha256,
            crop_image_sha256=crop_input.crop_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=crop_input.page_number,
            table_source_node_id=crop_input.table_source_node_id,
            table_id=crop_input.table_id,
            table_index=crop_input.table_index,
            table_bbox=crop_input.table_bbox,
            crop_image_width_px=crop_input.crop_image_width_px,
            crop_image_height_px=crop_input.crop_image_height_px,
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
                raw_text=f"visual-{cell.row}-{cell.column}",
            )
            for cell in table.cells
        )
        return VisualTableSnapshot(
            visual_table_snapshot_id=snapshot_id,
            source_sha256=crop_input.source_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=crop_input.page_number,
            page_bbox=crop_input.page_bbox,
            table_source_node_id=crop_input.table_source_node_id,
            table_id=crop_input.table_id,
            table_index=crop_input.table_index,
            table_bbox=crop_input.table_bbox,
            crop_image_sha256=crop_input.crop_image_sha256,
            crop_image_width_px=crop_input.crop_image_width_px,
            crop_image_height_px=crop_input.crop_image_height_px,
            row_count=table.row_count,
            column_count=table.column_count,
            cells=visual_cells,
        )


class _RejectingProvider(_Provider):
    def parse_table(self, crop_input: object) -> VisualTableSnapshot:
        raise VisualTableProviderRejected(
            VisualTableProviderRejectionCode.MULTIPLE_TABLES,
            "More than one table was returned.",
        )


def _page_locator() -> NativeLocator:
    return NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        bbox=(0.0, 0.0, 600.0, 800.0),
    )


def _table_node() -> SourceNode:
    locator = NativeLocator(
        format=NativeDocumentFormat.PDF,
        page_number=1,
        table_index=0,
        bbox=(50.0, 100.0, 550.0, 300.0),
    )
    table_id = stable_table_id(
        source_sha256=SOURCE_HASH,
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        native_locator=locator,
    )
    cells = tuple(
        TableCellIR(
            cell_id=stable_cell_id(table_id, row, column),
            row=row,
            column=column,
            raw_text=f"native-{row}-{column}",
            native_locator=NativeLocator(
                format=NativeDocumentFormat.PDF,
                page_number=1,
                table_index=0,
                row_index=row,
                column_index=column,
                bbox=(
                    50.0 + column * 250.0,
                    100.0 + row * 100.0,
                    300.0 + column * 250.0,
                    200.0 + row * 100.0,
                ),
            ),
        )
        for row in range(2)
        for column in range(2)
    )
    return SourceNode(
        source_node_id=stable_node_id(
            source_sha256=SOURCE_HASH,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            kind=SourceNodeKind.TABLE,
            native_locator=locator,
        ),
        kind=SourceNodeKind.TABLE,
        source_order=0,
        native_locator=locator,
        table_payload=TableIR(
            table_id=table_id,
            row_count=2,
            column_count=2,
            cells=cells,
        ),
    )


if __name__ == "__main__":
    unittest.main()
