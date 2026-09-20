
if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import unittest
import pymupdf

from app.application.pdf_annotations import capture_pdf_annotations


class PdfAnnotationTests(unittest.TestCase):
    def test_comment_layer_keeps_original_text_and_role_without_rewriting_body(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "source.pdf"
            pdf = pymupdf.open(); page = pdf.new_page()
            page.insert_text((50, 50), "The current limit is 5 mA.")
            page.add_text_annot((50, 70), "Please clarify whether 4 mA is required.")
            pdf.new_page()  # A blank trailing page is still part of the source.
            pdf.save(path); pdf.close()
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            captured = capture_pdf_annotations(path, expected_sha256=digest)
            self.assertEqual(captured["page_count"], 2)
            self.assertEqual(captured["annotations"][0]["text"], "Please clarify whether 4 mA is required.")
            self.assertEqual(captured["annotations"][0]["source_role"], "PDF_ANNOTATION")
            self.assertEqual(captured["annotations"][0]["normative_status"], "NOT_DETERMINED")
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
            with self.assertRaisesRegex(ValueError, "identity"):
                capture_pdf_annotations(path, expected_sha256="0" * 64)


if __name__ == "__main__":
    unittest.main()
