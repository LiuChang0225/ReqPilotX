from __future__ import annotations

# Test runtime isolation must be active before application imports.

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import fitz

from app.adapters.documents.pdf_visual_table_recovery import (
    PdfVisualTableRecoveryError,
    recover_visual_subcell_table_fragments,
)
from app.application.agent_workflow.native_structure_bridge import (
    _table_presentation,
)
from app.application.agent_workflow.structure_tool_loop import (
    StructureProjectionOperationKind,
    StructureProjectionOperationV1,
)


class PdfVisualTableRecoveryTests(unittest.TestCase):
    def test_recovers_vector_grid_without_absorbing_adjacent_text(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "visual-table.pdf"
            document = fitz.open()
            page = document.new_page(width=320, height=240)
            page.insert_text((30, 32), "Section before", fontsize=10)
            vertical = (30.0, 110.0, 190.0, 290.0)
            horizontal = (70.0, 100.0, 140.0)
            for x in vertical:
                page.draw_line((x, horizontal[0]), (x, horizontal[-1]))
            for y in horizontal:
                page.draw_line((vertical[0], y), (vertical[-1], y))
            page.insert_text((42, 89), "Item", fontsize=9)
            page.insert_text((122, 89), "Date", fontsize=9)
            page.insert_text((202, 89), "Volume", fontsize=9)
            page.insert_text((42, 124), "P1", fontsize=9)
            page.insert_text((122, 124), "2027-01", fontsize=9)
            page.insert_text((202, 124), "100 units", fontsize=9)
            page.insert_text((30, 172), "Section after must stay outside", fontsize=10)
            document.save(source)
            document.close()

            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            specs = (
                {
                    "page": 1,
                    "table_token": "T0001",
                    # Deliberately describes only the visible text region. The
                    # adapter must recover the enclosing vector grid.
                    "visual_bbox": [40.0, 74.0, 280.0, 126.0],
                },
            )
            fragments, receipt = recover_visual_subcell_table_fragments(
                source,
                expected_source_sha256=source_sha256,
                logical_table_scope_id="lt-milestones",
                fragment_specs=specs,
            )
            repeated, repeated_receipt = recover_visual_subcell_table_fragments(
                source,
                expected_source_sha256=source_sha256,
                logical_table_scope_id="lt-milestones",
                fragment_specs=specs,
            )

        self.assertEqual(fragments, repeated)
        self.assertEqual(receipt, repeated_receipt)
        self.assertEqual(receipt["status"], "RECOVERED")
        self.assertEqual(receipt["method"], "NATIVE_WORDS_WITH_VECTOR_GRID")
        self.assertEqual(len(fragments), 1)
        fragment = fragments[0]
        self.assertEqual(fragment["row_count"], 2)
        self.assertEqual(fragment["column_count"], 3)
        self.assertEqual(fragment["row_range"], [0, 2])
        self.assertEqual(
            [cell["text"] for cell in fragment["cells"]],
            ["Item", "Date", "Volume", "P1", "2027-01", "100 units"],
        )
        self.assertNotIn(
            "Section after",
            " ".join(cell["text"] for cell in fragment["cells"]),
        )
        visual_fallback = StructureProjectionOperationV1(
            operation=StructureProjectionOperationKind.USE_VISUAL_TABLE_REGION,
            target_scope_id="lt-milestones",
            logical_table_scope_id="lt-milestones",
        )
        self.assertEqual(
            _table_presentation(
                {},
                scope_id="lt-milestones",
                operations=(visual_fallback,),
                fragments=fragments,
            )["mode"],
            "VISUAL_REGION_ONLY",
        )
        self.assertEqual(
            _table_presentation(
                {},
                scope_id="lt-milestones",
                operations=(visual_fallback,),
                fragments=fragments,
                recovered_visual_cells=True,
            )["mode"],
            "STRUCTURED_CELLS",
        )

    def test_rejects_source_identity_drift(self) -> None:
        with TemporaryDirectory() as directory:
            source = Path(directory) / "visual-table.pdf"
            document = fitz.open()
            document.new_page()
            document.save(source)
            document.close()

            with self.assertRaisesRegex(
                PdfVisualTableRecoveryError,
                "VISUAL_TABLE_SOURCE_IDENTITY_INVALID",
            ):
                recover_visual_subcell_table_fragments(
                    source,
                    expected_source_sha256="0" * 64,
                    logical_table_scope_id="lt-milestones",
                    fragment_specs=(
                        {
                            "page": 1,
                            "table_token": "T0001",
                            "visual_bbox": [10.0, 10.0, 20.0, 20.0],
                        },
                    ),
                )


if __name__ == "__main__":
    unittest.main()
