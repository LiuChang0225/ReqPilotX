from __future__ import annotations

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()


import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

import pymupdf

from app.adapters.documents.pdf_text_reading_projection import (
    project_pdf_source_contents,
    project_pdf_text_block,
)


def line(text, x=10.0, y=20.0, size=5.0, font="Synthetic", direction=(1.0, 0.0)):
    chars = [{"c": c, "bbox": [x + i * size, y, x + (i + 1) * size, y + size],
              "origin": [x + i * size, y + size]} for i, c in enumerate(text)]
    return {"dir": direction, "spans": [{"font": font, "size": size, "alpha": 255, "color": 0, "chars": chars}]}


class PdfReadingProjectionTests(unittest.TestCase):
    def test_complete_line_is_selected_from_four_paint_passes(self):
        block = {"lines": [line("LEFT", 10.24), line("LEFT", 10, 20.24),
                           line("LEFT", 10.24, 20.24), line("LEFT 123", 10),
                           line("123", 35, 20.24), line("123", 35.24, 20.24), line("123", 35),
                           line("RIGHT", 10.24, 28), line("RIGHT 456", 10, 28),
                           line("456", 40.24, 28.24)]}
        original = copy.deepcopy(block)
        result = project_pdf_text_block(block)
        self.assertEqual(result["display_text"], "LEFT 123\nRIGHT 456")
        self.assertEqual(len(result["duplicate_ranges"]), 8)
        raw = "\n".join("".join(c["c"] for c in ln["spans"][0]["chars"]) for ln in block["lines"])
        self.assertEqual("\n".join(raw[r["start_offset"]:r["end_offset"]] for r in result["source_ranges"]), result["display_text"])
        for duplicate in result["duplicate_ranges"]:
            self.assertEqual(raw[duplicate["start_offset"]:duplicate["end_offset"]],
                             raw[duplicate["retained_start_offset"]:duplicate["retained_end_offset"]])
        self.assertEqual(block, original)

    def test_blank_lines_are_retained_in_range_projection(self):
        block = {"lines": [line("ONE"), line("ONE", x=10.2), line(""), line("TWO", y=40)]}
        result = project_pdf_text_block(block)
        self.assertEqual(result["display_text"], "ONE\n\nTWO")
        raw = "ONE\nONE\n\nTWO"
        self.assertEqual("\n".join(raw[r["start_offset"]:r["end_offset"]] for r in result["source_ranges"]), result["display_text"])

    def test_partial_overlap_is_explicit_when_other_complete_lines_fold(self):
        block = {"lines": [line("HEADER"), line("HEADER", x=10.2),
                           line("(ABC", y=40), line("ABC)", x=15, y=40)]}
        result = project_pdf_text_block(block)
        self.assertEqual(result["display_text"], "HEADER\n(ABC\nABC)")
        self.assertEqual(result["remaining_partial_overlaps"][0]["matching_character_count"], 3)

    def test_same_text_at_different_positions_survives(self):
        self.assertIsNone(project_pdf_text_block({"lines": [line("SAME"), line("SAME", y=30), line("SAME", x=60)]}))

    def test_partial_overlaps_and_prefix_at_another_location_survive(self):
        self.assertIsNone(project_pdf_text_block({"lines": [line("(ABC"), line("ABC)", x=15), line("ABC", y=50)]}))

    def test_different_fonts_invisible_spans_and_missing_geometry_do_not_fold(self):
        for other in [line("SAME", font="Other"), line("SAME", direction=(0.0, 1.0))]:
            self.assertIsNone(project_pdf_text_block({"lines": [line("SAME"), other]}))
        invisible = line("SAME"); invisible["spans"][0]["alpha"] = 0
        self.assertIsNone(project_pdf_text_block({"lines": [invisible, copy.deepcopy(invisible)]}))
        missing = line("SAME"); missing["spans"][0]["chars"][0].pop("origin")
        self.assertIsNone(project_pdf_text_block({"lines": [line("SAME"), missing]}))

    def test_tolerance_scales_with_font_and_has_absolute_cap(self):
        self.assertIsNone(project_pdf_text_block({"lines": [line("X", size=1), line("X", x=10.1, size=1)]}))
        self.assertIsNone(project_pdf_text_block({"lines": [line("X", size=30), line("X", x=10.4, size=30)]}))
        self.assertEqual(project_pdf_text_block({"lines": [line("X", size=5), line("X", x=10.24, size=5)]})["display_text"], "X")

    def test_rotated_pdf_binding_and_wrong_hash_or_location(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.pdf"
            doc = pymupdf.open(); page = doc.new_page(width=300, height=200)
            for x, y in [(30, 50), (30.2, 50), (30, 50.2), (30.2, 50.2)]:
                page.insert_text((x, y), "Test source", fontsize=10)
            page.set_rotation(90); doc.save(path); doc.close()
            payload = path.read_bytes(); sha = hashlib.sha256(payload).hexdigest()
            with pymupdf.open(path) as doc:
                page = doc[0]; block = page.get_text("rawdict", sort=False)["blocks"][0]
                text = "\n".join("".join(char["c"] for span in ln["spans"] for char in span["chars"]) for ln in block["lines"])
                content = {"content_id": "content-1", "page": 1, "bbox": list(pymupdf.Rect(block["bbox"]) * page.rotation_matrix), "text": text}
            results = project_pdf_source_contents(path, expected_source_sha256=sha, contents=[content])
            self.assertEqual(results["content-1"]["display_text"], "Test source")
            self.assertEqual(results["content-1"]["source_sha256"], sha)
            from app.application.agent_workflow.native_structure_bridge import _apply_tree_projection_operations, _content_reading_projections
            from app.application.agent_workflow.structure_tool_loop import StructureProjectionOperationV1, StructureProjectionOperationKind
            tree = {"nodes": [{"node_id": "section", "source_content": [content]}]}
            operations = (StructureProjectionOperationV1(
                operation=StructureProjectionOperationKind.COLLAPSE_REPEATED_SOURCE_LINES,
                target_scope_id="section"),)
            projected = _apply_tree_projection_operations(tree, operations=operations)
            self.assertEqual(projected, tree)
            reading = _content_reading_projections(tree["nodes"], operations=operations,
                                                  source_path=path, source_sha256=sha)
            self.assertEqual(reading["content-1"], results["content-1"])
            self.assertNotIn("display_text", tree["nodes"][0]["source_content"][0])
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(project_pdf_source_contents(path, expected_source_sha256=sha, contents=[{**content, "bbox": [0, 0, 10, 10]}]), {})
            with self.assertRaisesRegex(ValueError, "source_hash_mismatch"):
                project_pdf_source_contents(path, expected_source_sha256="0" * 64, contents=[content])
            with self.assertRaisesRegex(ValueError, "content_hash_mismatch"):
                project_pdf_source_contents(path, expected_source_sha256=sha, contents=[{**content, "text_sha256": "0" * 64}])


if __name__ == "__main__":
    unittest.main()
