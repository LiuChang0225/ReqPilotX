from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pymupdf

from app.adapters.documents.pdf_page_evidence_cache import PdfPageEvidenceCache
from app.adapters.documents import pdf_source_ir_adapter as pdf_adapter
from app.application.fast_source_capture import _capture_gaps, capture_fast_source
from app.domain.extraction.source_ir import SourceDiagnosticSeverity
from app.domain.extraction.source_ir import SourceIRSnapshot
from app.application.extraction.resolve_structure import build_structure_tree


def _table_page(document: pymupdf.Document, label: str) -> None:
    page = document.new_page(width=300, height=300)
    for x in (30, 140, 250):
        page.draw_line((x, 50), (x, 150))
    for y in (50, 100, 150):
        page.draw_line((30, y), (250, y))
    page.insert_text((40, 75), label)
    page.insert_text((150, 75), "Condition")
    page.insert_text((40, 125), "12")
    page.insert_text((150, 125), "At 20 C")


class FastSourceCaptureTests(unittest.TestCase):
    def test_repeated_number_gaps_keep_distinct_source_findings_and_unchanged_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'repeated-outline.pdf'
            with pymupdf.open() as document:
                for _ in range(2):
                    page=document.new_page(width=300,height=300)
                    page.insert_text((30,60),'3.10 Interface conditions',fontsize=11)
                    page.insert_text((30,110),'3.12 State retention',fontsize=11)
                document.save(source)
            full=capture_fast_source(source,output_dir=root/'full')
            raw=capture_fast_source(source,output_dir=root/'raw',include_structure=False)
            full_ir=json.loads((root/'full/source_ir.json').read_text())
            raw_ir=json.loads((root/'raw/source_ir.json').read_text())
            self.assertEqual(full['source_ir_id'],raw['source_ir_id'])
            self.assertEqual({k:v for k,v in full_ir.items() if k!='created_at'},
                             {k:v for k,v in raw_ir.items() if k!='created_at'})
            tree=json.loads((root/'full/structure.json').read_text())
            findings=[f for f in tree['findings'] if f['code']=='section_numbering_gap']
            self.assertEqual(len(findings),2)
            self.assertEqual(len({f['finding_id'] for f in findings}),2)
            pages={n['source_node_id']:n['native_locator']['page_number'] for n in full_ir['nodes']}
            self.assertEqual({pages[f['source_refs'][0]['source_id']] for f in findings},{1,2})
            self.assertTrue(all(f['severity']=='WARNING' and not f['blocking'] for f in findings))
            replay=build_structure_tree(SourceIRSnapshot.model_validate(raw_ir)).model_dump(mode='json')
            self.assertEqual(tree,replay)
            self.assertEqual((root/'full/source.pdf').read_bytes(),source.read_bytes())
            self.assertEqual(full['semantic_status']['document_map'],'not_decided')

    def test_capture_errors_remain_explicit_even_without_a_named_visual_gap(self) -> None:
        snapshot = SimpleNamespace(diagnostics=(SimpleNamespace(
            severity=SourceDiagnosticSeverity.ERROR,
            code="pdf_page_load_failed",
            native_locator=SimpleNamespace(page_number=2),
            message="A native page could not be loaded.",
        ),))
        self.assertEqual(_capture_gaps(snapshot)[0]["pages"], [2])

    def test_bundle_preserves_original_tables_blank_pages_and_does_not_use_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            with pymupdf.open() as document:
                _table_page(document, "Target")
                document.new_page(width=300, height=300)
                document.set_toc([[1, "A source bookmark", 1]])
                document.save(source)
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network forbidden")):
                receipt = capture_fast_source(source, output_dir=Path(directory) / "capture")
            self.assertEqual(receipt["semantic_status"]["classification"], "not_run")
            self.assertEqual(receipt["semantic_status"]["source_items"], "not_decided")
            self.assertEqual([page["page"] for page in receipt["page_facts"]], [1, 2])
            original = Path(directory) / "capture" / receipt["artifacts"]["source_document"]
            self.assertEqual(original.read_bytes(), source.read_bytes())
            ir = json.loads((Path(directory) / "capture/source_ir.json").read_text())
            table = next(node["table_payload"] for node in ir["nodes"] if node["kind"] == "table")
            self.assertIn("At 20 C", [cell["raw_text"] for cell in table["cells"]])
            self.assertTrue((Path(directory) / "capture/structure.json").is_file())
            outline = json.loads((Path(directory) / "capture/native_outline.json").read_text())
            self.assertEqual(outline["nodes"], [{"native_order": 0, "level": 1, "title": "A source bookmark", "page_number": 1}])
            with self.assertRaises(FileExistsError):
                capture_fast_source(source, output_dir=Path(directory) / "capture")

    def test_wrong_source_hash_is_rejected_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            with pymupdf.open() as document:
                _table_page(document, "Target")
                document.save(source)
            output = Path(directory) / "capture"
            with self.assertRaisesRegex(ValueError, "source_sha256_mismatch"):
                capture_fast_source(source, output_dir=output, expected_sha256="0" * 64)
            self.assertFalse(output.exists())

    def test_cached_table_text_cannot_be_replaced_by_another_page(self) -> None:
        with pymupdf.open() as document:
            _table_page(document, "FIRST")
            _table_page(document, "SECOND")
            first = PdfPageEvidenceCache(document[0])
            second = PdfPageEvidenceCache(document[1])
            native = pymupdf.Page.find_tables
            calls = []

            def count(page, *args, **kwargs):
                calls.append((page.number, kwargs.get("strategy")))
                return native(page, *args, **kwargs)

            with mock.patch.object(pymupdf.Page, "find_tables", count):
                table = first.find_tables().tables[0]
                first.find_tables(strategy="lines_strict")
                second.find_tables()
                again = first.find_tables().tables[0]
            self.assertIs(table, again)
            self.assertEqual(table.extract()[0][0], "FIRST")
            self.assertEqual(len(calls), 3)

    def test_non_watermark_page_skips_per_cell_character_rescanning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            with pymupdf.open() as document:
                _table_page(document, "Target")
                document.save(source)
            with mock.patch.object(pdf_adapter, "_raw_page_table_cell_text_channels", side_effect=AssertionError("unnecessary whole-page scan")):
                result = capture_fast_source(source, output_dir=Path(directory) / "capture", include_structure=False)
            self.assertEqual(result["capture_counts"]["table"], 1)

    def test_watermark_index_retains_exact_rotated_cell_channel_evidence(self) -> None:
        def block(text, x, y, watermark):
            return {"type": 0, "lines": [{"dir": (0.8, -0.6) if watermark else (1, 0), "spans": [{"alpha": 100 if watermark else 255, "chars": [{"c": value, "bbox": (x + i * 8, y, x + i * 8 + 8, y + 8)} for i, value in enumerate(text)]}]}]}
        page = {"blocks": [block("UA", 10, 10, False), block("COPY", 40, 2, True), block("Other", 10, 35, False)]}
        for rotation in (None, pymupdf.Matrix(0, 1, -1, 0, 200, 0)):
            index = pdf_adapter._PdfTableCellTextIndex(page, rotation)
            for cell in ((0, 0, 100, 20), (0, 0, 250, 250), (0, 16, 200, 40), (180, 0, 200, 100)):
                expected = pdf_adapter._raw_page_table_cell_text_channels(raw_page=page, page_rotation_matrix=rotation, cell_bbox=cell)
                self.assertEqual(index.channels(cell), expected)


if __name__ == "__main__":
    unittest.main()
