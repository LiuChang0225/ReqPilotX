from __future__ import annotations

# Test runtime isolation must be active before application imports.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

import pymupdf

from app.adapters.agents.codex_app_server.synthetic_pdf_fixture import (
    build_synthetic_pdf,
)
from app.adapters.agents.reference_workspace import (
    ReferenceWorkspaceError,
    materialize_product_evidence_workspace,
    materialize_reference_workspace,
    verify_reference_workspace,
)
from scripts.reference_workspace_tools import (
    WorkspaceToolError,
    render_page,
    render_region,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class ReferenceWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        os.chmod(self.root, 0o700)
        self.corpus = self.root / "corpus"
        self.corpus.mkdir(mode=0o700)
        two_page = self.corpus / "two-page.pdf"
        build_synthetic_pdf(two_page, include_prompt_injection=True)
        self.source = self.corpus / "three-page.pdf"
        original = pymupdf.open(two_page)
        combined = pymupdf.open()
        combined.insert_pdf(original)
        combined.new_page()
        combined.save(self.source, no_new_id=True, clean=True, deflate=True, garbage=4)
        combined.close()
        original.close()
        self.source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.output = self.root / "reference-workspace"

    def _materialize(self) -> dict[str, object]:
        return materialize_reference_workspace(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="SYNTHETIC_TEST",
            output_directory=self.output,
            corpus_root=self.corpus,
            repository_root=REPOSITORY_ROOT,
            run_id="agent-run-" + "1" * 32,
            opaque_source_handle="source-handle-" + "2" * 32,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_materializes_complete_source_bound_workspace(self) -> None:
        source_files_before = tuple(sorted(path.name for path in self.corpus.iterdir()))

        manifest = self._materialize()
        bundle_sha256 = str(manifest["workspace_bundle_sha256"])
        validation = verify_reference_workspace(
            self.output,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=bundle_sha256,
        )

        self.assertEqual(manifest["document"]["page_count"], 3)
        self.assertEqual(validation["status"], "PASS")
        self.assertEqual(validation["source_sha256"], self.source_sha256)
        self.assertEqual(
            hashlib.sha256(
                (self.output / "source/document.pdf").read_bytes()
            ).hexdigest(),
            self.source_sha256,
        )
        self.assertEqual(len(tuple((self.output / "thumbnails").glob("*.png"))), 3)
        self.assertEqual(len(tuple((self.output / "renders/high").glob("*.png"))), 3)
        self.assertTrue((self.output / "contact-sheet.png").is_file())
        self.assertTrue((self.output / "pages/index.jsonl").is_file())
        self.assertTrue((self.output / "pages/text-index.jsonl").is_file())
        self.assertTrue((self.output / "pages/source-map.jsonl").is_file())
        ordinal_path = self.output / "pages/source-ordinal-ledger.jsonl"
        self.assertTrue(ordinal_path.is_file())
        ordinal_records = tuple(
            json.loads(line)
            for line in ordinal_path.read_text(encoding="utf-8").splitlines()
        )
        self.assertEqual(
            tuple(item["source_ordinal"] for item in ordinal_records),
            tuple(range(len(ordinal_records))),
        )
        self.assertEqual(
            manifest["paths"]["source_ordinal_ledger"],
            "pages/source-ordinal-ledger.jsonl",
        )
        self.assertGreaterEqual(manifest["counts"]["physical_tables"], 1)
        self.assertTrue((self.output / "tables/T0001.json").is_file())
        self.assertTrue((self.output / "tables/T0001.html").is_file())
        table_payload = json.loads(
            (self.output / "tables/T0001.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            table_payload["schema_version"],
            "reqpilotx-reference-physical-table/v2",
        )
        self.assertEqual(
            tuple(item["canonical_cell_ordinal"] for item in table_payload["cells"]),
            tuple(range(len(table_payload["cells"]))),
        )
        self.assertEqual(
            tuple(
                (item["row"], item["column"], item["cell_id"])
                for item in table_payload["cells"]
            ),
            tuple(
                sorted(
                    (
                        (item["row"], item["column"], item["cell_id"])
                        for item in table_payload["cells"]
                    )
                )
            ),
        )
        self.assertTrue((self.output / "assets/placements.jsonl").is_file())
        self.assertGreaterEqual(manifest["counts"]["assets"], 1)
        self.assertGreaterEqual(manifest["counts"]["asset_placements"], 1)
        self.assertTrue((self.output / "scripts/workspace_tools.py").is_file())
        self.assertEqual(
            tuple(sorted(path.name for path in self.corpus.iterdir())),
            source_files_before,
        )

    def test_product_workspace_has_distinct_read_only_identity(self) -> None:
        manifest = materialize_product_evidence_workspace(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="CUSTOMER_SOR",
            output_directory=self.output,
            corpus_root=self.corpus,
            repository_root=REPOSITORY_ROOT,
            run_id="agent-run-" + "1" * 32,
            opaque_source_handle="source-handle-" + "2" * 32,
        )
        bundle_sha256 = str(manifest["workspace_bundle_sha256"])
        validation = verify_reference_workspace(
            self.output,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=bundle_sha256,
        )

        self.assertEqual(
            manifest["schema_version"],
            "reqpilotx-product-evidence-workspace/v2",
        )
        self.assertEqual(
            manifest["workspace_kind"],
            "APP_SERVER_PRODUCT_READ_ONLY",
        )
        self.assertEqual(manifest["run"]["run_id"], "agent-run-" + "1" * 32)
        self.assertEqual(
            validation["workspace_kind"],
            "APP_SERVER_PRODUCT_READ_ONLY",
        )
        self.assertTrue(manifest["provenance"]["is_product_read_only_workspace"])
        self.assertFalse(manifest["provenance"]["is_reference_discovery_workspace"])
        instructions = (self.output / "WORKSPACE.md").read_text(encoding="utf-8")
        self.assertIn("read-only source plane", instructions)
        self.assertNotIn("Write temporary work", instructions)

    def test_workspace_tools_write_only_mutable_scratch_and_keep_lock_valid(
        self,
    ) -> None:
        manifest = self._materialize()
        bundle_sha256 = str(manifest["workspace_bundle_sha256"])
        page_receipt = render_page(
            self.output,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=bundle_sha256,
            page_number=2,
            detail="readable",
        )
        region_receipt = render_region(
            self.output,
            expected_source_sha256=self.source_sha256,
            expected_bundle_sha256=bundle_sha256,
            page_number=1,
            bbox=(50.0, 50.0, 300.0, 300.0),
            scale=2.0,
        )

        self.assertTrue((self.output / str(page_receipt["path"])).is_file())
        self.assertTrue((self.output / str(region_receipt["path"])).is_file())
        self.assertEqual(
            verify_reference_workspace(
                self.output,
                expected_source_sha256=self.source_sha256,
                expected_bundle_sha256=bundle_sha256,
            )["status"],
            "PASS",
        )

    def test_tamper_and_corpus_output_fail_closed(self) -> None:
        with self.assertRaisesRegex(
            ReferenceWorkspaceError,
            "reference_workspace_output_inside_corpus",
        ):
            materialize_reference_workspace(
                self.source,
                expected_source_sha256=self.source_sha256,
                document_role="SYNTHETIC_TEST",
                output_directory=self.corpus / "forbidden",
                corpus_root=self.corpus,
                repository_root=REPOSITORY_ROOT,
            )

        manifest = self._materialize()
        bundle_sha256 = str(manifest["workspace_bundle_sha256"])
        page = self.output / "pages/P0001.md"
        page.chmod(0o600)
        page.write_text(page.read_text(encoding="utf-8") + "drift", encoding="utf-8")
        with self.assertRaisesRegex(
            ReferenceWorkspaceError,
            "reference_workspace_inventory_drift",
        ):
            verify_reference_workspace(
                self.output,
                expected_source_sha256=self.source_sha256,
                expected_bundle_sha256=bundle_sha256,
            )

    def test_rejects_source_outside_authorized_corpus(self) -> None:
        outside = self.root / "outside.pdf"
        outside.write_bytes(self.source.read_bytes())
        outside_sha256 = hashlib.sha256(outside.read_bytes()).hexdigest()

        with self.assertRaisesRegex(
            ReferenceWorkspaceError,
            "reference_workspace_source_outside_authorized_corpus",
        ):
            materialize_reference_workspace(
                outside,
                expected_source_sha256=outside_sha256,
                document_role="SYNTHETIC_TEST",
                output_directory=self.output,
                corpus_root=self.corpus,
                repository_root=REPOSITORY_ROOT,
            )

    def test_same_source_gets_independent_run_identity(self) -> None:
        first = self._materialize()
        second_output = self.root / "reference-workspace-second"
        second = materialize_reference_workspace(
            self.source,
            expected_source_sha256=self.source_sha256,
            document_role="SYNTHETIC_TEST",
            output_directory=second_output,
            corpus_root=self.corpus,
            repository_root=REPOSITORY_ROOT,
        )

        first_run = first["run"]
        second_run = second["run"]
        assert isinstance(first_run, dict)
        assert isinstance(second_run, dict)
        self.assertNotEqual(first_run["run_id"], second_run["run_id"])
        self.assertNotEqual(
            first_run["opaque_source_handle"],
            second_run["opaque_source_handle"],
        )

    def test_render_rejects_mutable_root_symlink_and_wrong_host_pin(self) -> None:
        manifest = self._materialize()
        bundle_sha256 = str(manifest["workspace_bundle_sha256"])
        with self.assertRaisesRegex(
            WorkspaceToolError,
            "workspace_expected_bundle_mismatch",
        ):
            render_page(
                self.output,
                expected_source_sha256=self.source_sha256,
                expected_bundle_sha256="f" * 64,
                page_number=1,
                detail="thumbnail",
            )

        scratch = self.output / "scratch"
        scratch.rename(self.output / "scratch-real")
        outside = self.root / "outside-scratch"
        outside.mkdir(mode=0o700)
        scratch.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(
            WorkspaceToolError,
            "workspace_symlink_forbidden",
        ):
            render_page(
                self.output,
                expected_source_sha256=self.source_sha256,
                expected_bundle_sha256=bundle_sha256,
                page_number=1,
                detail="thumbnail",
            )


if __name__ == "__main__":
    unittest.main()
