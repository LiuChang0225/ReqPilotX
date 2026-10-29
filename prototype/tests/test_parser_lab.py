"""Synthetic transport/geometry contracts only; not real-document acceptance."""
from __future__ import annotations

from .runtime_isolation import activate_test_runtime
activate_test_runtime()

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

import pymupdf
from PIL import Image
from fastapi.testclient import TestClient

from reqpilotx_server.parser_lab import create_parser_lab_app
from reqpilotx_server.application.parser_lab import ParserLabStore


def synthetic_contract_pdf(*, rotation=0, crop=False):
    document = pymupdf.open()
    page = document.new_page(width=400, height=500)
    page.insert_text((30, 40), "Synthetic parser contract: preserve all native words.")
    for x in (30, 160, 290):
        page.draw_line((x, 100), (x, 180))
    for y in (100, 140, 180):
        page.draw_line((30, y), (290, y))
    for x, y, text in ((40, 125, "Name"), (170, 125, "Value"), (40, 165, "Torque"), (170, 165, "12 Nm")):
        page.insert_text((x, y), text)
    stream = io.BytesIO()
    Image.new("RGB", (20, 15), "red").save(stream, format="PNG")
    page.insert_image(pymupdf.Rect(40, 230, 140, 305), stream=stream.getvalue())
    if crop:
        page.set_cropbox(pymupdf.Rect(10, 20, 390, 480))
    page.set_rotation(rotation)
    page = document.new_page(width=400, height=500)
    page.insert_text((30, 70), "Second synthetic page: must remain visible in complete content.")
    content = document.tobytes()
    document.close()
    return content


class ParserLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="reqpilotx-parser-lab-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.client = TestClient(create_parser_lab_app(root=self.root / "runs", frontend=self.root), base_url="http://127.0.0.1")
        self.addCleanup(self.client.close)

    def run_pdf(self, parser="pymupdf", content=None, **kwargs):
        response = self.client.post("/api/parser-lab/runs", files={"file": ("synthetic-contract-fixture.pdf", content or synthetic_contract_pdf(), "application/pdf")}, data={"parser": parser, **kwargs})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_standalone_declares_no_account_mode_for_shared_web_shell(self):
        response = self.client.get("/api/auth/session")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False, "user": None, "csrf_token": None})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(self.client.post("/api/auth/login", json={}).status_code, 404)

    def test_pymupdf_real_engine_raw_tables_assets_and_immutable_runs(self):
        content = synthetic_contract_pdf()
        run = self.run_pdf(content=content)
        self.assertIn(run["status"], ("COMPLETE", "PARTIAL"), run["diagnostics"])
        self.assertEqual(run["source_sha256"], hashlib.sha256(content).hexdigest())
        self.assertTrue(run["parser_version"])
        self.assertTrue(run["adapter_version"])
        nodes = run["source_ir"]["nodes"]
        self.assertTrue(any("Second synthetic page" in n["raw_text"] for n in nodes))
        table = next(n for n in nodes if n["kind"] == "table")
        self.assertTrue(any(c["raw_text"] == "12 Nm" for c in table["table_payload"]["cells"]))
        image = next(n for n in nodes if n["kind"] in {"image", "figure"})
        self.assertTrue(image["asset_ref"])
        artifact = next(a for a in run["artifacts"] if a["id"] == image["asset_ref"])
        response = self.client.get(f'/api/parser-lab/runs/{run["run_id"]}/artifacts/{artifact["id"]}')
        self.assertEqual(hashlib.sha256(response.content).hexdigest(), artifact["sha256"])
        raw_values = [json.loads((self.root / "runs" / run["run_id"] / a["path"]).read_bytes())
            for a in run["artifacts"] if a["path"].startswith("raw/")]
        self.assertTrue(any(v["method"] == "get_text" and v["config"]["args"] == ["rawdict"] for v in raw_values))
        self.assertTrue(any(v["method"] == "find_tables" and v["output"]["tables"] for v in raw_values))
        old = (self.root / "runs" / run["run_id"] / "source-ir.json").read_bytes()
        second = self.run_pdf(content=content)
        self.assertNotEqual(run["run_id"], second["run_id"])
        self.assertEqual((self.root / "runs" / run["run_id"] / "source-ir.json").read_bytes(), old)
        self.assertEqual(len(self.client.get("/api/parser-lab/runs").json()), 2)

    def test_rotated_cropped_page_metadata_and_bbox_frame(self):
        run = self.run_pdf(content=synthetic_contract_pdf(rotation=90, crop=True))
        self.assertNotEqual(run["status"], "FAILED", run["diagnostics"])
        page = run["pages"][0]
        self.assertEqual(page["rotation"], 90)
        self.assertEqual(page["bbox"], [0, 0, 460, 380])
        self.assertEqual(page["cropbox"], [10, 20, 390, 480])
        self.assertEqual(len(page["rotation_matrix"]), 6)
        for node in run["source_ir"]["nodes"]:
            loc = node["native_locator"]
            if loc["page_number"] == 1 and loc["bbox"]:
                x0, y0, x1, y1 = loc["bbox"]
                self.assertTrue(0 <= x0 < x1 <= 460 and 0 <= y0 < y1 <= 380)

    def test_pdfplumber_real_cells_and_missing_image_diagnostic(self):
        run = self.run_pdf("pdfplumber")
        self.assertEqual(run["status"], "PARTIAL", run["diagnostics"])
        table = next(n for n in run["source_ir"]["nodes"] if n["kind"] == "table")
        self.assertEqual(table["table_payload"]["row_count"], 2)
        self.assertTrue(any(d["code"] == "pdfplumber_image_assets_missing" for d in run["diagnostics"]))
        self.assertFalse(any(n["kind"] == "image" for n in run["source_ir"]["nodes"]))

    def test_pdfplumber_rotated_geometry_is_explicitly_unsupported(self):
        run = self.run_pdf("pdfplumber", content=synthetic_contract_pdf(rotation=90))
        self.assertEqual(run["status"], "PARTIAL", run["diagnostics"])
        self.assertTrue(any(d["code"] == "pdfplumber_geometry_unsupported" for d in run["diagnostics"]))
        self.assertFalse(any(n["native_locator"]["page_number"] == 1 for n in run["source_ir"]["nodes"]))

    def test_invalid_pdf_has_failed_receipt_and_original_bytes(self):
        run = self.run_pdf(content=b"not a PDF")
        self.assertEqual(run["status"], "FAILED")
        self.assertIsNone(run["source_ir"])
        self.assertTrue(run["diagnostics"])
        self.assertEqual((self.root / "runs" / run["run_id"] / "source.pdf").read_bytes(), b"not a PDF")

    def test_unsupported_and_missing_are_not_success(self):
        run = self.run_pdf("docling")
        self.assertEqual(run["status"], "UNSUPPORTED")
        self.assertIsNone(run["source_ir"])

        with mock.patch("reqpilotx_server.application.parser_lab.parser_catalog", return_value=[
            {"id": "pymupdf", "version": None, "availability": "MISSING_DEPENDENCY"}]):
            run = self.run_pdf()
        self.assertEqual(run["status"], "MISSING_DEPENDENCY")
        self.assertIsNone(run["source_ir"])

    def test_missing_ocr_language_is_distinct_from_parse_failure(self):
        from reqpilotx.adapters.documents.tesseract_source_ir_adapter import TesseractDependencyMissing
        with mock.patch("reqpilotx.adapters.documents.tesseract_source_ir_adapter.extract_tesseract",
                side_effect=TesseractDependencyMissing("TESSERACT_LANGUAGE_DATA_MISSING: chi_sim")):
            run = self.run_pdf("tesseract", language="chi_sim")
        self.assertEqual(run["status"], "MISSING_DEPENDENCY")
        self.assertIsNone(run["source_ir"])

    def test_artifact_integrity_and_no_arbitrary_paths(self):
        run = self.run_pdf()
        (self.root / "runs" / run["run_id"] / "source.pdf").write_bytes(b"changed")
        url = f'/api/parser-lab/runs/{run["run_id"]}/artifacts/source'
        self.assertEqual(self.client.get(url).status_code, 409)
        self.assertEqual(self.client.get(f'/api/parser-lab/runs/{run["run_id"]}/artifacts/run.json').status_code, 404)
        self.assertEqual(self.client.get('/api/parser-lab/runs/invalid').status_code, 404)

    def test_request_boundary_rejects_external_host_and_origin(self):
        self.assertEqual(self.client.get('/api/parser-lab/runs', headers={"host": "evil.example"}).status_code, 400)
        self.assertEqual(self.client.post('/api/parser-lab/runs', headers={"origin": "https://evil.example"}).status_code, 403)

    def test_local_cli_import_needs_no_db_or_business_runtime(self):
        repository = Path(__file__).resolve().parents[2]
        environment = {k: v for k, v in os.environ.items() if not k.startswith(("REQPILOT_", "REQPILOTX_"))}
        environment["PYTHONPATH"] = os.pathsep.join(str(repository / p) for p in ("packages/engine/src", "apps/server/src"))
        result = subprocess.run([sys.executable, "-B", "-c",
            "import sys; from reqpilotx_server.parser_lab import create_parser_lab_app; "
            "assert 'reqpilotx_server.db' not in sys.modules; "
            "assert 'reqpilotx_server.application.runtime' not in sys.modules"],
            env=environment, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ocr_is_separate_visual_layer_with_raw_tsv(self):
        import shutil
        if not shutil.which("tesseract"):
            self.skipTest("Local Tesseract not installed")
        run = self.run_pdf("tesseract")
        self.assertEqual(run["status"], "PARTIAL", run["diagnostics"])
        self.assertEqual(run["source_ir"]["nodes"], [])
        observations = run["source_ir"]["visual_text_snapshots"]
        self.assertEqual(len(observations), 2)
        self.assertTrue(any("Synthetic" in o["raw_text"] for o in observations[0]["observations"]))
        self.assertTrue(any("tesseract-tsv" in a["path"] for a in run["artifacts"]))


if __name__ == "__main__":
    unittest.main()
