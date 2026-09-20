"""Grounding and preservation checks for the experimental reading adapter."""
import io
import json
from pathlib import Path
import tempfile
import unittest

import fitz

from app.application.build_source_ir import build_source_ir
from app.domain.extraction.source_ir import SourceIRSnapshot
from eval.pdf_reading_candidate import load_page, prepare, select_evidence
from eval.workbuddy_pdf_probe import sha, to_visual_snapshot, write_json


class ReadingCandidateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "input.pdf"
        doc = fitz.open()
        page = doc.new_page(width=200, height=300)
        page.insert_text((20, 40), "Original 150 C")
        page.add_text_annot((20, 40), "Reviewer note")
        doc.save(self.source)
        doc.close()
        self.source_bytes = self.source.read_bytes()
        self.packet_dir = self.root / "packet"
        prepare(self.source, sha(self.source_bytes), [1], self.packet_dir)
        self.packet_path = self.packet_dir / "packet.json"

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self):
        frame = load_page(self.packet_path, 1)
        # Same block/line IDs in different paragraphs must remain separate.
        data = {"text": ["150", "C", "85", "C"], "conf": [90, 91, 92, 93],
                "page_num": [1]*4, "block_num": [1]*4, "par_num": [1, 1, 2, 2],
                "line_num": [1]*4, "left": [20, 100, 20, 100], "top": [100, 100, 150, 150],
                "width": [60, 20, 60, 20], "height": [20]*4}
        return to_visual_snapshot(frame, data, "test-v1")

    def setup_selection(self):
        base = build_source_ir(self.source)
        base_path = self.root / "base.json"
        write_json(base_path, base.model_dump(mode="json"))
        candidate_dir = self.root / "candidates"
        candidate_dir.mkdir()
        path = candidate_dir / "page.json"
        write_json(path, self.candidate().model_dump(mode="json"))
        write_json(candidate_dir / "receipt.json", {
            "packet_sha256": sha(self.packet_path.read_bytes()), "source_sha256": sha(self.source_bytes),
            "pages": [{"page": 1, "snapshot": path.name, "snapshot_sha256": sha(path.read_bytes())}]})
        return base_path, candidate_dir

    def test_annotation_free_frame_and_paragraph_geometry(self):
        from PIL import Image
        frame = load_page(self.packet_path, 1)
        with fitz.open(self.source) as doc:
            raw = doc[0].get_pixmap(matrix=fitz.Matrix(300/72, 300/72), annots=False).tobytes("png")
        self.assertEqual(Image.open(io.BytesIO(raw)).tobytes(),
                         Image.open(io.BytesIO(frame.page_image_bytes)).tobytes())
        candidate = self.candidate()
        self.assertEqual([o.raw_text for o in candidate.observations], ["150 C", "85 C"])
        self.assertIsNone(candidate.observations[0].confidence)
        self.assertAlmostEqual(candidate.observations[0].bbox[0], 20/frame.page_image_width_px*200)
        self.assertEqual(self.source.read_bytes(), self.source_bytes)

    def test_changed_image_rejected_before_use(self):
        (self.packet_dir / "page-0001.png").write_bytes(b"changed")
        with self.assertRaises(Exception) as ctx:
            load_page(self.packet_path, 1)
        self.assertIn("rendered page image", str(ctx.exception))

    def test_zero_duplicate_and_out_of_range_pages_rejected(self):
        for pages in ([0], [1, 1], [2]):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                prepare(self.source, sha(self.source_bytes), pages, self.root / "invalid")
        self.assertFalse((self.root / "invalid").exists())

    def test_select_preserves_native_capture_and_versions_evidence(self):
        base_path, candidate_dir = self.setup_selection()
        original = base_path.read_bytes()
        out = self.root / "selected"
        receipt = select_evidence(self.packet_path, candidate_dir, base_path, out)
        before = SourceIRSnapshot.model_validate_json(original)
        after = SourceIRSnapshot.model_validate_json((out / "source_ir.json").read_bytes())
        self.assertEqual(before.nodes, after.nodes)
        self.assertEqual(before.assets, after.assets)
        self.assertEqual(before.source_ir_id, after.source_ir_id)
        self.assertNotEqual(receipt["base_source_ir_sha256"], receipt["reading_evidence_revision"])
        self.assertEqual(base_path.read_bytes(), original)
        self.assertEqual(receipt["selection_purpose"], "READING_EVIDENCE_ONLY")
        self.assertFalse(receipt["business_results_modified"])

    def test_changed_candidate_and_packet_rejected(self):
        base_path, candidate_dir = self.setup_selection()
        p = candidate_dir / "page.json"
        original = p.read_bytes()
        changed = json.loads(original)
        changed["observations"][0]["raw_text"] = "130 C"
        write_json(p, changed)
        with self.assertRaisesRegex(ValueError, "candidate_changed"):
            select_evidence(self.packet_path, candidate_dir, base_path, self.root / "changed")
        p.write_bytes(original)
        self.packet_path.write_text(self.packet_path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "packet_changed"):
            select_evidence(self.packet_path, candidate_dir, base_path, self.root / "changed")
        self.assertFalse((self.root / "changed").exists())


if __name__ == "__main__":
    unittest.main()
