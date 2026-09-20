from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pymupdf

from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_ID,
    PDF_SOURCE_IR_ADAPTER_VERSION,
)
from app.application.extraction.resolve_structure import (
    DEFAULT_STRUCTURE_RESOLVER_ID,
    DEFAULT_STRUCTURE_RESOLVER_VERSION,
)
from scripts.extraction_case_runner import CorpusPathError
from scripts.extraction_resource_probe import (
    RESOURCE_PROBE_SCHEMA_VERSION,
    ExtractionResourceProbeRejected,
    _peak_rss_bytes,
    canonical_resource_report_sha256,
    measure_source_structure_resources,
    write_resource_report,
)


class ExtractionResourceProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source.pdf"
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 72), "CUSTOMER SECRET REQUIREMENT")
        document.save(self.source)
        document.close()
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_measurement_is_text_free_source_bound_and_version_bound(self) -> None:
        report = measure_source_structure_resources(
            self.source,
            document_id="DOC-SYNTHETIC",
            expected_source_sha256=self.source_sha256,
        )

        self.assertEqual(report["schema_version"], RESOURCE_PROBE_SCHEMA_VERSION)
        self.assertEqual(
            report["measurement_scope"]["process_mode"],
            "CALLER_PROCESS",
        )
        self.assertEqual(report["document"]["document_id"], "DOC-SYNTHETIC")
        self.assertEqual(
            report["document"]["source_sha256"],
            self.source_sha256,
        )
        self.assertEqual(report["document"]["page_count"], 1)
        self.assertEqual(
            report["pipeline"]["source_ir"]["adapter_id"],
            PDF_SOURCE_IR_ADAPTER_ID,
        )
        self.assertEqual(
            report["pipeline"]["source_ir"]["adapter_version"],
            PDF_SOURCE_IR_ADAPTER_VERSION,
        )
        self.assertEqual(
            report["pipeline"]["structure"]["resolver_id"],
            DEFAULT_STRUCTURE_RESOLVER_ID,
        )
        self.assertEqual(
            report["pipeline"]["structure"]["resolver_version"],
            DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        self.assertEqual(
            report["report_sha256"],
            canonical_resource_report_sha256(report),
        )
        self.assertEqual(
            set(report["measurements"]["stages"]),
            {"preflight", "source_ir", "structure"},
        )
        for metrics in report["measurements"]["stages"].values():
            self.assertGreaterEqual(metrics["wall_ms"], 0)
            self.assertGreaterEqual(metrics["cpu_ms"], 0)
            self.assertGreater(
                metrics["process_peak_rss_bytes_after_stage"],
                0,
            )

        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("CUSTOMER SECRET REQUIREMENT", serialized)
        self.assertNotIn(str(self.source), serialized)
        self.assertNotIn(self.source.name, serialized)
        self.assertFalse(report["privacy"]["contains_customer_text"])
        self.assertFalse(report["privacy"]["is_human_gold"])
        self.assertFalse(report["privacy"]["is_serving_activation_gate"])

    def test_measurement_rejects_source_hash_mismatch(self) -> None:
        with self.assertRaisesRegex(
            ExtractionResourceProbeRejected,
            "resource_probe_source_hash_mismatch",
        ):
            measure_source_structure_resources(
                self.source,
                document_id="DOC-SYNTHETIC",
                expected_source_sha256="0" * 64,
            )

    def test_peak_rss_normalizes_macos_bytes_and_linux_kibibytes(self) -> None:
        self.assertEqual(_peak_rss_bytes(123, platform_name="darwin"), 123)
        self.assertEqual(_peak_rss_bytes(123, platform_name="linux"), 125_952)
        self.assertEqual(_peak_rss_bytes(123, platform_name="win32"), 123)
        self.assertEqual(_peak_rss_bytes(-1, platform_name="linux"), 0)

    def test_report_writer_rejects_corpus_destination(self) -> None:
        with self.assertRaises(CorpusPathError):
            write_resource_report(
                {"schema_version": RESOURCE_PROBE_SCHEMA_VERSION},
                self.root / "report.json",
                corpus_root=self.root,
            )

    def test_report_writer_is_non_overwriting_and_cleans_temporary_file(self) -> None:
        corpus_root = self.root / "corpus"
        corpus_root.mkdir()
        output_root = self.root / "reports"
        destination = output_root / "measurement.json"
        report = {"schema_version": RESOURCE_PROBE_SCHEMA_VERSION}

        written = write_resource_report(
            report,
            destination,
            corpus_root=corpus_root,
        )
        self.assertEqual(json.loads(written.read_text(encoding="utf-8")), report)
        self.assertEqual(
            {path.name for path in output_root.iterdir()},
            {"measurement.json"},
        )
        with self.assertRaisesRegex(
            ExtractionResourceProbeRejected,
            "resource_probe_output_exists",
        ):
            write_resource_report(
                report,
                destination,
                corpus_root=corpus_root,
            )


if __name__ == "__main__":
    unittest.main()
