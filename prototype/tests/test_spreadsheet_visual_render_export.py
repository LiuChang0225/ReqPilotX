from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import dataclasses
import hashlib
import tempfile
import unittest
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.adapters.documents.spreadsheet_visual_profile import (
    SpreadsheetRenderedPage,
    SpreadsheetSheetPageBinding,
    SpreadsheetVisualProfile,
    SpreadsheetVisualRenderConfig,
)
from app.domain.extraction.preflight import DocumentFormat
from scripts.document_visual_review_bundle import (
    DocumentVisualReviewBundleRejected,
    _validate_render_profile,
    canonical_profile_sha256,
)
from scripts.spreadsheet_visual_render_export import (
    SpreadsheetVisualRenderExportRejected,
    export_spreadsheet_visual_render_profile,
)


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", (24, 32), color).save(output, format="PNG")
    return output.getvalue()


def _profile() -> SpreadsheetVisualProfile:
    source_bytes = b"verified XLSX source"
    sheet_hash = hashlib.sha256(b"Requirements").hexdigest()
    pages = []
    for page_number, color in (
        (1, (20, 30, 40)),
        (2, (50, 60, 70)),
    ):
        png_bytes = _png_bytes(color)
        pages.append(
            SpreadsheetRenderedPage(
                page_number=page_number,
                sheet_index=0,
                sheet_name_sha256=sheet_hash,
                width_pixels=24,
                height_pixels=32,
                png_sha256=hashlib.sha256(png_bytes).hexdigest(),
                standalone_pdf_sha256=hashlib.sha256(
                    f"pdf-{page_number}".encode()
                ).hexdigest(),
                png_bytes=png_bytes,
            )
        )
    pdf_bytes = b"synthetic PDF"
    return SpreadsheetVisualProfile(
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        source_size=len(source_bytes),
        source_format=DocumentFormat.XLSX,
        renderer_id="test-spreadsheet-renderer",
        renderer_version="1.0.0",
        libreoffice_version="26.2.4.2",
        libreoffice_build_id="a" * 40,
        libreoffice_executable_sha256="b" * 64,
        pymupdf_version="test",
        config=SpreadsheetVisualRenderConfig(),
        render_output_sha256=hashlib.sha256(b"log").hexdigest(),
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        pdf_bytes=pdf_bytes,
        sheets=(
            SpreadsheetSheetPageBinding(
                sheet_index=0,
                sheet_name_sha256=sheet_hash,
                sheet_state="visible",
                page_start=1,
                page_end=2,
            ),
        ),
        pages=tuple(pages),
    )


class SpreadsheetVisualRenderExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_exports_bundle_compatible_profile_with_explicit_sheet_identity(
        self,
    ) -> None:
        source_profile = _profile()
        output = self.root / "render"

        result = export_spreadsheet_visual_render_profile(
            source_profile,
            output_dir=output,
        )

        self.assertEqual(result.root, output.resolve())
        self.assertTrue(result.profile_path.is_file())
        self.assertRegex(result.profile_file_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(result.profile["page_count"], 2)
        for index, page in enumerate(result.profile["pages"]):
            locator = page["locator"]
            self.assertEqual(locator["sheet_index"], 0)
            self.assertEqual(locator["sheet_page_number"], index + 1)
            self.assertEqual(
                locator["sheet_locator"],
                f"sheet-0000-{locator['sheet_name_sha256']}",
            )
            artifact = output / page["artifact"]["path"]
            self.assertEqual(
                hashlib.sha256(artifact.read_bytes()).hexdigest(),
                page["artifact"]["sha256"],
            )
        _validate_render_profile(
            result.profile,
            source_sha256=source_profile.source_sha256,
            document_format="xlsx",
        )
        self.assertFalse(
            any(
                item.name.startswith(".render.")
                for item in self.root.iterdir()
            )
        )

        with self.assertRaises(SpreadsheetVisualRenderExportRejected):
            export_spreadsheet_visual_render_profile(
                source_profile,
                output_dir=output,
            )

    def test_sheet_identity_is_cross_bound_and_old_locator_stays_compatible(
        self,
    ) -> None:
        source_profile = _profile()
        result = export_spreadsheet_visual_render_profile(
            source_profile,
            output_dir=self.root / "render",
        )
        tampered = copy.deepcopy(result.profile)
        tampered["pages"][0]["locator"]["sheet_index"] = 1
        tampered["profile_sha256"] = canonical_profile_sha256(tampered)
        with self.assertRaises(DocumentVisualReviewBundleRejected):
            _validate_render_profile(
                tampered,
                source_sha256=source_profile.source_sha256,
                document_format="xlsx",
            )

        legacy = copy.deepcopy(result.profile)
        for page in legacy["pages"]:
            page["locator"].pop("sheet_index")
            page["locator"].pop("sheet_name_sha256")
        legacy["profile_sha256"] = canonical_profile_sha256(legacy)
        _validate_render_profile(
            legacy,
            source_sha256=source_profile.source_sha256,
            document_format="xlsx",
        )

    def test_incomplete_source_bound_render_cannot_be_exported_for_review(
        self,
    ) -> None:
        source_profile = _profile()
        incomplete = dataclasses.replace(
            source_profile,
            cell_image_audit_sha256="a" * 64,
            cell_image_reference_count=1,
            unresolved_cell_image_reference_count=1,
            render_input_sha256=source_profile.source_sha256,
            visual_fidelity_status="partial_source_render_missing",
            source_render_issue_page_numbers=(1, 2),
        )

        with self.assertRaisesRegex(
            SpreadsheetVisualRenderExportRejected,
            "source-bound spreadsheet render is incomplete",
        ):
            export_spreadsheet_visual_render_profile(
                incomplete,
                output_dir=self.root / "render",
            )


if __name__ == "__main__":
    unittest.main()
