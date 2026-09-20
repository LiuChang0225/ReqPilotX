from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import base64
import hashlib
import io
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pymupdf
from PIL import Image as PILImage
from PIL import ImageChops
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
)

from app.adapters.documents import pdf_source_ir_adapter as pdf_adapter_module
from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
    PageStagingAuthorityLostError,
    PdfSourceIRAdapter,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.build_source_ir import (
    SourceIRBuildRejected,
    SourceIRBuildRejectionCode,
    build_source_ir,
)
from app.application.extraction.build_shadow_preview import build_page_comparison_view
from app.application.extraction.classify_content import classify_content
from app.application.extraction.build_llm_shadow_preview import (
    LlmShadowPreviewProgressCancelled,
    _VisualCaptureProgressProvider,
)
from app.application.extraction.resolve_structure import build_structure_tree
from app.domain.extraction.source_ir import (
    SourceAssetDerivationKind,
    SourceAssetKind,
    SourceDiagnosticSeverity,
    SourceIRStatus,
    SourceNodeKind,
)
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.structure import StructureBoundaryBasis
from app.domain.extraction.visual_text import (
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.ports.source_ir_adapter import (
    SourceIRAdapterRejected,
    SourceIRAdapterRejectionCode,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


CREATED_AT = datetime(2026, 7, 20, 9, 30, tzinfo=timezone.utc)
_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


class PdfSourceIRAdapterTests(unittest.TestCase):
    def test_page_progress_is_initialized_before_document_wide_furniture_scan(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "progress-before-scan.pdf"
            document = pymupdf.open()
            for page_number in range(1, 4):
                page = document.new_page(width=320, height=240)
                page.insert_text((40, 80), f"Page {page_number} requirement")
            document.save(source)
            document.close()
            preflight = LocalDocumentPreflight().inspect(source)
            progress: list[tuple[int, int, int]] = []

            original_scan = pdf_adapter_module._pdf_variable_page_marker_kinds

            def furniture_scan(*args, **kwargs):
                self.assertEqual(progress, [(0, 3, 1)])
                return original_scan(*args, **kwargs)

            with mock.patch.object(
                pdf_adapter_module,
                "_pdf_variable_page_marker_kinds",
                side_effect=furniture_scan,
            ):
                PdfSourceIRAdapter(
                    page_progress_callback=lambda completed, total, current: (
                        progress.append((completed, total, current))
                    )
                ).extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

        self.assertEqual(
            progress,
            [(0, 3, 1), (1, 3, 2), (2, 3, 3), (3, 3, 3)],
        )

    def test_page_progress_callback_does_not_change_source_ir_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "progress.pdf"
            document = pymupdf.open()
            for page_number in range(1, 4):
                page = document.new_page(width=320, height=240)
                page.insert_text((40, 80), f"Page {page_number} requirement")
            document.save(source)
            document.close()
            preflight = LocalDocumentPreflight().inspect(source)
            progress: list[tuple[int, int, int]] = []

            baseline = PdfSourceIRAdapter().extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )
            instrumented = PdfSourceIRAdapter(
                page_progress_callback=lambda completed, total, current: (
                    progress.append((completed, total, current))
                )
            ).extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )

        self.assertEqual(
            baseline.model_dump(mode="json"),
            instrumented.model_dump(mode="json"),
        )
        self.assertEqual(
            progress,
            [(0, 3, 1), (1, 3, 2), (2, 3, 3), (3, 3, 3)],
        )

    def test_page_staging_callback_emits_completed_source_only_slices_without_changing_hash(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "page-staging.pdf"
            document = pymupdf.open()
            for page_number in range(1, 3):
                page = document.new_page(width=320, height=240)
                page.insert_text((40, 80), f"Page {page_number} shall comply")
            document.save(source)
            document.close()
            preflight = LocalDocumentPreflight().inspect(source)
            candidates: list[dict] = []

            baseline = PdfSourceIRAdapter().extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )
            instrumented = PdfSourceIRAdapter(
                page_staging_callback=candidates.append,
            ).extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )

        self.assertEqual(
            baseline.model_dump(mode="json"),
            instrumented.model_dump(mode="json"),
        )
        self.assertEqual([item["page_number"] for item in candidates], [1, 2])
        self.assertEqual([item["total_pages"] for item in candidates], [2, 2])
        self.assertTrue(candidates[0]["nodes"])
        self.assertNotIn("structure", candidates[0])
        self.assertEqual(
            candidates[0]["source_sha256"],
            baseline.source_sha256,
        )

    def test_page_staging_io_failure_is_best_effort_and_does_not_change_source_ir(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "page-staging-io-failure.pdf"
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_text((40, 80), "Supplier shall comply")
            document.save(source)
            document.close()
            preflight = LocalDocumentPreflight().inspect(source)
            baseline = PdfSourceIRAdapter().extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )

            def unavailable(_candidate: dict) -> None:
                raise OSError("object store unavailable")

            instrumented = PdfSourceIRAdapter(
                page_staging_callback=unavailable,
            ).extract(
                source,
                preflight=preflight,
                created_at=CREATED_AT,
            )

        self.assertEqual(
            baseline.model_dump(mode="json"),
            instrumented.model_dump(mode="json"),
        )

    def test_page_staging_authority_loss_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "page-staging-fence.pdf"
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_text((40, 80), "Supplier shall comply")
            document.save(source)
            document.close()
            preflight = LocalDocumentPreflight().inspect(source)

            def stale_delivery(_candidate: dict) -> None:
                raise PageStagingAuthorityLostError("lease lost")

            with self.assertRaises(PageStagingAuthorityLostError):
                PdfSourceIRAdapter(
                    page_staging_callback=stale_delivery,
                ).extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

    def test_native_watermark_proof_requires_diagonal_translucent_text(self) -> None:
        def block(
            *,
            direction: tuple[float, float] = (0.8, -0.6),
            alpha: object = 102,
            text: str = "review copy",
        ) -> dict[str, object]:
            return {
                "type": 0,
                "lines": [
                    {
                        "dir": direction,
                        "spans": [
                            {
                                "alpha": alpha,
                                "chars": [{"c": character} for character in text],
                            }
                        ],
                    }
                ],
            }

        proven = block()
        self.assertIs(
            pdf_adapter_module._native_pdf_text_kind(
                proven,
                fallback=SourceNodeKind.PARAGRAPH,
            ),
            SourceNodeKind.WATERMARK,
        )
        for candidate in (
            block(direction=(1.0, 0.0)),
            block(direction=(0.0, 1.0)),
            block(alpha=255),
            block(alpha=0),
            block(alpha=None),
            block(text="   "),
        ):
            with self.subTest(candidate=candidate):
                self.assertIs(
                    pdf_adapter_module._native_pdf_text_kind(
                        candidate,
                        fallback=SourceNodeKind.HEADER,
                    ),
                    SourceNodeKind.HEADER,
                )

    def test_table_cells_exclude_only_geometry_proven_watermark_glyphs(
        self,
    ) -> None:
        def positioned_block(
            text: str,
            *,
            x0: float,
            y0: float,
            watermark: bool,
        ) -> dict[str, object]:
            chars = [
                {
                    "c": character,
                    "bbox": (
                        x0 + index * 8.0,
                        y0,
                        x0 + (index + 1) * 8.0,
                        y0 + 8.0,
                    ),
                }
                for index, character in enumerate(text)
            ]
            line: dict[str, object] = {
                "dir": (0.8, -0.6) if watermark else (1.0, 0.0),
                "spans": [
                    {
                        "alpha": 100 if watermark else 255,
                        "chars": chars,
                    }
                ],
            }
            return {
                "type": 0,
                "bbox": (
                    x0,
                    y0,
                    x0 + len(text) * 8.0,
                    y0 + 8.0,
                ),
                "lines": [line],
            }

        candidate = _FakeTableCandidate(
            bbox=(0.0, 0.0, 100.0, 20.0),
            row_count=1,
            column_count=1,
            marker="unused",
        )
        candidate._matrix = (("C O P Y\nUA",),)
        raw_page = {
            "blocks": [
                positioned_block(
                    "UA",
                    x0=10.0,
                    y0=10.0,
                    watermark=False,
                ),
                positioned_block(
                    "COPY",
                    x0=40.0,
                    y0=2.0,
                    watermark=True,
                ),
            ]
        }

        node, diagnostics, partial = (
            pdf_adapter_module._table_node_from_candidate(
                candidate=candidate,
                raw_page=raw_page,
                page_rotation_matrix=None,
                page_number=1,
                table_index=0,
                source_sha256="a" * 64,
                adapter_id="test-adapter",
                adapter_version=PDF_SOURCE_IR_ADAPTER_VERSION,
                source_order=0,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertIsNotNone(node)
        assert node is not None and node.table_payload is not None
        self.assertEqual(node.table_payload.cells[0].raw_text, "UA")
        self.assertFalse(partial)
        self.assertEqual(
            [diagnostic.code for diagnostic in diagnostics],
            ["pdf_table_proven_watermark_text_excluded"],
        )

        unexplained = pdf_adapter_module._table_cell_text_without_proven_watermark(
            "C O P Y\nUAX",
            raw_page=raw_page,
            page_rotation_matrix=None,
            cell_bbox=(0.0, 0.0, 100.0, 20.0),
        )
        self.assertIsNone(unexplained)

    def test_variable_page_marker_prepass_propagates_memory_exhaustion(self) -> None:
        document = SimpleNamespace(
            page_count=1,
            load_page=mock.Mock(side_effect=MemoryError),
        )

        with self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded):
            pdf_adapter_module._pdf_variable_page_marker_kinds(
                document,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

    def test_variable_page_marker_candidate_memory_error_uses_budget_failure(
        self,
    ) -> None:
        page = SimpleNamespace(
            rect=(0.0, 0.0, 595.0, 800.0),
            get_text=mock.Mock(
                return_value={
                    "blocks": [{"type": 0, "bbox": (290.0, 775.0, 305.0, 790.0)}]
                }
            ),
        )
        document = SimpleNamespace(
            page_count=1,
            load_page=mock.Mock(return_value=page),
        )

        with (
            mock.patch.object(
                pdf_adapter_module,
                "_raw_text_from_block",
                side_effect=MemoryError,
            ),
            self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded),
        ):
            pdf_adapter_module._pdf_variable_page_marker_kinds(
                document,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

    def test_page_furniture_prepass_has_a_document_wide_block_budget(self) -> None:
        block = {
            "type": 0,
            "bbox": (10.0, 5.0, 40.0, 15.0),
            "lines": [],
        }
        page = SimpleNamespace(
            rect=(0.0, 0.0, 100.0, 100.0),
            get_text=mock.Mock(return_value={"blocks": [block]}),
        )
        document = SimpleNamespace(
            page_count=3,
            load_page=mock.Mock(return_value=page),
        )
        resource_budget = pdf_adapter_module._PdfResourceBudget()

        with (
            mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_PAGE_FURNITURE_SCAN_BLOCKS",
                2,
            ),
            self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded),
        ):
            pdf_adapter_module._pdf_variable_page_marker_kinds(
                document,
                resource_budget=resource_budget,
            )

        self.assertEqual(resource_budget.page_furniture_scan_blocks, 3)
        self.assertEqual(document.load_page.call_count, 3)

    def test_fixed_native_margin_text_is_header_on_three_adjacent_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixed-header.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=("DOC-CODE",) * 3,
                baseline_y=35,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            furniture = tuple(
                node for node in snapshot.nodes if node.raw_text == "DOC-CODE"
            )
            self.assertEqual(len(furniture), 3)
            self.assertTrue(
                all(node.kind is SourceNodeKind.HEADER for node in furniture)
            )

    def test_fixed_native_margin_text_on_only_two_pages_remains_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-page-header.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=("DOC-CODE",) * 2,
                baseline_y=35,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            candidates = tuple(
                node for node in snapshot.nodes if node.raw_text == "DOC-CODE"
            )
            self.assertEqual(len(candidates), 2)
            self.assertTrue(
                all(node.kind is SourceNodeKind.PARAGRAPH for node in candidates)
            )

    def test_repeated_native_margin_image_is_preserved_but_adopted_as_header_furniture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "repeated-header-image.pdf"
            _write_pdf_with_repeated_margin_image(source)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        image_nodes = tuple(
            node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
        )
        self.assertEqual(len(image_nodes), 5)
        header_nodes = tuple(
            node
            for node in image_nodes
            if any(
                diagnostic.code == "pdf_repeated_header_image_furniture"
                for diagnostic in node.extraction_diagnostics
            )
        )
        self.assertEqual(
            [node.native_locator.page_number for node in header_nodes],
            [1, 2, 3, 4],
        )
        self.assertTrue(all(node.asset_ref for node in header_nodes))
        body_node = next(
            node
            for node in image_nodes
            if node.native_locator.page_number == 2
            and node.native_locator.bbox is not None
            and node.native_locator.bbox[1] > 200
        )
        self.assertFalse(
            any(
                diagnostic.code.startswith("pdf_repeated_")
                for diagnostic in body_node.extraction_diagnostics
            )
        )

        structure = build_structure_tree(snapshot)
        structure_by_source_id = {
            ref.source_id: structure_node
            for structure_node in structure.nodes
            for ref in structure_node.source_refs
            if ref.kind.value == "source_node"
        }
        self.assertTrue(
            all(
                structure_by_source_id[node.source_node_id].boundary_basis
                is StructureBoundaryBasis.PAGE_FURNITURE
                for node in header_nodes
            )
        )
        self.assertIs(
            structure_by_source_id[body_node.source_node_id].boundary_basis,
            StructureBoundaryBasis.NATIVE_BLOCK,
        )
        semantics = classify_content(snapshot, structure)
        annotations_by_target = {
            annotation.target_structure_node_id: annotation
            for annotation in semantics.annotations
        }
        self.assertTrue(
            all(
                annotations_by_target[
                    structure_by_source_id[node.source_node_id].structure_node_id
                ].semantic_class
                is SemanticClass.NOISE
                for node in header_nodes
            )
        )
        page_view = build_page_comparison_view(
            snapshot,
            structure,
            page_bounds=((0.0, 0.0, 600.0, 800.0),) * 4,
        )
        block_by_id = {
            block["id"]: block
            for page in page_view["pages"]
            for block in page["blocks"]
        }
        self.assertTrue(
            all(
                block_by_id[node.source_node_id]["type"] == "HEADER"
                for node in header_nodes
            )
        )
        self.assertTrue(
            all(
                block_by_id[node.source_node_id]["asset"]["kind"] == "image"
                for node in header_nodes
            )
        )
        self.assertEqual(block_by_id[body_node.source_node_id]["type"], "FIGURE")

    def test_fixed_text_and_advancing_page_number_block_is_native_footer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "numbered-footer.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=tuple(f"{page_number}\nDOC-CODE" for page_number in range(1, 5)),
                baseline_y=555,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            furniture = tuple(
                node for node in snapshot.nodes if "DOC-CODE" in node.raw_text
            )
            self.assertEqual(
                tuple(node.raw_text for node in furniture),
                tuple(f"{page_number}\nDOC-CODE" for page_number in range(1, 5)),
            )
            self.assertTrue(
                all(node.kind is SourceNodeKind.FOOTER for node in furniture)
            )

    def test_bare_advancing_number_with_fixed_prose_remains_paragraph(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "numbered-running-title.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=tuple(
                    f"{page_number}\nTest stage" for page_number in range(1, 5)
                ),
                baseline_y=35,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            candidates = tuple(
                node for node in snapshot.nodes if "Test stage" in node.raw_text
            )
            self.assertEqual(len(candidates), 4)
            self.assertTrue(
                all(node.kind is SourceNodeKind.PARAGRAPH for node in candidates)
            )

    def test_stacked_current_and_total_page_numbers_are_native_footer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stacked-page-total.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=tuple(f"{page_number}\n28" for page_number in range(1, 5)),
                baseline_y=555,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            furniture = tuple(
                node for node in snapshot.nodes if node.raw_text.endswith("\n28")
            )
            self.assertEqual(len(furniture), 4)
            self.assertTrue(
                all(node.kind is SourceNodeKind.FOOTER for node in furniture)
            )

    def test_current_of_total_page_marker_is_native_footer(self) -> None:
        for template in ("{current} of 4", "Page {current} of 4"):
            with (
                self.subTest(template=template),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = Path(directory) / "current-of-total.pdf"
                _write_pdf_with_repeated_native_text(
                    source,
                    texts=tuple(
                        template.format(current=page_number)
                        for page_number in range(1, 5)
                    ),
                    baseline_y=575,
                )

                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

                furniture = tuple(
                    node for node in snapshot.nodes if node.raw_text.endswith("of 4")
                )
                self.assertEqual(len(furniture), 4)
                self.assertTrue(
                    all(node.kind is SourceNodeKind.FOOTER for node in furniture)
                )

    def test_separate_current_and_total_page_number_blocks_are_native_footer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "separate-page-total.pdf"
            _write_pdf_with_separate_current_and_total_page_numbers(source)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            furniture = tuple(
                node
                for node in snapshot.nodes
                if node.raw_text == "28" or node.raw_text in {"1", "2", "3", "4"}
            )
            self.assertEqual(len(furniture), 8)
            self.assertTrue(
                all(node.kind is SourceNodeKind.FOOTER for node in furniture)
            )

    def test_visual_margin_detection_uses_rotated_page_coordinates(self) -> None:
        for rotation in (90, 180, 270):
            with (
                self.subTest(rotation=rotation),
                tempfile.TemporaryDirectory() as directory,
            ):
                source = Path(directory) / f"rotated-{rotation}.pdf"
                _write_pdf_with_rotated_page_numbers(source, rotation=rotation)

                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

                furniture = tuple(
                    node
                    for node in snapshot.nodes
                    if node.raw_text in {"1", "2", "3", "4"}
                )
                self.assertEqual(len(furniture), 4)
                self.assertTrue(
                    all(node.kind is SourceNodeKind.FOOTER for node in furniture)
                )

    def test_native_block_locators_use_visible_rotated_page_coordinates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rotated-visible-locators.pdf"
            document = pymupdf.open()
            page = document.new_page(width=595, height=842)
            page.set_rotation(90)
            native_baseline = pymupdf.Point(100, 300) * page.derotation_matrix
            page.insert_text(native_baseline, "synthetic rotated fact", fontsize=12)
            document.save(source)
            document.close()

            with pymupdf.open(source) as native_document:
                native_page = native_document[0]
                native_block = next(
                    block
                    for block in native_page.get_text("rawdict", sort=False)["blocks"]
                    if block.get("type") == 0
                )
                raw_bbox = pymupdf.Rect(native_block["bbox"])
                expected_bbox = raw_bbox * native_page.rotation_matrix
                visible_page_bbox = native_page.rect
                self.assertGreater(raw_bbox.y1, visible_page_bbox.y1)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            node = next(
                item
                for item in snapshot.nodes
                if item.raw_text == "synthetic rotated fact"
            )
            assert node.native_locator.bbox is not None
            for actual, expected in zip(
                node.native_locator.bbox,
                tuple(expected_bbox),
                strict=True,
            ):
                self.assertAlmostEqual(actual, expected)
            self.assertGreaterEqual(node.native_locator.bbox[0], visible_page_bbox.x0)
            self.assertGreaterEqual(node.native_locator.bbox[1], visible_page_bbox.y0)
            self.assertLessEqual(node.native_locator.bbox[2], visible_page_bbox.x1)
            self.assertLessEqual(node.native_locator.bbox[3], visible_page_bbox.y1)
            self.assertEqual(node.visual_locator, node.native_locator)

    def test_native_text_lines_bind_exact_raw_text_offsets_and_geometry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "line-geometry.pdf"
            document = pymupdf.open()
            page = document.new_page(width=400, height=300)
            page.insert_textbox(
                pymupdf.Rect(40, 40, 360, 150),
                "4.1.3 previous clause\n4.1.4 selected clause",
                fontsize=11,
            )
            document.save(source)
            document.close()

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            node = next(
                item
                for item in snapshot.nodes
                if "4.1.4 selected clause" in item.raw_text
            )
            self.assertEqual(len(node.pdf_text_lines), 2)
            selected = node.pdf_text_lines[1]
            self.assertEqual(
                node.raw_text[selected.start_offset : selected.end_offset],
                "4.1.4 selected clause",
            )
            self.assertEqual(
                selected.start_offset,
                len("4.1.3 previous clause\n"),
            )
            self.assertGreater(selected.bbox[2], selected.bbox[0])
            self.assertGreater(selected.bbox[3], selected.bbox[1])

    def test_curve_seeded_compound_vector_figure_folds_embedded_fragment(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "compound-vector-figure.pdf"
            _write_pdf_with_compound_vector_figure(
                source,
                include_embedded_fragment=True,
            )
            before = _relative_inventory(root)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            figures = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.FIGURE
            )
            self.assertEqual(len(figures), 1)
            self.assertFalse(
                any(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes)
            )
            figure = figures[0]
            self.assertIsNotNone(figure.visual_locator)
            assert figure.native_locator.bbox is not None
            self.assertLessEqual(figure.native_locator.bbox[0], 38.0)
            self.assertLessEqual(figure.native_locator.bbox[1], 38.0)
            self.assertGreaterEqual(figure.native_locator.bbox[2], 262.0)
            self.assertGreaterEqual(figure.native_locator.bbox[3], 162.0)
            asset = next(
                item for item in snapshot.assets if item.asset_id == figure.asset_ref
            )
            self.assertIs(asset.kind, SourceAssetKind.FIGURE)
            self.assertEqual(asset.media_type, "image/png")
            self.assertTrue(asset.storage_key.endswith(".png"))
            self.assertEqual(
                {
                    diagnostic.code
                    for diagnostic in snapshot.diagnostics
                    if diagnostic.source_node_id == figure.source_node_id
                },
                {
                    "pdf_vector_figure_image_occurrence_folded",
                    "pdf_vector_figure_materialized",
                },
            )
            folded_occurrence = next(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_vector_figure_image_occurrence_folded"
            )
            self.assertIsNotNone(folded_occurrence.native_locator)
            assert folded_occurrence.native_locator is not None
            self.assertIsNotNone(folded_occurrence.native_locator.block_index)
            self.assertEqual(_relative_inventory(root), before)

    def test_curve_figure_masked_fragment_requires_unique_degenerate_companion(
        self,
    ) -> None:
        figure = SimpleNamespace(
            node_id="figure-node",
            region=SimpleNamespace(
                proof_kind="curve_seeded",
                bbox=(0.0, 0.0, 100.0, 100.0),
            ),
        )
        masked = {
            "type": 1,
            "bbox": (10.0, 10.0, 40.0, 30.0),
            "width": 30,
            "height": 20,
            "mask": b"mask",
        }
        companion = {
            "type": 1,
            "bbox": (10.0, 10.0, 40.0, 30.0),
            "width": 1,
            "height": 20,
            "image": _PNG_BYTES,
        }

        ownership = (
            pdf_adapter_module._curve_figure_masked_companion_ownership(
                figures=[figure],
                blocks=(masked, companion),
                rotation_matrix=None,
            )
        )

        self.assertEqual(tuple(ownership), (0,))
        self.assertEqual(ownership[0].companion_block_index, 1)
        self.assertEqual(ownership[0].masked_overlap_ratio, 1.0)
        self.assertEqual(ownership[0].companion_overlap_ratio, 1.0)

        ambiguous = pdf_adapter_module._curve_figure_masked_companion_ownership(
            figures=[figure],
            blocks=(masked, companion, dict(companion)),
            rotation_matrix=None,
        )
        self.assertEqual(ambiguous, {})

        nondegenerate = dict(companion, width=30)
        unsupported = (
            pdf_adapter_module._curve_figure_masked_companion_ownership(
                figures=[figure],
                blocks=(masked, nondegenerate),
                rotation_matrix=None,
            )
        )
        self.assertEqual(unsupported, {})

        insufficient_overlap = dict(
            companion,
            bbox=(39.0, 10.0, 69.0, 30.0),
        )
        self.assertEqual(
            pdf_adapter_module._curve_figure_masked_companion_ownership(
                figures=[figure],
                blocks=(masked, insufficient_overlap),
                rotation_matrix=None,
            ),
            {},
        )

    def test_sparse_unproven_table_layout_with_figure_label_becomes_figure(
        self,
    ) -> None:
        candidate = _FakeTableCandidate(
            bbox=(10.0, 20.0, 90.0, 80.0),
            row_count=5,
            column_count=20,
            marker="diagram",
        )
        candidate.rows = ()
        candidate._matrix = tuple(
            tuple(
                "x" if row_index * 20 + column_index < 8 else ""
                for column_index in range(20)
            )
            for row_index in range(5)
        )

        def find_tables(*, strategy: str | None = None) -> object:
            return SimpleNamespace(
                tables=(candidate,) if strategy is None else (),
            )

        drawings = tuple(
            {
                "fill": (0.2, 0.4, 0.8),
                "fill_opacity": 1.0,
                "rect": (
                    12.0 + (index % 5) * 14.0,
                    25.0 + (index // 5) * 20.0,
                    22.0 + (index % 5) * 14.0,
                    35.0 + (index // 5) * 20.0,
                ),
                "items": (("re",),),
            }
            for index in range(10)
        )
        raw_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(10.0, 5.0, 50.0, 15.0),
                    text="Figure 7",
                )
            ]
        }
        page = SimpleNamespace(
            find_tables=find_tables,
            get_drawings=lambda: drawings,
            get_bboxlog=lambda: (),
        )

        regions = pdf_adapter_module._sparse_filled_table_figure_regions_from_page(
            page=page,
            raw_page=raw_page,
            page_number=1,
            page_bbox=(0.0, 0.0, 100.0, 100.0),
            rotation_matrix=None,
            resource_budget=pdf_adapter_module._PdfResourceBudget(),
        )

        self.assertEqual(len(regions), 1)
        self.assertEqual(regions[0].proof_kind, "sparse_filled_table_figure")
        self.assertEqual(regions[0].owned_table_bboxes, (candidate.bbox,))
        self.assertEqual(regions[0].drawing_count, 10)
        self.assertRegex(regions[0].evidence_sha256 or "", r"^[0-9a-f]{64}$")

        no_figure_regions = (
            pdf_adapter_module._sparse_filled_table_figure_regions_from_page(
                page=page,
                raw_page={
                    "blocks": [
                        _fake_table_suppression_text_block(
                            bbox=(10.0, 5.0, 50.0, 15.0),
                            text="Table 7",
                        )
                    ]
                },
                page_number=1,
                page_bbox=(0.0, 0.0, 100.0, 100.0),
                rotation_matrix=None,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )
        self.assertEqual(no_figure_regions, ())

    def test_sparse_figure_owns_only_exact_provenance_table_bbox(self) -> None:
        figure = SimpleNamespace(
            region=SimpleNamespace(
                owned_table_bboxes=((10.0, 20.0, 90.0, 80.0),),
            )
        )
        exact_table = SimpleNamespace(
            native_locator=SimpleNamespace(
                bbox=(10.0, 20.0, 90.0, 80.0),
            )
        )
        adjacent_table = SimpleNamespace(
            native_locator=SimpleNamespace(
                bbox=(10.0, 82.0, 90.0, 98.0),
            )
        )

        self.assertTrue(
            pdf_adapter_module._vector_figure_owns_table_candidate(
                table_node=exact_table,
                figure=figure,
            )
        )
        self.assertFalse(
            pdf_adapter_module._vector_figure_owns_table_candidate(
                table_node=adjacent_table,
                figure=figure,
            )
        )

    def test_caption_anchored_quadrilateral_figure_is_uniquely_grounded(
        self,
    ) -> None:
        quadrilateral_bboxes = (
            (40.0, 50.0, 260.0, 130.0),
            (55.0, 60.0, 105.0, 85.0),
            (105.0, 60.0, 155.0, 85.0),
            (55.0, 85.0, 105.0, 110.0),
            (155.0, 60.0, 205.0, 85.0),
            (205.0, 60.0, 245.0, 85.0),
        )
        drawings = tuple(
            _fake_stroked_quadrilateral_drawing(bbox) for bbox in quadrilateral_bboxes
        )
        bboxlog = tuple(
            (
                "stroke-path",
                (bbox[0] - 0.5, bbox[1] - 0.5, bbox[2] + 0.5, bbox[3] + 0.5),
            )
            for bbox in quadrilateral_bboxes
        )
        table_candidates = (
            _fake_quadrilateral_figure_table_candidate(bbox=(60.0, 65.0, 100.0, 100.0)),
            _fake_quadrilateral_figure_table_candidate(
                bbox=(120.0, 65.0, 160.0, 100.0)
            ),
        )

        def find_tables() -> object:
            return SimpleNamespace(tables=table_candidates)

        page = SimpleNamespace(
            get_drawings=lambda: drawings,
            get_bboxlog=lambda: bboxlog,
            find_tables=find_tables,
        )
        raw_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(20.0, 28.0, 280.0, 40.0),
                    text="See Figure 7.",
                ),
                _fake_table_suppression_text_block(
                    bbox=(100.0, 140.0, 200.0, 150.0),
                    text="Figure 7 synthetic layout",
                ),
                {
                    "type": 1,
                    "bbox": (60.0, 65.0, 100.0, 100.0),
                    "image": _PNG_BYTES,
                    "mask": b"synthetic-mask",
                },
                {
                    "type": 1,
                    "bbox": (120.0, 65.0, 160.0, 100.0),
                    "image": _PNG_BYTES,
                },
            ]
        }

        regions = (
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertEqual(region.proof_kind, "caption_anchored_quadrilateral")
        self.assertEqual(region.drawing_count, 6)
        self.assertEqual(region.anchor_count, 1)
        self.assertEqual(
            region.owned_table_bboxes,
            tuple(candidate.bbox for candidate in table_candidates),
        )
        self.assertEqual(region.owned_image_block_indexes, (2, 3))
        self.assertRegex(region.evidence_sha256 or "", r"^[0-9a-f]{64}$")
        self.assertEqual(
            pdf_adapter_module._vector_figure_diagnostic_code(
                proof_kind=region.proof_kind,
                event="materialized",
            ),
            "pdf_caption_anchored_quadrilateral_figure_materialized",
        )
        materialized_figure = SimpleNamespace(region=region)
        self.assertIs(
            pdf_adapter_module._vector_figure_for_image_occurrence(
                [materialized_figure],
                bbox=(60.0, 65.0, 100.0, 100.0),
                block_index=2,
                has_mask=True,
            ),
            materialized_figure,
        )
        self.assertIsNone(
            pdf_adapter_module._vector_figure_for_image_occurrence(
                [materialized_figure],
                bbox=(60.0, 65.0, 100.0, 100.0),
                block_index=4,
                has_mask=True,
            )
        )

    def test_quadrilateral_figure_owns_exact_whole_region_one_by_two_table(
        self,
    ) -> None:
        candidate = _FakeTableCandidate(
            bbox=(40.0, 50.0, 260.0, 130.0),
            row_count=1,
            column_count=2,
            marker="diagram",
        )
        page = SimpleNamespace(find_tables=lambda: SimpleNamespace(tables=(candidate,)))

        owned = pdf_adapter_module._quadrilateral_figure_owned_table_bboxes(
            page=page,
            component_bbox=(40.0, 50.0, 260.0, 130.0),
            rotation_matrix=None,
            resource_budget=pdf_adapter_module._PdfResourceBudget(),
        )

        self.assertEqual(owned, (candidate.bbox,))
        adjacent_candidate = _FakeTableCandidate(
            bbox=(40.0, 132.0, 260.0, 180.0),
            row_count=1,
            column_count=2,
            marker="real",
        )
        adjacent_page = SimpleNamespace(
            find_tables=lambda: SimpleNamespace(tables=(adjacent_candidate,))
        )
        self.assertEqual(
            pdf_adapter_module._quadrilateral_figure_owned_table_bboxes(
                page=adjacent_page,
                component_bbox=(40.0, 50.0, 260.0, 130.0),
                rotation_matrix=None,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            ),
            (),
        )

    def test_quadrilateral_figure_rejects_horizontally_disjoint_reference(
        self,
    ) -> None:
        page, raw_page = _fake_quadrilateral_figure_inputs(
            reference_bbox=(400.0, 28.0, 520.0, 40.0),
        )

        regions = (
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 600.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertEqual(regions, ())

    def test_quadrilateral_figure_requires_body_reference_semantics(self) -> None:
        page, raw_page = _fake_quadrilateral_figure_inputs(
            reference_text="Figure 7 unrelated heading",
        )

        regions = (
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertEqual(regions, ())

    def test_quadrilateral_figure_evidence_binds_reference_geometry(self) -> None:
        page, first_raw_page = _fake_quadrilateral_figure_inputs()
        _, shifted_raw_page = _fake_quadrilateral_figure_inputs(
            reference_bbox=(21.0, 28.0, 280.0, 40.0),
        )

        def regions(raw_page: object) -> object:
            return pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

        first = regions(first_raw_page)
        shifted = regions(shifted_raw_page)

        self.assertEqual(len(first), 1)
        self.assertEqual(len(shifted), 1)
        self.assertNotEqual(
            first[0].evidence_sha256,
            shifted[0].evidence_sha256,
        )

    def test_quadrilateral_bboxlog_work_is_bounded(self) -> None:
        page, raw_page = _fake_quadrilateral_figure_inputs()

        with (
            mock.patch.object(
                pdf_adapter_module,
                "_MAX_QUADRILATERAL_BBOXLOG_MATCH_CHECKS",
                1,
            ),
            self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded),
        ):
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

    def test_quadrilateral_bboxlog_entry_count_is_bounded(self) -> None:
        page, raw_page = _fake_quadrilateral_figure_inputs()

        with (
            mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_BBOXLOG_ENTRIES_PER_PAGE",
                5,
            ),
            self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded),
        ):
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

    def test_quadrilateral_owned_table_with_independent_caption_is_preserved(
        self,
    ) -> None:
        candidate = _FakeTableCandidate(
            bbox=(40.0, 50.0, 260.0, 130.0),
            row_count=1,
            column_count=2,
            marker="real-table",
        )
        page = SimpleNamespace(find_tables=lambda: SimpleNamespace(tables=(candidate,)))
        raw_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(80.0, 48.0, 220.0, 58.0),
                    text="Table 3 real measurements",
                )
            ]
        }

        owned = pdf_adapter_module._quadrilateral_figure_owned_table_bboxes(
            page=page,
            raw_page=raw_page,
            component_bbox=(40.0, 50.0, 260.0, 130.0),
            rotation_matrix=None,
            resource_budget=pdf_adapter_module._PdfResourceBudget(),
        )

        self.assertEqual(owned, ())

    def test_caption_anchored_quadrilateral_figure_fails_closed(
        self,
    ) -> None:
        quadrilateral_bboxes = (
            (40.0, 50.0, 260.0, 130.0),
            (55.0, 60.0, 105.0, 85.0),
            (105.0, 60.0, 155.0, 85.0),
            (55.0, 85.0, 105.0, 110.0),
            (155.0, 60.0, 205.0, 85.0),
            (205.0, 60.0, 245.0, 85.0),
        )
        drawings = tuple(
            _fake_stroked_quadrilateral_drawing(bbox) for bbox in quadrilateral_bboxes
        )
        bboxlog = tuple(("stroke-path", bbox) for bbox in quadrilateral_bboxes)
        page = SimpleNamespace(
            get_drawings=lambda: drawings,
            get_bboxlog=lambda: bboxlog,
            find_tables=lambda: SimpleNamespace(tables=()),
        )
        matching_raw_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(20.0, 28.0, 280.0, 40.0),
                    text="See Figure 7.",
                ),
                _fake_table_suppression_text_block(
                    bbox=(100.0, 140.0, 200.0, 150.0),
                    text="Figure 7 synthetic layout",
                ),
            ]
        }
        mismatched_raw_page = {
            "blocks": [
                matching_raw_page["blocks"][0],
                _fake_table_suppression_text_block(
                    bbox=(100.0, 140.0, 200.0, 150.0),
                    text="Figure 8 synthetic layout",
                ),
            ]
        }

        for raw_page, curve_seeded_regions in (
            (mismatched_raw_page, ()),
            (
                matching_raw_page,
                (
                    pdf_adapter_module._VectorFigureRegion(
                        bbox=(30.0, 45.0, 270.0, 135.0),
                        drawing_count=1,
                        anchor_count=1,
                        anchor_bboxes=((30.0, 45.0, 270.0, 135.0),),
                    ),
                ),
            ),
        ):
            with self.subTest(
                raw_page=raw_page,
                curve_seeded_regions=curve_seeded_regions,
            ):
                self.assertEqual(
                    pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                        page=page,
                        raw_page=raw_page,
                        page_number=1,
                        page_bbox=(0.0, 0.0, 300.0, 200.0),
                        rotation_matrix=None,
                        curve_seeded_regions=curve_seeded_regions,
                        resource_budget=pdf_adapter_module._PdfResourceBudget(),
                    ),
                    (),
                )

        ambiguous_bboxlog_page = SimpleNamespace(
            get_drawings=lambda: drawings,
            get_bboxlog=lambda: (
                bboxlog[0],
                bboxlog[0],
                *bboxlog[1:],
            ),
            find_tables=lambda: SimpleNamespace(tables=()),
        )
        self.assertEqual(
            pdf_adapter_module._caption_anchored_quadrilateral_figure_regions_from_page(
                page=ambiguous_bboxlog_page,
                raw_page=matching_raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 300.0, 200.0),
                rotation_matrix=None,
                curve_seeded_regions=(),
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            ),
            (),
        )

    def test_caption_anchored_connector_diagram_is_uniquely_grounded(
        self,
    ) -> None:
        left_bitfield = _fake_connector_bitfield_candidate(
            bbox=(80.0, 60.0, 160.0, 100.0),
            marker="left",
        )
        right_bitfield = _fake_connector_bitfield_candidate(
            bbox=(164.0, 60.0, 244.0, 100.0),
            marker="right",
        )
        later_real_table = _FakeTableCandidate(
            bbox=(40.0, 175.0, 280.0, 210.0),
            row_count=5,
            column_count=4,
            marker="real",
        )
        page, raw_page = _fake_caption_anchored_connector_diagram_inputs(
            pair_count=4,
            adjacent_text_blocks=(
                _fake_table_suppression_text_block(
                    bbox=(264.0, 78.0, 276.0, 90.0),
                    text="ECU",
                ),
            ),
            table_candidates=(
                left_bitfield,
                right_bitfield,
                later_real_table,
            ),
        )

        regions = (
            pdf_adapter_module._caption_anchored_connector_diagram_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 320.0, 220.0),
                rotation_matrix=None,
                excluded_regions=(),
                source_sha256="a" * 64,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertEqual(len(regions), 1)
        region = regions[0]
        self.assertEqual(
            region.proof_kind,
            "caption_anchored_connector_diagram",
        )
        self.assertEqual(region.anchor_count, 4)
        self.assertGreaterEqual(region.drawing_count, 8)
        self.assertEqual(
            region.owned_table_bboxes,
            (left_bitfield.bbox, right_bitfield.bbox),
        )
        self.assertGreaterEqual(region.bbox[2], 276.0)
        self.assertRegex(region.evidence_sha256 or "", r"^[0-9a-f]{64}$")
        self.assertEqual(
            pdf_adapter_module._vector_figure_diagnostic_code(
                proof_kind=region.proof_kind,
                event="materialized",
            ),
            "pdf_caption_anchored_connector_diagram_materialized",
        )
        figure = SimpleNamespace(region=region)
        for candidate in (left_bitfield, right_bitfield):
            self.assertTrue(
                pdf_adapter_module._vector_figure_owns_table_candidate(
                    table_node=SimpleNamespace(
                        native_locator=SimpleNamespace(bbox=candidate.bbox),
                    ),
                    figure=figure,
                )
            )
        self.assertFalse(
            pdf_adapter_module._vector_figure_owns_table_candidate(
                table_node=SimpleNamespace(
                    native_locator=SimpleNamespace(
                        bbox=later_real_table.bbox,
                    ),
                ),
                figure=figure,
            )
        )
        self.assertIsNone(
            pdf_adapter_module._vector_figure_for_image_occurrence(
                [figure],
                bbox=(90.0, 70.0, 110.0, 90.0),
                block_index=3,
                has_mask=False,
            )
        )

    def test_connector_diagram_owns_only_continuous_equal_bitfield_pair(
        self,
    ) -> None:
        left = _fake_connector_bitfield_candidate(
            bbox=(80.0, 60.0, 160.0, 100.0),
            marker="left",
        )
        right = _fake_connector_bitfield_candidate(
            bbox=(164.0, 60.0, 244.0, 100.0),
            marker="right",
        )
        later_real_table = _FakeTableCandidate(
            bbox=(40.0, 175.0, 280.0, 210.0),
            row_count=5,
            column_count=4,
            marker="real",
        )

        def owned(
            candidates: tuple[object, ...],
            *,
            raw_page: object = None,
        ) -> tuple[tuple[float, float, float, float], ...]:
            page = SimpleNamespace(
                find_tables=lambda: SimpleNamespace(tables=candidates),
            )
            return pdf_adapter_module._connector_diagram_owned_bitfield_table_bboxes(
                page=page,
                raw_page=raw_page,
                component_bbox=(40.0, 40.0, 340.0, 140.0),
                rotation_matrix=None,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )

        self.assertEqual(
            owned((left, right, later_real_table)),
            (left.bbox, right.bbox),
        )
        uneven = _fake_connector_bitfield_candidate(
            bbox=(164.0, 60.0, 244.0, 100.0),
            marker="uneven",
            column_widths=(15.0, 15.0, 15.0, 35.0),
        )
        vertically_shifted = _fake_connector_bitfield_candidate(
            bbox=(164.0, 62.0, 244.0, 102.0),
            marker="shifted",
        )
        discontinuous = _fake_connector_bitfield_candidate(
            bbox=(190.0, 60.0, 270.0, 100.0),
            marker="far",
        )
        third = _fake_connector_bitfield_candidate(
            bbox=(248.0, 60.0, 328.0, 100.0),
            marker="third",
        )
        table_caption_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(95.0, 48.0, 225.0, 58.0),
                    text="Table 3 independent data",
                )
            ]
        }
        for candidates, raw_page in (
            ((left, uneven), None),
            ((left, vertically_shifted), None),
            ((left, discontinuous), None),
            ((left, right, third), None),
            ((left, right), table_caption_page),
        ):
            with self.subTest(candidates=candidates, raw_page=raw_page):
                self.assertEqual(
                    owned(candidates, raw_page=raw_page),
                    (),
                )

    def test_caption_anchored_connector_diagram_fails_closed(
        self,
    ) -> None:
        page, matching_raw_page = _fake_caption_anchored_connector_diagram_inputs(
            pair_count=3
        )
        _unused_page, missing_caption_raw_page = (
            _fake_caption_anchored_connector_diagram_inputs(
                pair_count=3,
                caption_text=None,
            )
        )
        _unused_page, table_caption_raw_page = (
            _fake_caption_anchored_connector_diagram_inputs(
                pair_count=3,
                extra_text_blocks=(
                    _fake_table_suppression_text_block(
                        bbox=(80.0, 120.0, 220.0, 132.0),
                        text="Table 4 independent data",
                    ),
                ),
            )
        )
        insufficient_page, _unused_raw_page = (
            _fake_caption_anchored_connector_diagram_inputs(pair_count=2)
        )
        excluded = (
            pdf_adapter_module._VectorFigureRegion(
                bbox=(35.0, 15.0, 265.0, 135.0),
                drawing_count=1,
                anchor_count=1,
                anchor_bboxes=((35.0, 15.0, 265.0, 135.0),),
            ),
        )

        cases = (
            (insufficient_page, matching_raw_page, ()),
            (page, missing_caption_raw_page, ()),
            (page, table_caption_raw_page, ()),
            (page, matching_raw_page, excluded),
        )
        for candidate_page, raw_page, excluded_regions in cases:
            with self.subTest(
                candidate_page=candidate_page,
                raw_page=raw_page,
                excluded_regions=excluded_regions,
            ):
                self.assertEqual(
                    pdf_adapter_module._caption_anchored_connector_diagram_regions_from_page(
                        page=candidate_page,
                        raw_page=raw_page,
                        page_number=1,
                        page_bbox=(0.0, 0.0, 320.0, 220.0),
                        rotation_matrix=None,
                        excluded_regions=excluded_regions,
                        source_sha256="a" * 64,
                        resource_budget=pdf_adapter_module._PdfResourceBudget(),
                    ),
                    (),
                )

    def test_caption_anchored_connector_diagram_rejects_ambiguity(
        self,
    ) -> None:
        left_drawings = _fake_connector_diagram_drawings(
            pair_count=3,
            x_offset=0.0,
        )
        right_drawings = _fake_connector_diagram_drawings(
            pair_count=3,
            x_offset=320.0,
        )
        page = SimpleNamespace(
            get_drawings=lambda: (*left_drawings, *right_drawings),
            find_tables=lambda: SimpleNamespace(tables=()),
        )
        raw_page = {
            "blocks": [
                _fake_table_suppression_text_block(
                    bbox=(95.0, 140.0, 205.0, 150.0),
                    text="Figure 7 left diagram",
                ),
                _fake_table_suppression_text_block(
                    bbox=(415.0, 140.0, 525.0, 150.0),
                    text="Figure 8 right diagram",
                ),
            ]
        }

        regions = (
            pdf_adapter_module._caption_anchored_connector_diagram_regions_from_page(
                page=page,
                raw_page=raw_page,
                page_number=1,
                page_bbox=(0.0, 0.0, 640.0, 220.0),
                rotation_matrix=None,
                excluded_regions=(),
                source_sha256="a" * 64,
                resource_budget=pdf_adapter_module._PdfResourceBudget(),
            )
        )

        self.assertEqual(regions, ())

    def test_caption_anchored_connector_diagram_work_is_bounded(
        self,
    ) -> None:
        page, raw_page = _fake_caption_anchored_connector_diagram_inputs(
            pair_count=3,
        )

        for limit_name in (
            "_MAX_CONNECTOR_DIAGRAM_ENDPOINT_CHECKS",
            "_MAX_CONNECTOR_DIAGRAM_ADJACENCY_CHECKS",
        ):
            with (
                self.subTest(limit_name=limit_name),
                mock.patch.object(pdf_adapter_module, limit_name, 0),
                self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded),
            ):
                pdf_adapter_module._caption_anchored_connector_diagram_regions_from_page(
                    page=page,
                    raw_page=raw_page,
                    page_number=1,
                    page_bbox=(0.0, 0.0, 320.0, 220.0),
                    rotation_matrix=None,
                    excluded_regions=(),
                    source_sha256="a" * 64,
                    resource_budget=pdf_adapter_module._PdfResourceBudget(),
                )

    def test_fragmented_image_mosaic_folds_to_one_bound_figure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "fragmented-image-mosaic.pdf"
            _write_pdf_with_fragmented_image_mosaic(source)
            before = _relative_inventory(root)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            figures = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.FIGURE
            )
            images = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(len(figures), 1)
            self.assertEqual(images, ())
            figure = figures[0]
            self.assertEqual(figure.visual_locator, figure.native_locator)
            assert figure.native_locator.bbox is not None
            self.assertLessEqual(figure.native_locator.bbox[0], 48.0)
            self.assertLessEqual(figure.native_locator.bbox[1], 78.0)
            self.assertGreaterEqual(figure.native_locator.bbox[2], 100.0)
            self.assertGreaterEqual(figure.native_locator.bbox[3], 107.0)
            asset = next(
                item for item in snapshot.assets if item.asset_id == figure.asset_ref
            )
            self.assertIs(asset.kind, SourceAssetKind.FIGURE)
            self.assertEqual(asset.media_type, "image/png")
            folded = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_fragmented_image_mosaic_occurrence_folded"
            )
            self.assertEqual(len(folded), 250)
            self.assertTrue(
                all(
                    diagnostic.source_node_id == figure.source_node_id
                    and diagnostic.native_locator is not None
                    and diagnostic.native_locator.block_index is not None
                    and diagnostic.evidence_sha256 is not None
                    for diagnostic in folded
                )
            )
            materialized = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_fragmented_image_mosaic_materialized"
            )
            self.assertEqual(len(materialized), 1)
            self.assertEqual(
                materialized[0].evidence_sha256,
                folded[0].evidence_sha256,
            )
            self.assertEqual(_relative_inventory(root), before)

    def test_ambiguous_fragmented_image_mosaic_preserves_original_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ambiguous-fragmented-image-mosaic.pdf"
            _write_pdf_with_fragmented_image_mosaic(
                source,
                include_oversized_strip=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertFalse(
                any(node.kind is SourceNodeKind.FIGURE for node in snapshot.nodes)
            )
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                251,
            )
            self.assertNotIn(
                "pdf_fragmented_image_mosaic_occurrence_folded",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )

    def test_fragmented_image_mosaic_adjacency_budget_preserves_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bounded-fragmented-image-mosaic.pdf"
            _write_pdf_with_fragmented_image_mosaic(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_IMAGE_MOSAIC_ADJACENCY_CHECKS",
                0,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertFalse(
                any(node.kind is SourceNodeKind.FIGURE for node in snapshot.nodes)
            )
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                250,
            )

    def test_curve_seeded_compound_vector_figure_preserves_masked_image(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "compound-vector-masked-image.pdf"
            _write_pdf_with_compound_vector_figure(
                source,
                include_embedded_fragment=False,
                include_masked_fragment=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            figures = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.FIGURE
            )
            images = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(len(figures), 1)
            self.assertEqual(len(images), 1)
            self.assertIsNotNone(images[0].asset_derivation)
            self.assertNotIn(
                "pdf_vector_figure_image_occurrence_folded",
                {
                    diagnostic.code
                    for diagnostic in snapshot.diagnostics
                    if diagnostic.native_locator == images[0].native_locator
                },
            )
            self.assertEqual(
                sum(
                    diagnostic.code == "pdf_image_mask_materialized"
                    for diagnostic in snapshot.diagnostics
                ),
                1,
            )

    def test_rotated_curve_seeded_figure_uses_visible_page_coordinates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "rotated-vector-figure.pdf"
            _write_pdf_with_rotated_vector_figure(source)

            with pymupdf.open(source) as document:
                page = document[0]
                drawing = next(
                    item
                    for item in page.get_drawings()
                    if any(operation[0] == "c" for operation in item.get("items", ()))
                )
                expected = pymupdf.Rect(drawing["rect"]) * page.rotation_matrix
                visible_page = pymupdf.Rect(page.rect)
                padding = pdf_adapter_module._VECTOR_FIGURE_PADDING_POINTS
                expected = pymupdf.Rect(
                    max(visible_page.x0, expected.x0 - padding),
                    max(visible_page.y0, expected.y0 - padding),
                    min(visible_page.x1, expected.x1 + padding),
                    min(visible_page.y1, expected.y1 + padding),
                )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            figure = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.FIGURE
            )
            assert figure.native_locator.bbox is not None
            for actual, expected_value in zip(
                figure.native_locator.bbox,
                tuple(expected),
                strict=True,
            ):
                self.assertAlmostEqual(actual, expected_value, places=3)
            self.assertEqual(figure.visual_locator, figure.native_locator)
            self.assertGreaterEqual(figure.native_locator.bbox[0], visible_page.x0)
            self.assertGreaterEqual(figure.native_locator.bbox[1], visible_page.y0)
            self.assertLessEqual(figure.native_locator.bbox[2], visible_page.x1)
            self.assertLessEqual(figure.native_locator.bbox[3], visible_page.y1)

    def test_ruled_table_paths_do_not_become_vector_figures(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ruled-table-not-figure.pdf"
            _write_pdf_with_bordered_table(source)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertTrue(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertFalse(
                any(node.kind is SourceNodeKind.FIGURE for node in snapshot.nodes)
            )

    def test_stroked_cell_rectangles_with_cell_ellipse_keep_real_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stroked-grid-with-cell-ellipse.pdf"
            _write_pdf_with_stroked_cell_rectangles_and_ellipse(source)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertTrue(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertFalse(
                any(
                    diagnostic.code == "pdf_vector_figure_table_candidate_folded"
                    for diagnostic in snapshot.diagnostics
                )
            )
            figures = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.FIGURE
            )
            self.assertEqual(len(figures), 1)
            assert figures[0].native_locator.bbox is not None
            self.assertLess(
                pdf_adapter_module._bbox_area(figures[0].native_locator.bbox),
                0.50 * 320 * 240,
            )

    def test_vector_figure_final_union_cannot_exceed_page_area_limit(
        self,
    ) -> None:
        drawings = [
            _fake_vector_drawing((10.0, 10.0, 190.0, 180.0), operation="c"),
            _fake_vector_drawing((180.0, 170.0, 220.0, 230.0), operation="l"),
            _fake_vector_drawing((210.0, 220.0, 390.0, 390.0), operation="c"),
        ]
        page = SimpleNamespace(get_drawings=lambda: drawings)

        regions = pdf_adapter_module._vector_figure_regions_from_page(
            page=page,
            page_bbox=(0.0, 0.0, 400.0, 400.0),
            rotation_matrix=None,
        )

        self.assertEqual(regions, ())

    def test_zero_extent_stroked_connector_bridges_vector_components(
        self,
    ) -> None:
        page = SimpleNamespace(
            get_drawings=lambda: [
                _fake_vector_drawing(
                    (20.0, 80.0, 100.0, 120.0),
                    operation="c",
                ),
                {
                    **_fake_vector_drawing(
                        (100.0, 100.0, 180.0, 100.0),
                        operation="l",
                    ),
                    "width": 1.0,
                },
                _fake_vector_drawing(
                    (180.0, 80.0, 260.0, 120.0),
                    operation="c",
                ),
            ]
        )

        regions = pdf_adapter_module._vector_figure_regions_from_page(
            page=page,
            page_bbox=(0.0, 0.0, 300.0, 200.0),
            rotation_matrix=None,
        )

        self.assertEqual(len(regions), 1)
        self.assertLessEqual(regions[0].bbox[0], 18.0)
        self.assertGreaterEqual(regions[0].bbox[2], 262.0)

    def test_vector_figure_header_center_is_rejected(self) -> None:
        page = SimpleNamespace(
            get_drawings=lambda: [
                _fake_vector_drawing(
                    (20.0, 0.0, 180.0, 25.5),
                    operation="c",
                )
            ]
        )

        regions = pdf_adapter_module._vector_figure_regions_from_page(
            page=page,
            page_bbox=(0.0, 0.0, 200.0, 200.0),
            rotation_matrix=None,
        )

        self.assertEqual(regions, ())

    def test_vector_figure_adjacency_work_is_bounded(self) -> None:
        drawings = [
            _fake_vector_drawing(
                (10.0, float(index * 50), 60.0, float(index * 50 + 20)),
                operation="c",
            )
            for index in range(1_500)
        ]
        page = SimpleNamespace(get_drawings=lambda: drawings)

        with self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded):
            pdf_adapter_module._vector_figure_regions_from_page(
                page=page,
                page_bbox=(0.0, 0.0, 100.0, 75_000.0),
                rotation_matrix=None,
            )

    def test_vector_figure_drawing_item_work_is_bounded(self) -> None:
        page = SimpleNamespace(
            get_drawings=lambda: [
                {
                    "rect": (20.0, 20.0, 180.0, 120.0),
                    "type": "s",
                    "items": _OversizedDrawingItems(),
                }
            ]
        )

        with self.assertRaises(pdf_adapter_module._PdfResourceBudgetExceeded):
            pdf_adapter_module._vector_figure_regions_from_page(
                page=page,
                page_bbox=(0.0, 0.0, 200.0, 200.0),
                rotation_matrix=None,
            )

    def test_folded_figure_table_is_reindexed_before_following_page(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "folded-table-with-following-page.pdf"
            _write_pdf_with_compound_vector_figure(
                source,
                include_embedded_fragment=False,
                include_table_candidate=True,
                include_following_page=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            folded_table = next(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_vector_figure_table_candidate_folded"
            )
            self.assertIsNone(folded_table.source_node_id)
            self.assertIsNotNone(folded_table.native_locator)
            assert folded_table.native_locator is not None
            self.assertIsNotNone(folded_table.native_locator.table_index)
            self.assertTrue(
                any(
                    node.native_locator.page_number == 2
                    and node.raw_text == "FOLLOWING PAGE"
                    for node in snapshot.nodes
                )
            )
            self.assertEqual(
                [node.source_order for node in snapshot.nodes],
                list(range(len(snapshot.nodes))),
            )

    def test_folded_figure_table_diagnostic_does_not_leave_stale_partial(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "folded-ambiguous-table.pdf"
            _write_pdf_with_compound_vector_figure(
                source,
                include_embedded_fragment=False,
                include_table_candidate=True,
            )
            original_table_builder = pdf_adapter_module._table_node_from_candidate

            def inject_folded_warning(**kwargs: object) -> object:
                node, diagnostics, _ = original_table_builder(**kwargs)
                if node is None:
                    return node, diagnostics, True
                warning = pdf_adapter_module._diagnostic(
                    "pdf_table_merge_geometry_unresolved",
                    "Synthetic folded-candidate warning.",
                    source_node_id=node.source_node_id,
                    native_locator=node.native_locator,
                )
                return node, [*diagnostics, warning], True

            with mock.patch.object(
                pdf_adapter_module,
                "_table_node_from_candidate",
                side_effect=inject_folded_warning,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertNotIn(
                "pdf_table_merge_geometry_unresolved",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )
            self.assertIn(
                "pdf_vector_figure_table_candidate_folded",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )

    def test_sparse_page_middle_summary_and_numbers_remain_paragraphs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sparse-middle.pdf"
            _write_pdf_with_repeated_native_text(
                source,
                texts=tuple(
                    f"Stage summary\n{page_number}" for page_number in range(1, 5)
                ),
                baseline_y=300,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            summaries = tuple(
                node
                for node in snapshot.nodes
                if node.raw_text.startswith("Stage summary\n")
            )
            self.assertEqual(len(summaries), 4)
            self.assertTrue(
                all(node.kind is SourceNodeKind.PARAGRAPH for node in summaries)
            )

    def test_noncontinuous_printed_numbers_do_not_become_furniture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "noncontinuous-footer.pdf"
            printed_numbers = (1, 3, 5, 7)
            _write_pdf_with_repeated_native_text(
                source,
                texts=tuple(
                    f"Page {number} of 7\nDOC-CODE" for number in printed_numbers
                ),
                baseline_y=555,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            candidates = tuple(
                node for node in snapshot.nodes if "DOC-CODE" in node.raw_text
            )
            self.assertEqual(len(candidates), 4)
            self.assertTrue(
                all(node.kind is SourceNodeKind.PARAGRAPH for node in candidates)
            )

    def test_extracts_raw_blocks_bbox_and_textless_embedded_image(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "native.pdf"
            _write_pdf_with_text_and_image(source)
            before = _relative_inventory(root)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(snapshot.created_at, CREATED_AT)
            self.assertEqual(len(snapshot.nodes), 3)
            text_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.PARAGRAPH
            )
            image_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(
                tuple(node.raw_text for node in text_nodes),
                ("raw   text", "raw   text"),
            )
            self.assertTrue(
                all(node.native_locator.bbox is not None for node in snapshot.nodes)
            )
            self.assertTrue(
                all(node.visual_locator is not None for node in snapshot.nodes)
            )
            self.assertEqual(len(image_nodes), 1)
            self.assertFalse(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertEqual(image_nodes[0].raw_text, "")
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(image_nodes[0].asset_ref, snapshot.assets[0].asset_id)

            with pymupdf.open(source) as document:
                native_image = document[1].get_text("rawdict", sort=False)["blocks"][0][
                    "image"
                ]
            self.assertEqual(
                snapshot.assets[0].content_sha256,
                hashlib.sha256(native_image).hexdigest(),
            )
            storage_key = snapshot.assets[0].storage_key
            self.assertFalse(storage_key.startswith("/"))
            self.assertNotIn("..", Path(storage_key).parts)
            self.assertEqual(_relative_inventory(root), before)

    def test_ignores_whitespace_only_text_blocks_without_rewriting_real_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_bordered_table(source)
            preflight = LocalDocumentPreflight().inspect(source)
            whitespace_with_line_break = {
                "type": 0,
                "bbox": (2.0, 20.0, 18.0, 28.0),
                "lines": [
                    {"spans": [{"alpha": 255, "chars": [{"c": " "}]}]},
                    {
                        "spans": [
                            {"alpha": 255, "chars": [{"c": " "}, {"c": " "}]}
                        ]
                    },
                ],
            }
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(),
                strict_candidates=(),
                raw_blocks=(
                    _fake_table_suppression_text_block(
                        bbox=(2.0, 2.0, 18.0, 8.0),
                        text=" ",
                    ),
                    _fake_table_suppression_text_block(
                        bbox=(2.0, 10.0, 18.0, 16.0),
                        text="\N{NO-BREAK SPACE}",
                    ),
                    whitespace_with_line_break,
                    _fake_table_suppression_text_block(
                        bbox=(20.0, 30.0, 80.0, 40.0),
                        text="raw   text  ",
                    ),
                ),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(
                tuple(node.raw_text for node in snapshot.nodes),
                ("raw   text  ",),
            )
            dispositions = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_whitespace_only_text_blocks_ignored"
            )
            self.assertEqual(len(dispositions), 1)
            self.assertIn("3 native text block(s)", dispositions[0].message)
            self.assertEqual(
                dispositions[0].severity,
                SourceDiagnosticSeverity.INFO,
            )
            self.assertRegex(dispositions[0].evidence_sha256 or "", r"^[0-9a-f]{64}$")

    def test_materializes_alpha_mask_as_png_with_component_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "alpha-mask.pdf"
            _write_pdf_with_alpha_image(source)
            before = _relative_inventory(root)

            with pymupdf.open(source) as document:
                native_image = document[0].get_text("rawdict", sort=False)["blocks"][0]
                base_bytes = bytes(native_image["image"])
                mask_bytes = bytes(native_image["mask"])
                composite_bytes = bytes(
                    pymupdf.Pixmap(
                        pymupdf.Pixmap(base_bytes),
                        pymupdf.Pixmap(mask_bytes),
                    ).tobytes("png")
                )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(len(snapshot.assets), 1)
            asset = snapshot.assets[0]
            self.assertEqual(asset.media_type, "image/png")
            self.assertTrue(asset.storage_key.endswith(".png"))
            self.assertEqual(
                asset.content_sha256,
                hashlib.sha256(composite_bytes).hexdigest(),
            )
            image_node = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertIsNotNone(image_node.asset_derivation)
            assert image_node.asset_derivation is not None
            self.assertIs(
                image_node.asset_derivation.kind,
                SourceAssetDerivationKind.ALPHA_MASK_COMPOSITE,
            )
            self.assertEqual(
                image_node.asset_derivation.base_content_sha256,
                hashlib.sha256(base_bytes).hexdigest(),
            )
            self.assertEqual(
                image_node.asset_derivation.mask_content_sha256,
                hashlib.sha256(mask_bytes).hexdigest(),
            )
            footprint = image_node.asset_derivation.alpha_footprint
            self.assertIsNotNone(footprint)
            assert footprint is not None
            self.assertEqual((footprint.width_px, footprint.height_px), (4, 4))
            self.assertEqual(footprint.nonzero_sample_count, 12)
            self.assertEqual(
                tuple((run.y, run.x_start, run.x_end) for run in footprint.runs),
                ((0, 1, 4), (1, 1, 4), (2, 1, 4), (3, 1, 4)),
            )
            self.assertNotIn(
                "pdf_image_mask_not_materialized",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )
            self.assertEqual(_relative_inventory(root), before)

    def test_alpha_footprint_run_cap_fails_closed_without_losing_composite(
        self,
    ) -> None:
        pixmap = pymupdf.Pixmap(
            pymupdf.csRGB,
            pymupdf.IRect(0, 0, 4, 2),
            True,
        )
        pixmap.clear_with(0x336699)
        pixmap.set_alpha(bytes((255, 0, 255, 0, 0, 255, 0, 255)))

        with mock.patch.object(
            pdf_adapter_module,
            "_MAX_PDF_ALPHA_FOOTPRINT_RUNS",
            3,
        ):
            footprint = pdf_adapter_module._exact_alpha_footprint_from_pixmap(pixmap)

        self.assertIsNone(footprint)

    def test_alpha_footprint_default_run_cap_accepts_8192_and_rejects_8193(
        self,
    ) -> None:
        accepted = pymupdf.Pixmap(
            pymupdf.csRGB,
            pymupdf.IRect(0, 0, 16_384, 1),
            True,
        )
        accepted.clear_with(0x336699)
        accepted.set_alpha(bytes((255, 0)) * 8_192)

        accepted_footprint = pdf_adapter_module._exact_alpha_footprint_from_pixmap(
            accepted
        )

        self.assertIsNotNone(accepted_footprint)
        assert accepted_footprint is not None
        self.assertEqual(len(accepted_footprint.runs), 8_192)

        rejected = pymupdf.Pixmap(
            pymupdf.csRGB,
            pymupdf.IRect(0, 0, 16_385, 1),
            True,
        )
        rejected.clear_with(0x336699)
        rejected.set_alpha(bytes((255, 0)) * 8_192 + bytes((255,)))

        self.assertIsNone(
            pdf_adapter_module._exact_alpha_footprint_from_pixmap(rejected)
        )

    def test_proven_inline_header_logo_fragments_are_not_standalone_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "inline-header-logo.pdf"
            _write_pdf_with_inline_header_fragments(source, exact_wrappers=True)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertFalse(
                any(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes)
            )
            diagnostics = tuple(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_inline_header_logo_fragment_excluded"
            )
            self.assertEqual(len(diagnostics), 128)
            self.assertEqual(
                len({item.evidence_sha256 for item in diagnostics}),
                1,
            )

    def test_inline_header_fragment_wrapper_ambiguity_preserves_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ambiguous-inline-header.pdf"
            _write_pdf_with_inline_header_fragments(source, exact_wrappers=False)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                128,
            )
            self.assertNotIn(
                "pdf_inline_header_logo_fragment_excluded",
                {item.code for item in snapshot.diagnostics},
            )

    def test_materializes_source_bound_zero_matte_uniform_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "zero-matte-transparent.pdf"
            _write_pdf_with_matte_soft_mask(source, mask_values=(0,) * 16)
            transparent = pymupdf.Pixmap(
                pymupdf.csRGB,
                pymupdf.IRect(0, 0, 4, 4),
                True,
            )
            transparent.clear_with()
            expected_bytes = bytes(transparent.tobytes("png"))

            with mock.patch.object(
                pdf_adapter_module,
                "_isolated_pdf_image_xobject_pixmap",
                wraps=pdf_adapter_module._isolated_pdf_image_xobject_pixmap,
            ) as isolated_render:
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(isolated_render.call_count, 0)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                snapshot.assets[0].content_sha256,
                hashlib.sha256(expected_bytes).hexdigest(),
            )
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_image_mask_materialized",),
            )
            self.assertIsNotNone(snapshot.diagnostics[0].evidence_sha256)

    def test_materializes_source_bound_zero_matte_variable_mask(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "zero-matte-variable.pdf"
            mask_values = (0, 64, 128, 255) * 4
            base_xref = _write_pdf_with_matte_soft_mask(
                source,
                mask_values=mask_values,
            )
            with pymupdf.open(source) as document:
                isolated_page = document.new_page(width=4, height=4)
                isolated_page.insert_image(isolated_page.rect, xref=base_xref)
                expected_bytes = bytes(
                    isolated_page.get_pixmap(alpha=True, annots=False).tobytes("png")
                )

            with mock.patch.object(
                pdf_adapter_module,
                "_isolated_pdf_image_xobject_pixmap",
                wraps=pdf_adapter_module._isolated_pdf_image_xobject_pixmap,
            ) as isolated_render:
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(isolated_render.call_count, 1)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                snapshot.assets[0].content_sha256,
                hashlib.sha256(expected_bytes).hexdigest(),
            )
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_image_mask_materialized",),
            )
            self.assertIsNotNone(snapshot.diagnostics[0].evidence_sha256)

    def test_nonzero_matte_alpha_mask_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "nonzero-matte.pdf"
            _write_pdf_with_matte_soft_mask(
                source,
                mask_values=(0, 64, 128, 255) * 4,
                matte_values=(1, 0, 0),
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(snapshot.assets), 1)
            image_node = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertIsNone(image_node.asset_derivation)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_image_mask_base_alpha_ambiguous",),
            )

    def test_alpha_mask_proof_checks_xobject_samples_beyond_info_digest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source-bound.pdf"
            base_xref = _write_pdf_with_matte_soft_mask(
                source,
                mask_values=(0, 64, 128, 255) * 4,
            )
            source_bytes = source.read_bytes()
            with pymupdf.open(stream=source_bytes, filetype="pdf") as document:
                page = document[0]
                block = dict(page.get_text("rawdict", sort=False)["blocks"][0])
                tampered = pymupdf.Pixmap(
                    pymupdf.csRGB,
                    pymupdf.IRect(0, 0, 4, 4),
                    True,
                )
                tampered.clear_with(0xCC9966)
                tampered.set_alpha(bytes([255] * 16))
                tampered_bytes = bytes(tampered.tobytes("png"))
                tampered_digest = hashlib.md5(
                    pymupdf.Pixmap(tampered_bytes).samples,
                    usedforsecurity=False,
                ).digest()
                image_infos = tuple(
                    {
                        **info,
                        "digest": tampered_digest,
                    }
                    if info.get("xref") == base_xref
                    else info
                    for info in page.get_image_info(hashes=True, xrefs=True)
                )
                block["image"] = tampered_bytes
                mask_bytes = bytes(block["mask"])
                mask_pixmap = pymupdf.Pixmap(mask_bytes)

                proof = pdf_adapter_module._proven_pdf_alpha_mask_source(
                    document=document,
                    block=block,
                    image_infos=image_infos,
                    image_bytes=tampered_bytes,
                    mask_bytes=mask_bytes,
                    expected_width=4,
                    expected_height=4,
                    expected_mask_width=mask_pixmap.width,
                    expected_mask_height=mask_pixmap.height,
                    resource_budget=pdf_adapter_module._PdfResourceBudget(),
                )

            self.assertIsNone(proof)

    def test_unproven_base_alpha_and_mask_relationship_remains_partial(self) -> None:
        mask_bytes = _png_bytes_from_pixels("L", [0, 64, 128, 255])
        for alpha_values in ([10, 20, 30, 40], [255, 255, 255, 255]):
            with self.subTest(alpha_values=alpha_values):
                base_bytes = _png_bytes_from_pixels(
                    "RGBA",
                    [(51, 102, 153, alpha) for alpha in alpha_values],
                )

                with self.assertRaisesRegex(
                    pdf_adapter_module._ImageMaskMaterializationRejected,
                    "pdf_image_mask_base_alpha_ambiguous",
                ):
                    pdf_adapter_module._materialize_alpha_mask_composite(
                        image_bytes=base_bytes,
                        mask_bytes=mask_bytes,
                        expected_width=2,
                        expected_height=2,
                        expected_mask_width=2,
                        expected_mask_height=2,
                    )

    def test_mask_materialization_failure_preserves_primary_asset_as_partial(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "alpha-mask.pdf"
            _write_pdf_with_alpha_image(source)
            with pymupdf.open(source) as document:
                native_image = document[0].get_text("rawdict", sort=False)["blocks"][0][
                    "image"
                ]

            with mock.patch.object(
                pdf_adapter_module,
                "_materialize_alpha_mask_composite",
                side_effect=ValueError("invalid mask"),
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                snapshot.assets[0].content_sha256,
                hashlib.sha256(native_image).hexdigest(),
            )
            image_node = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertIsNone(image_node.asset_derivation)
            diagnostics = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_image_mask_materialization_failed"
            )
            self.assertEqual(len(diagnostics), 1)
            self.assertIsNotNone(diagnostics[0].source_node_id)

    def test_repeated_image_mask_pair_composes_once_and_shares_asset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "repeated-alpha-mask.pdf"
            _write_pdf_with_explicit_masks(source, mask_values=(64, 64))

            with mock.patch.object(
                pdf_adapter_module,
                "_materialize_alpha_mask_composite",
                wraps=pdf_adapter_module._materialize_alpha_mask_composite,
            ) as compose:
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            image_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(compose.call_count, 1)
            self.assertEqual(len(image_nodes), 2)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                {node.asset_ref for node in image_nodes}, {snapshot.assets[0].asset_id}
            )
            self.assertTrue(
                all(node.asset_derivation is not None for node in image_nodes)
            )

    def test_repeated_failed_image_mask_pair_is_attempted_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "repeated-bad-alpha-mask.pdf"
            _write_pdf_with_explicit_masks(source, mask_values=(64, 64))

            with mock.patch.object(
                pdf_adapter_module,
                "_materialize_alpha_mask_composite",
                side_effect=ValueError("invalid mask"),
            ) as compose:
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(compose.call_count, 1)
            self.assertEqual(
                sum(
                    item.code == "pdf_image_mask_materialization_failed"
                    for item in snapshot.diagnostics
                ),
                2,
            )

    def test_same_primary_image_with_different_masks_keeps_distinct_assets(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "different-alpha-masks.pdf"
            _write_pdf_with_explicit_masks(source, mask_values=(64, 192))

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            image_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            derivations = tuple(node.asset_derivation for node in image_nodes)
            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(len(image_nodes), 2)
            self.assertEqual(len(snapshot.assets), 2)
            self.assertEqual(
                len(
                    {
                        item.base_content_sha256
                        for item in derivations
                        if item is not None
                    }
                ),
                1,
            )
            self.assertEqual(
                len(
                    {
                        item.mask_content_sha256
                        for item in derivations
                        if item is not None
                    }
                ),
                2,
            )

    def test_materializes_higher_resolution_explicit_mask_at_mask_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "higher-resolution-mask.pdf"
            _write_pdf_with_higher_resolution_mask(source)

            with pymupdf.open(source) as document:
                source_page = document[0]
                native_image = source_page.get_text("rawdict", sort=False)["blocks"][0]
                base_xref = source_page.get_images(full=True)[0][0]
                base_pixmap = pymupdf.Pixmap(native_image["image"])
                mask_pixmap = pymupdf.Pixmap(native_image["mask"])
                scaled_base = pymupdf.Pixmap(
                    base_pixmap,
                    float(mask_pixmap.width),
                    float(mask_pixmap.height),
                )
                expected_bytes = bytes(
                    pymupdf.Pixmap(scaled_base, mask_pixmap).tobytes("png")
                )
                isolated_page = document.new_page(
                    width=mask_pixmap.width,
                    height=mask_pixmap.height,
                )
                isolated_page.insert_image(isolated_page.rect, xref=base_xref)
                native_render = bytes(
                    isolated_page.get_pixmap(alpha=False).tobytes("png")
                )

            expected_image = PILImage.open(io.BytesIO(expected_bytes)).convert("RGBA")
            flattened = PILImage.new("RGBA", expected_image.size, "white")
            flattened.alpha_composite(expected_image)
            render_image = PILImage.open(io.BytesIO(native_render)).convert("RGBA")
            extrema = ImageChops.difference(flattened, render_image).getextrema()
            self.assertLessEqual(max(channel[1] for channel in extrema), 1)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                snapshot.assets[0].content_sha256,
                hashlib.sha256(expected_bytes).hexdigest(),
            )
            self.assertEqual(snapshot.assets[0].media_type, "image/png")
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_image_mask_materialized",),
            )

    def test_image_mask_composite_work_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "alpha-mask.pdf"
            _write_pdf_with_alpha_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_IMAGE_COMPOSITE_WORK_BYTES",
                1,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_resource_budget_exceeded",),
            )

    def test_image_mask_source_proof_work_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source-bound.pdf"
            _write_pdf_with_matte_soft_mask(
                source,
                mask_values=(0, 64, 128, 255) * 4,
            )

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_IMAGE_MASK_PROOF_WORK_BYTES",
                1,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_resource_budget_exceeded",),
            )

    def test_alpha_mask_isolated_render_count_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source-bound.pdf"
            _write_pdf_with_matte_soft_mask(
                source,
                mask_values=(0, 64, 128, 255) * 4,
            )

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_ALPHA_MASK_ISOLATED_RENDERS",
                0,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_resource_budget_exceeded",),
            )

    def test_image_mask_budget_charges_higher_resolution_mask_before_decode(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "higher-resolution-mask.pdf"
            _write_pdf_with_higher_resolution_mask(source)

            with (
                mock.patch.object(
                    pdf_adapter_module,
                    "_MAX_PDF_IMAGE_COMPOSITE_WORK_BYTES",
                    2_000,
                ),
                mock.patch.object(
                    pdf_adapter_module,
                    "_materialize_alpha_mask_composite",
                    wraps=pdf_adapter_module._materialize_alpha_mask_composite,
                ) as compose,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(compose.call_count, 0)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_resource_budget_exceeded",),
            )

    def test_lower_resolution_mask_preserves_primary_asset_as_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "lower-resolution-mask.pdf"
            _write_pdf_with_lower_resolution_mask(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_materialize_alpha_mask_composite",
                wraps=pdf_adapter_module._materialize_alpha_mask_composite,
            ) as compose:
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(compose.call_count, 0)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_image_mask_lower_resolution_unsupported",),
            )
            image_node = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertIsNone(image_node.asset_derivation)

    def test_image_mask_memory_error_fails_as_resource_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "alpha-mask.pdf"
            _write_pdf_with_alpha_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_materialize_alpha_mask_composite",
                side_effect=MemoryError,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(
                tuple(item.code for item in snapshot.diagnostics),
                ("pdf_resource_budget_exceeded",),
            )

    def test_extracts_bordered_table_as_parallel_table_ir_with_cell_locators(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "table.pdf"
            _write_pdf_with_bordered_table(source)
            before = _relative_inventory(root)

            first = build_source_ir(source, clock=lambda: CREATED_AT)
            second = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(first.status, SourceIRStatus.COMPLETE)
            self.assertEqual(first, second)
            table_nodes = tuple(
                node for node in first.nodes if node.kind is SourceNodeKind.TABLE
            )
            raw_nodes = tuple(
                node for node in first.nodes if node.kind is SourceNodeKind.PARAGRAPH
            )
            self.assertEqual(len(table_nodes), 1)
            self.assertGreater(len(raw_nodes), 0)

            table_node = table_nodes[0]
            table = table_node.table_payload
            assert table is not None
            self.assertEqual(table_node.raw_text, "")
            self.assertEqual(table_node.native_locator.page_number, 1)
            self.assertEqual(table_node.native_locator.table_index, 0)
            self.assertIsNone(table_node.native_locator.block_index)
            self.assertIsNotNone(table_node.native_locator.bbox)
            self.assertEqual((table.row_count, table.column_count), (2, 2))
            self.assertEqual(table.header_regions, ())
            self.assertEqual(table.merged_regions, ())

            cells = {(cell.row, cell.column): cell for cell in table.cells}
            self.assertEqual(
                {position: cell.raw_text for position, cell in cells.items()},
                {
                    (0, 0): "Material",
                    (0, 1): "Threshold",
                    (1, 0): "Steel",
                    (1, 1): "100MPa",
                },
            )
            for (row, column), cell in cells.items():
                with self.subTest(row=row, column=column):
                    locator = cell.native_locator
                    self.assertEqual(locator.page_number, 1)
                    self.assertEqual(locator.table_index, 0)
                    self.assertEqual(locator.row_index, row)
                    self.assertEqual(locator.column_index, column)
                    self.assertIsNone(locator.block_index)
                    self.assertIsNotNone(locator.bbox)
                    self.assertTrue(cell.cell_id.startswith("cell-"))
            self.assertTrue(table.table_id.startswith("table-"))
            self.assertEqual(_relative_inventory(root), before)

    def test_preserves_geometry_proven_pdf_merged_cells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _fake_merged_table_candidate(anchor_right=60.0)
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertNotIn(
                "pdf_table_merge_geometry_unresolved",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )
            table_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(table_nodes), 1)
            table = table_nodes[0].table_payload
            assert table is not None
            self.assertEqual(len(table.cells), 6)
            self.assertEqual(
                tuple(
                    (region.row, region.column, region.row_span, region.column_span)
                    for region in table.merged_regions
                ),
                ((0, 0, 2, 2),),
            )
            anchor = next(
                cell for cell in table.cells if (cell.row, cell.column) == (0, 0)
            )
            self.assertEqual((anchor.row_span, anchor.column_span), (2, 2))
            self.assertEqual(anchor.native_locator.bbox, (0.0, 0.0, 60.0, 40.0))
            self.assertNotIn((0, 1), {(cell.row, cell.column) for cell in table.cells})
            self.assertNotIn((1, 0), {(cell.row, cell.column) for cell in table.cells})

    def test_default_detector_preserves_geometry_proven_single_row_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _FakeTableCandidate(
                bbox=(20.0, 20.0, 80.0, 40.0),
                row_count=1,
                column_count=2,
                marker="default",
            )
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(PDF_SOURCE_IR_ADAPTER_VERSION, "1.13.0")
            table_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(table_nodes), 1)
            table = table_nodes[0].table_payload
            assert table is not None
            self.assertEqual((table.row_count, table.column_count), (1, 2))
            self.assertEqual(
                tuple(cell.raw_text for cell in table.cells),
                ("default-0-0", "default-0-1"),
            )

    def test_single_row_table_selection_rejects_unproven_and_strict_candidates(
        self,
    ) -> None:
        single_cell = _FakeTableCandidate(
            bbox=(20.0, 20.0, 80.0, 40.0),
            row_count=1,
            column_count=1,
            marker="single",
        )
        unproven = _FakeTableCandidate(
            bbox=(20.0, 20.0, 80.0, 40.0),
            row_count=1,
            column_count=2,
            marker="unproven",
        )
        unproven.rows = (SimpleNamespace(cells=((20.0, 20.0, 50.0, 40.0), None)),)
        strict_single_row = _FakeTableCandidate(
            bbox=(20.0, 20.0, 80.0, 40.0),
            row_count=1,
            column_count=2,
            marker="strict",
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(single_cell, unproven),
                strict_candidates=(strict_single_row,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
            )
        )

        self.assertEqual(selected, ())
        self.assertEqual((strict_added, strict_replaced), (0, 0))

    def test_fails_closed_when_pdf_merged_cell_grid_is_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _fake_merged_table_candidate(anchor_right=58.0)
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertIn(
                "pdf_table_merge_geometry_unresolved",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )
            table = next(
                node.table_payload
                for node in snapshot.nodes
                if node.kind is SourceNodeKind.TABLE
            )
            assert table is not None
            self.assertEqual(table.merged_regions, ())
            self.assertEqual(len(table.cells), 9)
            self.assertTrue(all(cell.row_span == 1 for cell in table.cells))
            self.assertTrue(all(cell.column_span == 1 for cell in table.cells))

    def test_empty_uncovered_native_slot_is_geometry_proven_cell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _FakeTableCandidate(
                bbox=(20.0, 20.0, 80.0, 60.0),
                row_count=2,
                column_count=2,
                marker="default",
            )
            candidate.rows = (
                SimpleNamespace(
                    cells=((20.0, 20.0, 50.0, 40.0), None),
                ),
                SimpleNamespace(
                    cells=(
                        (20.0, 40.0, 50.0, 60.0),
                        (50.0, 40.0, 80.0, 60.0),
                    ),
                ),
            )
            candidate._matrix = (
                ("top-left", ""),
                ("bottom-left", "bottom-right"),
            )
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertNotIn(
                "pdf_table_merge_geometry_unresolved",
                {diagnostic.code for diagnostic in snapshot.diagnostics},
            )
            table = next(
                node.table_payload
                for node in snapshot.nodes
                if node.kind is SourceNodeKind.TABLE
            )
            assert table is not None
            self.assertEqual(len(table.cells), 4)
            empty_cell = next(
                cell for cell in table.cells if (cell.row, cell.column) == (0, 1)
            )
            self.assertEqual(empty_cell.raw_text, "")
            self.assertEqual(empty_cell.native_locator.bbox, (50.0, 20.0, 80.0, 40.0))

    def test_lines_strict_replaces_smaller_geometry_proven_equivalent_grid(
        self,
    ) -> None:
        default_candidate = _FakeTableCandidate(
            bbox=(10.0, 10.0, 90.0, 90.0),
            row_count=4,
            column_count=4,
            marker="default",
        )
        default_candidate._matrix = (
            ("A", "", "B", ""),
            ("C", "", "D", ""),
            ("E", "", "F", ""),
            ("G", "", "H", ""),
        )
        default_rows = list(default_candidate.rows)
        first_cells = list(default_rows[0].cells)
        first_cells[0] = (10.0, 10.0, 50.0, 30.0)
        default_rows[0] = SimpleNamespace(cells=tuple(first_cells))
        default_candidate.rows = tuple(default_rows)

        strict_candidate = _FakeTableCandidate(
            bbox=(10.0, 10.0, 90.0, 90.0),
            row_count=4,
            column_count=2,
            marker="strict",
        )
        strict_candidate._matrix = (
            ("A", "B"),
            ("C", "D"),
            ("E", "F"),
            ("G", "H"),
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(default_candidate,),
                strict_candidates=(strict_candidate,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
            )
        )

        self.assertEqual(selected, (strict_candidate,))
        self.assertEqual((strict_added, strict_replaced), (0, 1))

    def test_lines_strict_near_full_grid_replaces_duplicate_default_text(
        self,
    ) -> None:
        bbox = (5.0, 5.0, 95.0, 100.0)
        default_candidate = _FakeTableCandidate(
            bbox=bbox,
            row_count=6,
            column_count=6,
            marker="default",
        )
        default_candidate._matrix = (
            ("A" * 128 + "B" * 5, "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
        )
        default_rows = list(default_candidate.rows)
        first_cells = list(default_rows[0].cells)
        first_cells[0] = (5.0, 5.0, 50.0, 20.0)
        default_rows[0] = SimpleNamespace(cells=tuple(first_cells))
        default_candidate.rows = tuple(default_rows)

        strict_candidate = _FakeTableCandidate(
            bbox=bbox,
            row_count=3,
            column_count=3,
            marker="strict",
        )
        strict_candidate._matrix = (
            ("A" * 128, "", ""),
            ("", "", ""),
            ("", "", ""),
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(default_candidate,),
                strict_candidates=(strict_candidate,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
            )
        )

        self.assertEqual(selected, (strict_candidate,))
        self.assertEqual((strict_added, strict_replaced), (0, 1))

    def test_lines_strict_near_full_grid_cannot_drop_material_text(
        self,
    ) -> None:
        bbox = (5.0, 5.0, 95.0, 100.0)
        default_candidate = _FakeTableCandidate(
            bbox=bbox,
            row_count=6,
            column_count=6,
            marker="default",
        )
        default_candidate._matrix = (
            ("A" * 128 + "MATERIAL", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
            ("", "", "", "", "", ""),
        )
        default_rows = list(default_candidate.rows)
        first_cells = list(default_rows[0].cells)
        first_cells[0] = (5.0, 5.0, 50.0, 20.0)
        default_rows[0] = SimpleNamespace(cells=tuple(first_cells))
        default_candidate.rows = tuple(default_rows)
        strict_candidate = _FakeTableCandidate(
            bbox=bbox,
            row_count=3,
            column_count=3,
            marker="strict",
        )
        strict_candidate._matrix = (
            ("A" * 128, "", ""),
            ("", "", ""),
            ("", "", ""),
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(default_candidate,),
                strict_candidates=(strict_candidate,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
            )
        )

        self.assertEqual(selected, (default_candidate,))
        self.assertEqual((strict_added, strict_replaced), (0, 0))

    def test_table_suppression_ignores_whitespace_only_native_blocks(self) -> None:
        ordinary, watermarks = pdf_adapter_module._table_suppression_text_blocks(
            raw_page={
                "blocks": [
                    _fake_table_suppression_text_block(
                        bbox=(5.0, 5.0, 20.0, 10.0),
                        text=" \N{NO-BREAK SPACE} ",
                    ),
                    _fake_table_suppression_text_block(
                        bbox=(5.0, 12.0, 20.0, 18.0),
                        text="material text",
                    ),
                ]
            },
            page_rotation_matrix=None,
            candidate_bbox=(0.0, 0.0, 30.0, 30.0),
        )

        self.assertEqual(
            tuple(block.normalized_text for block in ordinary),
            ("materialtext",),
        )
        self.assertEqual(watermarks, ())

    def test_near_full_unruled_paragraph_layout_is_not_emitted_as_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_single_page_pdf(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fragments = tuple(f"ordinary-{index:02d}-" + "X" * 12 for index in range(8))
            candidate = _FakeTableCandidate(
                bbox=(0.0, 0.0, 100.0, 86.0),
                row_count=8,
                column_count=2,
                marker="layout",
            )
            candidate._matrix = tuple((fragment, "") for fragment in fragments)
            blocks = tuple(
                _fake_table_suppression_text_block(
                    bbox=(5.0, index * 10.0 + 1.0, 95.0, index * 10.0 + 9.0),
                    text=fragment,
                )
                for index, fragment in enumerate(fragments)
            )
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
                raw_blocks=blocks,
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertFalse(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertEqual(
                {node.raw_text for node in snapshot.nodes},
                set(fragments),
            )
            suppressions = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_default_table_candidate_suppressed"
            )
            self.assertEqual(len(suppressions), 1)
            self.assertIn(
                "near_full_unruled_paragraph_layout",
                suppressions[0].message,
            )
            self.assertIsNotNone(suppressions[0].evidence_sha256)
            self.assertFalse(
                any(fragment in suppressions[0].message for fragment in fragments)
            )

    def test_watermark_only_tiny_layout_is_not_emitted_as_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_single_page_pdf(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _FakeTableCandidate(
                bbox=(10.0, 10.0, 60.0, 20.0),
                row_count=3,
                column_count=4,
                marker="watermark",
            )
            candidate._matrix = (("W" * 20, "", "", ""),) + (("", "", "", ""),) * 2
            watermark_text = "W" * 18
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
                raw_blocks=(
                    _fake_table_suppression_text_block(
                        bbox=(10.0, 10.0, 60.0, 20.0),
                        text=watermark_text,
                        watermark=True,
                    ),
                ),
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertFalse(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertEqual(
                tuple(node.raw_text for node in snapshot.nodes),
                (watermark_text,),
            )
            self.assertTrue(
                all(node.kind is SourceNodeKind.WATERMARK for node in snapshot.nodes)
            )
            suppressions = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_default_table_candidate_suppressed"
            )
            self.assertEqual(len(suppressions), 1)
            self.assertIn("watermark_only_tiny_layout", suppressions[0].message)
            self.assertIsNotNone(suppressions[0].evidence_sha256)
            self.assertNotIn(watermark_text, suppressions[0].message)

    def test_shallow_wide_paragraph_conflict_is_not_emitted_as_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_single_page_pdf(source)
            preflight = LocalDocumentPreflight().inspect(source)
            candidate = _FakeTableCandidate(
                bbox=(0.0, 40.0, 100.0, 50.0),
                row_count=5,
                column_count=12,
                marker="paragraph",
            )
            candidate._matrix = (("Z" * 20,) + ("",) * 11,) + (("",) * 12,) * 4
            blocks = (
                _fake_table_suppression_text_block(
                    bbox=(0.0, 40.0, 75.0, 45.0),
                    text="Z" * 9,
                ),
                _fake_table_suppression_text_block(
                    bbox=(0.0, 45.0, 75.0, 50.0),
                    text="Z" * 9,
                ),
            )
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(candidate,),
                strict_candidates=(),
                raw_blocks=blocks,
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertFalse(
                any(node.kind is SourceNodeKind.TABLE for node in snapshot.nodes)
            )
            self.assertEqual(
                tuple(node.raw_text for node in snapshot.nodes),
                ("Z" * 9, "Z" * 9),
            )
            suppressions = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_default_table_candidate_suppressed"
            )
            self.assertEqual(len(suppressions), 1)
            self.assertIn(
                "shallow_wide_paragraph_conflict",
                suppressions[0].message,
            )
            self.assertIsNotNone(suppressions[0].evidence_sha256)
            self.assertNotIn("Z" * 9, suppressions[0].message)

    def test_table_suppression_rules_keep_boundary_misses(self) -> None:
        near_full_text = "A" * 128
        near_full = _FakeTableCandidate(
            bbox=(0.0, 0.0, 100.0, 85.0),
            row_count=8,
            column_count=2,
            marker="near-full",
        )
        near_full._matrix = ((near_full_text, ""),) + (("", ""),) * 7
        near_full_blocks = tuple(
            _fake_table_suppression_text_block(
                bbox=(0.0, index * 10.0, 100.0, index * 10.0 + 8.0),
                text=near_full_text if index == 0 else "",
            )
            for index in range(8)
        )

        watermark = _FakeTableCandidate(
            bbox=(10.0, 10.0, 60.01, 20.0),
            row_count=3,
            column_count=4,
            marker="watermark",
        )
        watermark._matrix = (("W" * 20, "", "", ""),) + (("", "", "", ""),) * 2
        watermark_blocks = (
            _fake_table_suppression_text_block(
                bbox=(10.0, 10.0, 60.01, 20.0),
                text="W" * 20,
                watermark=True,
            ),
        )

        shallow_wide = _FakeTableCandidate(
            bbox=(0.0, 40.0, 100.0, 50.0),
            row_count=5,
            column_count=12,
            marker="wide",
        )
        shallow_wide._matrix = (("Z" * 20,) + ("",) * 11,) + (("",) * 12,) * 4
        shallow_wide_blocks = (
            _fake_table_suppression_text_block(
                bbox=(0.0, 40.0, 74.99, 45.0),
                text="Z" * 10,
            ),
            _fake_table_suppression_text_block(
                bbox=(0.0, 45.0, 75.0, 50.0),
                text="Z" * 10,
            ),
        )

        for label, candidate, blocks in (
            ("area_must_exceed_85_percent", near_full, near_full_blocks),
            ("watermark_area_must_not_exceed_5_percent", watermark, watermark_blocks),
            (
                "every_wide_block_must_reach_75_percent",
                shallow_wide,
                shallow_wide_blocks,
            ),
        ):
            with self.subTest(label=label):
                retained, diagnostics = (
                    pdf_adapter_module._suppress_default_table_candidates(
                        default_candidates=(candidate,),
                        raw_strict_candidates=(),
                        raw_page={"blocks": blocks},
                        page_rotation_matrix=None,
                        page_number=1,
                        page_bbox=(0.0, 0.0, 100.0, 100.0),
                        source_sha256="a" * 64,
                    )
                )
                self.assertEqual(retained, (candidate,))
                self.assertEqual(diagnostics, ())

    def test_lines_strict_adds_ruled_grid_missed_by_default_detector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            strict_candidate = _FakeTableCandidate(
                bbox=(20.0, 20.0, 80.0, 70.0),
                row_count=3,
                column_count=3,
                marker="strict",
            )
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(),
                strict_candidates=(strict_candidate,),
            )
            fake_document = _FakeDocumentWithPage(fake_page)

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            table_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(table_nodes), 1)
            table = table_nodes[0].table_payload
            assert table is not None
            self.assertEqual((table.row_count, table.column_count), (3, 3))
            self.assertEqual(fake_page.strategies, [None, "lines_strict"])
            self.assertIn(
                "pdf_lines_strict_table_enhancement_applied",
                {item.code for item in snapshot.diagnostics},
            )

    def test_lines_strict_replaces_one_overlapping_obviously_incomplete_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            default_candidate = _FakeTableCandidate(
                bbox=(10.0, 10.0, 80.0, 80.0),
                row_count=2,
                column_count=2,
                marker="default",
            )
            strict_candidate = _FakeTableCandidate(
                bbox=(8.0, 8.0, 82.0, 82.0),
                row_count=4,
                column_count=3,
                marker="strict",
            )
            fake_document = _FakeDocumentWithPage(
                _FakePageWithTableCandidates(
                    default_candidates=(default_candidate,),
                    strict_candidates=(strict_candidate,),
                )
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            tables = tuple(
                node.table_payload
                for node in snapshot.nodes
                if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(tables), 1)
            assert tables[0] is not None
            self.assertEqual((tables[0].row_count, tables[0].column_count), (4, 3))
            self.assertTrue(
                all(cell.raw_text.startswith("strict-") for cell in tables[0].cells)
            )

    def test_lines_strict_caption_cleanup_enables_more_complete_column_grid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            default_candidate = _FakeTableCandidate(
                bbox=(10.0, 20.0, 90.0, 80.0),
                row_count=4,
                column_count=4,
                marker="default",
            )
            strict_candidate = _fake_captioned_table_candidate(
                bbox=(10.0, 10.0, 90.0, 80.0),
                body_row_count=4,
                column_count=6,
                marker="strict",
            )
            fake_document = _FakeDocumentWithPage(
                _FakePageWithTableCandidates(
                    default_candidates=(default_candidate,),
                    strict_candidates=(strict_candidate,),
                    bboxlog=(("fill-path", (12.0, 12.0, 15.0, 16.0)),),
                )
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            tables = tuple(
                node.table_payload
                for node in snapshot.nodes
                if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(tables), 1)
            assert tables[0] is not None
            self.assertEqual((tables[0].row_count, tables[0].column_count), (4, 6))
            self.assertTrue(
                all(cell.raw_text.startswith("strict-") for cell in tables[0].cells)
            )
            self.assertIn(
                "pdf_lines_strict_table_enhancement_applied",
                {item.code for item in snapshot.diagnostics},
            )

    def test_table_candidate_preparation_preserves_ruled_full_width_body_row(
        self,
    ) -> None:
        candidate = _fake_captioned_table_candidate(
            bbox=(10.0, 10.0, 90.0, 80.0),
            body_row_count=4,
            column_count=4,
            marker="body",
        )
        page_bboxlog = _fake_ruled_first_row_bboxlog(
            bbox=(10.0, 10.0, 90.0, 20.0),
        )

        prepared = pdf_adapter_module._prepare_table_candidate(
            candidate,
            page_bboxlog=page_bboxlog,
        )

        self.assertIs(prepared, candidate)
        self.assertEqual(
            pdf_adapter_module._candidate_dimensions(prepared),
            (5, 4),
        )

    def test_table_candidate_preparation_removes_ruled_caption_before_giant_panel(
        self,
    ) -> None:
        x0, y0, x1 = 10.0, 10.0, 90.0
        column_count = 4
        caption_y1 = 20.0
        panel_y1 = 90.0
        row_boundaries = (caption_y1, panel_y1, 110.0, 130.0)
        rows = [
            SimpleNamespace(
                bbox=(x0, y0, x1, caption_y1),
                cells=((x0, y0, x1, caption_y1), None, None, None),
            ),
            SimpleNamespace(
                bbox=(x0, caption_y1, x1, panel_y1),
                cells=((x0, caption_y1, x1, panel_y1), None, None, None),
            ),
        ]
        for row_y0, row_y1 in zip(
            row_boundaries[1:-1],
            row_boundaries[2:],
            strict=True,
        ):
            rows.append(
                SimpleNamespace(
                    bbox=(x0, row_y0, x1, row_y1),
                    cells=tuple(
                        (
                            x0 + column_index * 20.0,
                            row_y0,
                            x0 + (column_index + 1) * 20.0,
                            row_y1,
                        )
                        for column_index in range(column_count)
                    ),
                )
            )
        matrix = (
            ("caption", "", "", ""),
            ("panel", "", "", ""),
            ("a", "b", "c", "d"),
            ("e", "f", "g", "h"),
        )
        candidate = SimpleNamespace(
            bbox=(x0, y0, x1, row_boundaries[-1]),
            row_count=4,
            col_count=column_count,
            rows=tuple(rows),
            extract=lambda: matrix,
        )
        page_bboxlog = (
            *_fake_ruled_first_row_bboxlog(
                bbox=(x0, y0, x1, caption_y1),
            ),
            ("fill-path", (12.0, 12.0, 15.0, 16.0)),
        )

        prepared = pdf_adapter_module._prepare_table_candidate(
            candidate,
            page_bboxlog=page_bboxlog,
        )

        self.assertIsNot(prepared, candidate)
        self.assertEqual(
            pdf_adapter_module._candidate_dimensions(prepared),
            (3, 4),
        )
        self.assertEqual(prepared.bbox, (x0, caption_y1, x1, row_boundaries[-1]))
        self.assertEqual(prepared.extract()[0], matrix[1])
        self.assertEqual(prepared.rows[0].cells[1:], (None, None, None))

    def test_lines_strict_ruled_axis_tradeoff_recovers_missing_columns(
        self,
    ) -> None:
        default_candidate, strict_candidate, page_bboxlog = (
            _fake_ruled_axis_tradeoff_candidates()
        )
        prepared_strict = pdf_adapter_module._prepare_table_candidate(
            strict_candidate,
            page_bboxlog=page_bboxlog,
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(default_candidate,),
                strict_candidates=(prepared_strict,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
                page_bboxlog=page_bboxlog,
            )
        )

        self.assertEqual(selected, (prepared_strict,))
        self.assertEqual((strict_added, strict_replaced), (0, 1))
        self.assertEqual(
            pdf_adapter_module._candidate_dimensions(prepared_strict),
            (5, 4),
        )

    def test_lines_strict_ruled_axis_tradeoff_cannot_drop_material_text(
        self,
    ) -> None:
        default_candidate, strict_candidate, page_bboxlog = (
            _fake_ruled_axis_tradeoff_candidates(strict_character_count=8)
        )
        prepared_strict = pdf_adapter_module._prepare_table_candidate(
            strict_candidate,
            page_bboxlog=page_bboxlog,
        )

        selected, strict_added, strict_replaced = (
            pdf_adapter_module._select_table_candidates(
                default_candidates=(default_candidate,),
                strict_candidates=(prepared_strict,),
                page_bbox=(0.0, 0.0, 100.0, 100.0),
                page_bboxlog=page_bboxlog,
            )
        )

        self.assertEqual(selected, (default_candidate,))
        self.assertEqual((strict_added, strict_replaced), (0, 0))

    def test_lines_strict_adds_geometry_proven_two_row_ruled_grid(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            strict_candidate = _fake_two_row_strict_table_candidate()
            page_bboxlog = _fake_two_row_table_bboxlog()
            fake_page = _FakePageWithTableCandidates(
                default_candidates=(),
                strict_candidates=(strict_candidate,),
                bboxlog=page_bboxlog,
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=_FakeDocumentWithPage(fake_page),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

        tables = tuple(
            node.table_payload
            for node in snapshot.nodes
            if node.kind is SourceNodeKind.TABLE
        )
        self.assertEqual(len(tables), 1)
        assert tables[0] is not None
        self.assertEqual((tables[0].row_count, tables[0].column_count), (2, 3))
        self.assertIn(
            "pdf_lines_strict_table_enhancement_applied",
            {item.code for item in snapshot.diagnostics},
        )

    def test_lines_strict_two_row_exception_fails_closed_without_all_proofs(
        self,
    ) -> None:
        for label, candidate, page_bboxlog in (
            (
                "missing_bottom_rule",
                _fake_two_row_strict_table_candidate(),
                _fake_two_row_table_bboxlog()[:-2],
            ),
            (
                "unproven_merge_text",
                _fake_two_row_strict_table_candidate(
                    incompatible_merged_text=True,
                ),
                _fake_two_row_table_bboxlog(),
            ),
        ):
            with self.subTest(label=label):
                selected, strict_added, strict_replaced = (
                    pdf_adapter_module._select_table_candidates(
                        default_candidates=(),
                        strict_candidates=(candidate,),
                        page_bbox=(0.0, 0.0, 100.0, 100.0),
                        page_bboxlog=page_bboxlog,
                    )
                )

                self.assertEqual(selected, ())
                self.assertEqual((strict_added, strict_replaced), (0, 0))

    def test_table_candidate_preparation_preserves_tall_full_width_body_row(
        self,
    ) -> None:
        candidate = _fake_captioned_table_candidate(
            bbox=(10.0, 10.0, 90.0, 180.0),
            body_row_count=4,
            column_count=4,
            marker="body",
        )
        rows = list(candidate.rows)
        rows[0] = SimpleNamespace(
            bbox=(10.0, 10.0, 90.0, 110.0),
            cells=((10.0, 10.0, 90.0, 110.0), None, None, None),
        )
        body_height = 70.0 / 4
        for row_index in range(1, 5):
            y0 = 110.0 + (row_index - 1) * body_height
            y1 = 110.0 + row_index * body_height
            rows[row_index] = SimpleNamespace(
                bbox=(10.0, y0, 90.0, y1),
                cells=tuple(
                    (
                        10.0 + column_index * 20.0,
                        y0,
                        30.0 + column_index * 20.0,
                        y1,
                    )
                    for column_index in range(4)
                ),
            )
        candidate.rows = tuple(rows)

        prepared = pdf_adapter_module._prepare_table_candidate(
            candidate,
            page_bboxlog=(("fill-path", (12.0, 12.0, 15.0, 16.0)),),
        )

        self.assertIs(prepared, candidate)

    def test_table_caption_cleanup_preserves_native_merged_body_row(self) -> None:
        x0, y0, x1 = 10.0, 10.0, 90.0
        column_count = 4
        caption_y1 = 20.0
        row_boundaries = (caption_y1, 40.0, 60.0, 80.0)
        caption_cells = ((x0, y0, x1, caption_y1), None, None, None)
        rows = [
            SimpleNamespace(
                bbox=(x0, y0, x1, caption_y1),
                cells=caption_cells,
            ),
            SimpleNamespace(
                bbox=(x0, row_boundaries[0], x1, row_boundaries[1]),
                cells=(
                    (x0, row_boundaries[0], x1, row_boundaries[1]),
                    None,
                    None,
                    None,
                ),
            ),
        ]
        for row_index in range(1, 3):
            row_y0 = row_boundaries[row_index]
            row_y1 = row_boundaries[row_index + 1]
            rows.append(
                SimpleNamespace(
                    bbox=(x0, row_y0, x1, row_y1),
                    cells=tuple(
                        (
                            x0 + column_index * 20.0,
                            row_y0,
                            x0 + (column_index + 1) * 20.0,
                            row_y1,
                        )
                        for column_index in range(column_count)
                    ),
                )
            )
        matrix = (
            ("caption", "", "", ""),
            ("merged body", "", "", ""),
            ("a", "b", "c", "d"),
            ("e", "f", "g", "h"),
        )
        candidate = SimpleNamespace(
            bbox=(x0, y0, x1, row_boundaries[-1]),
            row_count=4,
            col_count=column_count,
            rows=tuple(rows),
            extract=lambda: matrix,
        )

        prepared = pdf_adapter_module._prepare_table_candidate(
            candidate,
            page_bboxlog=(("fill-path", (12.0, 12.0, 15.0, 16.0)),),
        )

        self.assertIsNot(prepared, candidate)
        self.assertEqual((prepared.row_count, prepared.col_count), (3, 4))
        self.assertEqual(prepared.rows[0].cells[1:], (None, None, None))
        observation = pdf_adapter_module._candidate_geometry_observation(prepared)
        self.assertIsNotNone(observation)
        assert observation is not None
        proven_geometry, _, _ = observation
        self.assertIsNotNone(proven_geometry)
        assert proven_geometry is not None
        self.assertEqual(proven_geometry[(0, 0)].column_span, 4)

    def test_mixed_layout_parent_is_replaced_by_unique_dense_table_child(
        self,
    ) -> None:
        parent = _fake_mixed_layout_parent_candidate()
        nested_child = _FakeTableCandidate(
            bbox=(10.0, 50.0, 30.0, 60.0),
            row_count=2,
            column_count=2,
            marker="nested",
        )

        candidates, diagnostics = (
            pdf_adapter_module._decompose_mixed_layout_table_candidates(
                candidates=(parent, nested_child),
                page_number=7,
                page_bbox=(0.0, 0.0, 100.0, 100.0),
                source_sha256="a" * 64,
            )
        )

        self.assertEqual(len(candidates), 2)
        dense_child = candidates[0]
        self.assertEqual((dense_child.row_count, dense_child.col_count), (3, 4))
        self.assertEqual(
            dense_child.extract(),
            (
                ("dense-3-1", "dense-3-2", "dense-3-3", "dense-3-4"),
                ("dense-4-1", "dense-4-2", "dense-4-3", "dense-4-4"),
                ("dense-5-1", "dense-5-2", "dense-5-3", "dense-5-4"),
            ),
        )
        self.assertIs(candidates[1], nested_child)
        self.assertEqual(len(diagnostics), 1)
        self.assertEqual(
            diagnostics[0].code,
            "pdf_mixed_layout_table_candidate_decomposed",
        )
        self.assertIsNotNone(diagnostics[0].evidence_sha256)

    def test_mixed_layout_parent_without_separate_child_fails_closed(self) -> None:
        parent = _fake_mixed_layout_parent_candidate()

        candidates, diagnostics = (
            pdf_adapter_module._decompose_mixed_layout_table_candidates(
                candidates=(parent,),
                page_number=7,
                page_bbox=(0.0, 0.0, 100.0, 100.0),
                source_sha256="a" * 64,
            )
        )

        self.assertEqual(candidates, (parent,))
        self.assertEqual(diagnostics, ())

    def test_lines_strict_deduplicates_same_grid_and_rejects_full_page_pseudo_table(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            default_candidate = _FakeTableCandidate(
                bbox=(10.0, 10.0, 80.0, 80.0),
                row_count=2,
                column_count=2,
                marker="default",
            )
            duplicate_candidate = _FakeTableCandidate(
                bbox=(10.0, 10.0, 80.0, 80.0),
                row_count=2,
                column_count=2,
                marker="duplicate",
            )
            pseudo_candidate = _FakeTableCandidate(
                bbox=(0.0, 0.0, 100.0, 100.0),
                row_count=20,
                column_count=3,
                marker="pseudo",
            )
            fake_document = _FakeDocumentWithPage(
                _FakePageWithTableCandidates(
                    default_candidates=(default_candidate,),
                    strict_candidates=(duplicate_candidate, pseudo_candidate),
                )
            )

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            tables = tuple(
                node.table_payload
                for node in snapshot.nodes
                if node.kind is SourceNodeKind.TABLE
            )
            self.assertEqual(len(tables), 1)
            assert tables[0] is not None
            self.assertEqual((tables[0].row_count, tables[0].column_count), (2, 2))
            self.assertTrue(
                all(cell.raw_text.startswith("default-") for cell in tables[0].cells)
            )

    def test_identity_is_path_independent_and_repeated_text_uses_locator_ids(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_dir = root / "first"
            second_dir = root / "second"
            first_dir.mkdir()
            second_dir.mkdir()
            first = first_dir / "one.pdf"
            second = second_dir / "two.pdf"
            _write_pdf_with_text_and_image(first)
            shutil.copyfile(first, second)

            first_snapshot = build_source_ir(first, clock=lambda: CREATED_AT)
            second_snapshot = build_source_ir(second, clock=lambda: CREATED_AT)

            self.assertEqual(
                first_snapshot.model_dump(mode="json"),
                second_snapshot.model_dump(mode="json"),
            )
            repeated_nodes = tuple(
                node
                for node in first_snapshot.nodes
                if node.kind is SourceNodeKind.PARAGRAPH
                and node.raw_text == "raw   text"
            )
            self.assertEqual(len(repeated_nodes), 2)
            self.assertNotEqual(
                repeated_nodes[0].source_node_id,
                repeated_nodes[1].source_node_id,
            )
            self.assertNotEqual(
                repeated_nodes[0].native_locator.block_index,
                repeated_nodes[1].native_locator.block_index,
            )

    def test_repeated_image_bytes_share_one_asset_but_keep_each_placement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "repeated-image.pdf"
            document = pymupdf.open()
            for _ in range(2):
                page = document.new_page()
                page.insert_image(pymupdf.Rect(20, 20, 100, 100), stream=_PNG_BYTES)
            document.save(source)
            document.close()

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            image_nodes = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(len(image_nodes), 2)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                {node.asset_ref for node in image_nodes},
                {snapshot.assets[0].asset_id},
            )
            self.assertNotEqual(
                image_nodes[0].native_locator,
                image_nodes[1].native_locator,
            )
            self.assertIsNone(snapshot.assets[0].native_locator)

    def test_injected_asset_publisher_receives_exact_unique_image_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "published-image.pdf"
            _write_pdf_with_text_and_image(source)
            published: list[tuple[object, bytes]] = []
            adapter = PdfSourceIRAdapter(
                source_asset_publisher=lambda asset, payload: published.append(
                    (asset, payload)
                )
            )

            snapshot = build_source_ir(
                source,
                adapters=(adapter,),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(len(published), 1)
            asset, payload = published[0]
            self.assertEqual(asset, snapshot.assets[0])
            self.assertEqual(hashlib.sha256(payload).hexdigest(), asset.content_sha256)

    def test_injected_asset_publisher_failure_aborts_source_ir_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "unpublished-image.pdf"
            _write_pdf_with_text_and_image(source)

            def reject_asset(_asset: object, _payload: bytes) -> None:
                raise RuntimeError("asset object publication unavailable")

            with self.assertRaisesRegex(
                RuntimeError,
                "asset object publication unavailable",
            ):
                build_source_ir(
                    source,
                    adapters=(
                        PdfSourceIRAdapter(
                            source_asset_publisher=reject_asset,
                        ),
                    ),
                    clock=lambda: CREATED_AT,
                )

    def test_exact_white_unmasked_image_is_excluded_but_colored_image_remains(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "white-and-colored-images.pdf"
            white_output = io.BytesIO()
            PILImage.new("RGB", (20, 10), "white").save(
                white_output,
                format="JPEG",
                quality=100,
                subsampling=0,
            )
            colored_output = io.BytesIO()
            PILImage.new("RGB", (20, 10), (20, 80, 160)).save(
                colored_output,
                format="PNG",
            )
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_image(
                pymupdf.Rect(20, 20, 120, 70),
                stream=white_output.getvalue(),
            )
            page.insert_image(
                pymupdf.Rect(160, 20, 260, 70),
                stream=colored_output.getvalue(),
            )
            document.save(source)
            document.close()
            before = _relative_inventory(root)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            images = tuple(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            self.assertEqual(len(images), 1)
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                images[0].native_locator.bbox,
                (160.0, 20.0, 260.0, 70.0),
            )
            exclusions = tuple(
                diagnostic
                for diagnostic in snapshot.diagnostics
                if diagnostic.code == "pdf_uniform_opaque_white_image_excluded"
            )
            self.assertEqual(len(exclusions), 1)
            self.assertIsNotNone(exclusions[0].native_locator)
            self.assertIsNotNone(exclusions[0].evidence_sha256)
            self.assertEqual(_relative_inventory(root), before)

    def test_masked_white_image_and_budget_ambiguity_remain_fail_closed(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            masked_source = root / "masked-white-image.pdf"
            white_output = io.BytesIO()
            PILImage.new("RGB", (20, 20), "white").save(
                white_output,
                format="PNG",
            )
            mask_output = io.BytesIO()
            PILImage.new("L", (20, 20), 128).save(mask_output, format="PNG")
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_image(
                pymupdf.Rect(20, 20, 120, 120),
                stream=white_output.getvalue(),
                mask=mask_output.getvalue(),
            )
            document.save(masked_source)
            document.close()

            masked_snapshot = build_source_ir(
                masked_source,
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(
                sum(
                    node.kind is SourceNodeKind.IMAGE for node in masked_snapshot.nodes
                ),
                1,
            )
            self.assertNotIn(
                "pdf_uniform_opaque_white_image_excluded",
                {diagnostic.code for diagnostic in masked_snapshot.diagnostics},
            )

            budget_source = root / "white-image-budget-exhausted.pdf"
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_image(
                pymupdf.Rect(20, 20, 120, 120),
                stream=white_output.getvalue(),
            )
            document.save(budget_source)
            document.close()

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_UNIFORM_WHITE_IMAGE_PROOF_WORK_BYTES",
                0,
            ):
                budget_snapshot = build_source_ir(
                    budget_source,
                    clock=lambda: CREATED_AT,
                )

            self.assertEqual(
                sum(
                    node.kind is SourceNodeKind.IMAGE for node in budget_snapshot.nodes
                ),
                1,
            )
            self.assertNotIn(
                "pdf_uniform_opaque_white_image_excluded",
                {diagnostic.code for diagnostic in budget_snapshot.diagnostics},
            )

    def test_annotation_layer_is_detected_but_comment_content_is_not_ingested(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "annotated.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "visible source text")
            page.add_text_annot((96, 96), "private engineer annotation")
            document.save(source)
            document.close()

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertIn(
                "pdf_annotations_ignored_by_policy",
                {item.code for item in snapshot.diagnostics},
            )
            serialized = snapshot.model_dump_json()
            self.assertIn("visible source text", serialized)
            self.assertNotIn("private engineer annotation", serialized)

    def test_blank_page_is_explicitly_partial_and_located(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "blank.pdf"
            document = pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.nodes, ())
            diagnostic = next(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_page_without_locatable_content"
            )
            self.assertIsNotNone(diagnostic.native_locator)
            self.assertEqual(diagnostic.native_locator.page_number, 1)
            self.assertIsNotNone(diagnostic.native_locator.bbox)

    def test_whole_page_image_with_alpha_zero_text_keeps_raw_text_and_visual_sidecar(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "hidden-layer.pdf"
            _write_pdf_with_full_page_image_and_invisible_text(source)
            before = _relative_inventory(root)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            paragraph = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.PARAGRAPH
            )
            self.assertEqual(paragraph.raw_text, "hidden OCR layer")
            self.assertEqual(len(snapshot.visual_text_snapshots), 1)
            visual_snapshot = snapshot.visual_text_snapshots[0]
            self.assertEqual(
                tuple(item.raw_text for item in visual_snapshot.observations),
                ("visual page fact",),
            )
            self.assertEqual(visual_snapshot.source_sha256, snapshot.source_sha256)
            self.assertEqual(visual_snapshot.page_number, 1)
            self.assertEqual(len(provider.page_inputs), 1)
            page_input = provider.page_inputs[0]
            self.assertEqual(page_input.source_sha256, snapshot.source_sha256)
            self.assertEqual(
                page_input.page_image_sha256,
                hashlib.sha256(page_input.page_image_bytes).hexdigest(),
            )
            self.assertTrue(page_input.page_image_bytes.startswith(b"\x89PNG"))
            diagnostic_codes = {item.code for item in snapshot.diagnostics}
            self.assertIn("pdf_invisible_text_span_detected", diagnostic_codes)
            self.assertIn(
                "pdf_full_page_image_with_invisible_text_layer",
                diagnostic_codes,
            )
            self.assertIn("pdf_visual_text_observations_captured", diagnostic_codes)
            self.assertNotIn(
                "hidden OCR layer",
                "\n".join(item.message for item in snapshot.diagnostics),
            )
            self.assertEqual(_relative_inventory(root), before)

    def test_invisible_text_without_whole_page_image_is_partial_without_ocr_guess(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "invisible-text.pdf"
            document = pymupdf.open()
            page = document.new_page(width=200, height=200)
            page.insert_text((20, 40), "hidden native fact", render_mode=3)
            document.save(source)
            document.close()
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.visual_text_snapshots, ())
            self.assertEqual(provider.page_inputs, [])
            self.assertEqual(
                next(
                    node.raw_text
                    for node in snapshot.nodes
                    if node.kind is SourceNodeKind.PARAGRAPH
                ),
                "hidden native fact",
            )
            self.assertIn(
                "pdf_invisible_text_span_detected",
                {item.code for item in snapshot.diagnostics},
            )

    def test_whole_page_scan_without_native_text_uses_explicit_visual_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scan.pdf"
            _write_pdf_with_full_page_image(source)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(provider.page_inputs), 1)
            self.assertEqual(len(snapshot.visual_text_snapshots), 1)
            self.assertIn(
                "pdf_full_page_image_without_visible_text",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertIn(
                "pdf_visual_figure_content_not_evaluated",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertEqual(snapshot.assets[0].kind, SourceAssetKind.IMAGE)
            self.assertNotIn(
                "pdf_mrc_scan_page_composited",
                {item.code for item in snapshot.diagnostics},
            )
            image_node = next(
                node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
            )
            structure = build_structure_tree(snapshot)
            self.assertFalse(
                any(
                    ref.kind.value == "source_node"
                    and ref.source_id == image_node.source_node_id
                    for structure_node in structure.nodes
                    for ref in structure_node.source_refs
                )
            )

    def test_full_page_scan_requires_figure_evaluation_beyond_text_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scan-layout.pdf"
            _write_pdf_with_full_page_image(source)
            provider = _RecordingFullLayoutVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(len(snapshot.visual_text_snapshots), 1)
            self.assertNotIn(
                "pdf_visual_figure_content_not_evaluated",
                {item.code for item in snapshot.diagnostics},
            )

    def test_mrc_scan_layers_fold_to_one_bound_4x_page_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "mrc-scan.pdf"
            _write_pdf_with_mrc_scan_page(source, stencil_count=2)
            before = _relative_inventory(root)
            provider = _RecordingVisualTextProvider()

            with pymupdf.open(source) as document:
                page = document[0]
                raw_image_hashes = {
                    hashlib.sha256(bytes(block["image"])).hexdigest()
                    for block in page.get_text("rawdict", sort=False)["blocks"]
                    if block.get("type") == 1
                }
                expected_render = bytes(
                    page.get_pixmap(
                        matrix=pymupdf.Matrix(4.0, 4.0),
                        alpha=False,
                        annots=False,
                    ).tobytes("png")
                )

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(snapshot.nodes), 1)
            self.assertEqual(snapshot.nodes[0].kind, SourceNodeKind.IMAGE)
            self.assertIsNone(snapshot.nodes[0].native_locator.block_index)
            self.assertEqual(snapshot.nodes[0].native_locator.page_number, 1)
            self.assertEqual(len(snapshot.assets), 1)
            asset = snapshot.assets[0]
            self.assertEqual(asset.kind, SourceAssetKind.PAGE_RENDER)
            self.assertEqual(asset.media_type, "image/png")
            self.assertEqual(
                asset.content_sha256,
                hashlib.sha256(expected_render).hexdigest(),
            )
            self.assertNotIn(asset.content_sha256, raw_image_hashes)
            self.assertIsNotNone(asset.native_locator)
            assert asset.native_locator is not None
            self.assertEqual(asset.native_locator.page_number, 1)
            self.assertIsNone(asset.native_locator.block_index)
            self.assertEqual(len(provider.page_inputs), 1)
            page_input = provider.page_inputs[0]
            self.assertEqual(
                (page_input.page_image_width_px, page_input.page_image_height_px),
                (400, 400),
            )
            self.assertEqual(page_input.page_image_bytes, expected_render)
            self.assertEqual(page_input.page_image_sha256, asset.content_sha256)
            summary = next(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_mrc_scan_page_composited"
            )
            self.assertIn("3 original image layer(s)", summary.message)
            self.assertIn("[0, 1, 2]", summary.message)
            self.assertIsNotNone(summary.evidence_sha256)
            layer_diagnostics = tuple(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_mrc_source_layer_folded"
            )
            self.assertEqual(len(layer_diagnostics), 3)
            self.assertEqual(
                tuple(item.native_locator.block_index for item in layer_diagnostics),
                (0, 1, 2),
            )
            self.assertTrue(
                all(item.evidence_sha256 is not None for item in layer_diagnostics)
            )
            self.assertEqual(_relative_inventory(root), before)

    def test_mrc_scan_with_invisible_text_keeps_text_and_one_page_render(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-hidden-text.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                invisible_text=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        image_nodes = tuple(
            node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
        )
        self.assertEqual(len(image_nodes), 1)
        self.assertEqual(snapshot.assets[0].kind, SourceAssetKind.PAGE_RENDER)
        self.assertTrue(
            any(node.raw_text.startswith("hidden native tex") for node in snapshot.nodes)
        )
        summary = next(
            diagnostic
            for diagnostic in snapshot.diagnostics
            if diagnostic.code == "pdf_mrc_scan_page_composited"
        )
        self.assertIn("native text blocks retained separately [2]", summary.message)

    def test_mrc_scan_with_proven_header_and_orthogonal_drawing_is_page_furniture(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-header-drawing.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                visible_text=True,
                one_pixel_base=True,
                same_transform_stencil=True,
                include_drawing=True,
            )
            with mock.patch.object(
                pdf_adapter_module,
                "_pdf_variable_page_marker_kinds",
                return_value={(1, 2): SourceNodeKind.HEADER},
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        image_node = next(
            node for node in snapshot.nodes if node.kind is SourceNodeKind.IMAGE
        )
        header_node = next(
            node for node in snapshot.nodes if node.kind is SourceNodeKind.HEADER
        )
        asset = next(
            asset for asset in snapshot.assets if asset.asset_id == image_node.asset_ref
        )
        self.assertIs(asset.kind, SourceAssetKind.PAGE_RENDER)
        summary = next(
            diagnostic
            for diagnostic in snapshot.diagnostics
            if diagnostic.code == "pdf_mrc_scan_page_composited"
        )
        self.assertIn("orthogonal drawing count 1", summary.message)

        structure = build_structure_tree(snapshot)
        structure_by_source_id = {
            ref.source_id: structure_node
            for structure_node in structure.nodes
            for ref in structure_node.source_refs
            if ref.kind.value == "source_node"
        }
        self.assertNotIn(
            image_node.source_node_id,
            structure_by_source_id,
        )
        self.assertIs(
            structure_by_source_id[header_node.source_node_id].boundary_basis,
            StructureBoundaryBasis.PAGE_FURNITURE,
        )
        semantics = classify_content(snapshot, structure)
        annotations_by_target = {
            annotation.target_structure_node_id: annotation
            for annotation in semantics.annotations
        }
        self.assertIs(
            annotations_by_target[
                structure_by_source_id[header_node.source_node_id].structure_node_id
            ].semantic_class,
            SemanticClass.NOISE,
        )
        self.assertNotIn(
            image_node.source_node_id,
            {
                ref.source_id
                for structure_node in structure.nodes
                for ref in structure_node.source_refs
                if ref.kind.value == "source_node"
            },
        )
        page_view = build_page_comparison_view(
            snapshot,
            structure,
            page_bounds=((0.0, 0.0, 100.0, 100.0),),
        )
        page = page_view["pages"][0]
        self.assertEqual(page["counts"]["support_blocks"], 1)
        self.assertEqual(
            [
                block["type"]
                for block in page["blocks"]
                if block["id"] == image_node.source_node_id
            ],
            ["PAGE_RASTER"],
        )

    def test_mrc_scan_rejects_unproven_page_drawings(self) -> None:
        for drawing_kind in (
            "small_gray_rectangle",
            "large_white_rectangle",
            "white_triangle",
        ):
            with self.subTest(drawing_kind=drawing_kind):
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / f"mrc-{drawing_kind}.pdf"
                    _write_pdf_with_mrc_scan_page(
                        source,
                        stencil_count=1,
                        one_pixel_base=True,
                        same_transform_stencil=True,
                        include_drawing=True,
                        drawing_kind=drawing_kind,
                    )

                    snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

                self.assertEqual(
                    sum(
                        node.kind is SourceNodeKind.IMAGE
                        for node in snapshot.nodes
                    ),
                    2,
                )
                self.assertEqual(
                    {asset.kind for asset in snapshot.assets},
                    {SourceAssetKind.IMAGE},
                )
                self.assertNotIn(
                    "pdf_mrc_scan_page_composited",
                    {diagnostic.code for diagnostic in snapshot.diagnostics},
                )

    def test_rotated_mrc_scan_uses_visible_page_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-rotated.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                one_pixel_base=True,
                same_transform_stencil=True,
                page_width=100,
                page_height=200,
                rotation=90,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        self.assertEqual(
            [asset.kind for asset in snapshot.assets],
            [SourceAssetKind.PAGE_RENDER],
        )
        self.assertIn(
            "pdf_mrc_scan_page_composited",
            {diagnostic.code for diagnostic in snapshot.diagnostics},
        )

    def test_qct_shaped_60_page_mrc_render_budget_is_per_page_peak(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "qct-shaped-mrc.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                visible_text=True,
                one_pixel_base=True,
                same_transform_stencil=True,
                include_drawing=True,
                drawing_page_numbers=(1,),
                page_count=60,
                page_width=595,
                page_height=842,
                base_device_gray=True,
            )
            furniture = {
                (page_number, 2): SourceNodeKind.HEADER
                for page_number in range(1, 61)
            }
            with (
                mock.patch.object(
                    pdf_adapter_module,
                    "_pdf_variable_page_marker_kinds",
                    return_value=furniture,
                ),
                mock.patch.object(
                    pdf_adapter_module,
                    "_MAX_PDF_VISUAL_RENDER_BYTES",
                    40 * 1024 * 1024,
                ),
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        self.assertIsNot(snapshot.status, SourceIRStatus.FAILED)
        page_render_assets = tuple(
            asset
            for asset in snapshot.assets
            if asset.kind is SourceAssetKind.PAGE_RENDER
        )
        self.assertEqual(len(page_render_assets), 60)
        self.assertEqual(
            sum(
                diagnostic.code == "pdf_mrc_scan_page_composited"
                for diagnostic in snapshot.diagnostics
            ),
            60,
        )
        self.assertNotIn(
            "pdf_resource_budget_exceeded",
            {diagnostic.code for diagnostic in snapshot.diagnostics},
        )
        page_render_source_ids = {
            node.source_node_id
            for node in snapshot.nodes
            if node.asset_ref
            in {asset.asset_id for asset in page_render_assets}
        }
        structure = build_structure_tree(snapshot)
        self.assertTrue(page_render_source_ids)
        self.assertTrue(
            page_render_source_ids.isdisjoint(
                ref.source_id
                for structure_node in structure.nodes
                for ref in structure_node.source_refs
                if ref.kind.value == "source_node"
            )
        )

    def test_mrc_shape_with_visible_native_text_preserves_original_images(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native-mrc-shape.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                visible_text=True,
            )
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(provider.page_inputs, [])
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                2,
            )
            self.assertEqual(
                {asset.kind for asset in snapshot.assets},
                {SourceAssetKind.IMAGE},
            )
            self.assertNotIn(
                "pdf_mrc_scan_page_composited",
                {item.code for item in snapshot.diagnostics},
            )

    def test_one_bit_monochrome_overlay_without_imagemask_is_not_folded(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "monochrome-overlay.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                stencil_image_mask=False,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                2,
            )
            self.assertEqual(len(snapshot.assets), 2)
            self.assertEqual(
                {asset.kind for asset in snapshot.assets},
                {SourceAssetKind.IMAGE},
            )
            self.assertNotIn(
                "pdf_mrc_scan_page_composited",
                {item.code for item in snapshot.diagnostics},
            )

    def test_unfolded_physical_scan_layers_present_as_one_logical_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-unfolded-support.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                stencil_image_mask=False,
                same_transform_stencil=True,
                invisible_text=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        image_source_ids = {
            node.source_node_id
            for node in snapshot.nodes
            if node.kind is SourceNodeKind.IMAGE
        }
        self.assertEqual(len(image_source_ids), 2)
        self.assertNotIn(
            "pdf_mrc_scan_page_composited",
            {diagnostic.code for diagnostic in snapshot.diagnostics},
        )
        structure = build_structure_tree(snapshot)
        self.assertTrue(
            image_source_ids.isdisjoint(
                ref.source_id
                for structure_node in structure.nodes
                for ref in structure_node.source_refs
                if ref.kind.value == "source_node"
            )
        )
        page_view = build_page_comparison_view(
            snapshot,
            structure,
            page_bounds=((0.0, 0.0, 100.0, 100.0),),
        )
        page = page_view["pages"][0]
        supports = [
            block for block in page["blocks"] if block["type"] == "PAGE_RASTER"
        ]
        self.assertEqual(len(supports), 1)
        self.assertEqual(supports[0]["physical_layer_count"], 2)
        self.assertEqual(
            {
                source_ref["source_id"]
                for source_ref in supports[0]["physical_layer_source_refs"]
            },
            image_source_ids,
        )
        self.assertEqual(page["counts"]["content_blocks"], 1)
        self.assertEqual(page["counts"]["support_blocks"], 1)

    def test_full_page_figures_without_raster_proof_remain_separate_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "full-page-figures.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                stencil_image_mask=False,
                same_transform_stencil=True,
                invisible_text=True,
            )
            native_snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

        figure_asset_ids = {
            asset.asset_id
            for asset in native_snapshot.assets
            if asset.kind is SourceAssetKind.IMAGE
        }
        snapshot = native_snapshot.model_copy(
            update={
                "assets": tuple(
                    asset.model_copy(update={"kind": SourceAssetKind.FIGURE})
                    if asset.asset_id in figure_asset_ids
                    else asset
                    for asset in native_snapshot.assets
                ),
                "nodes": tuple(
                    node.model_copy(
                        update={
                            "kind": SourceNodeKind.FIGURE,
                            "extraction_diagnostics": (),
                        }
                    )
                    if node.asset_ref in figure_asset_ids
                    else node
                    for node in native_snapshot.nodes
                ),
                "diagnostics": (),
            }
        )
        structure = build_structure_tree(snapshot)
        page_view = build_page_comparison_view(
            snapshot,
            structure,
            page_bounds=((0.0, 0.0, 100.0, 100.0),),
        )
        page = page_view["pages"][0]
        self.assertEqual(
            [block["type"] for block in page["blocks"]].count("FIGURE"),
            2,
        )
        self.assertEqual(page["counts"]["support_blocks"], 0)
        self.assertEqual(page["counts"]["content_blocks"], 3)

    def test_paint_stencil_before_scan_base_is_not_treated_as_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stencil-underlay.pdf"
            _write_pdf_with_mrc_scan_page(
                source,
                stencil_count=1,
                stencil_before_base=True,
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                2,
            )
            self.assertEqual(len(snapshot.assets), 2)
            self.assertNotIn(
                "pdf_mrc_scan_page_composited",
                {item.code for item in snapshot.diagnostics},
            )

    def test_mrc_4x_page_render_obeys_single_render_resource_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-budget.pdf"
            _write_pdf_with_mrc_scan_page(source, stencil_count=1)

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_VISUAL_RENDER_BYTES",
                1,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(snapshot.assets, ())
            self.assertEqual(
                snapshot.diagnostics[0].code,
                "pdf_resource_budget_exceeded",
            )

    def test_actual_render_peak_cannot_hide_behind_estimate_delta(self) -> None:
        class _LargerThanEstimatedPixmap:
            width = 2
            height = 1

            @staticmethod
            def tobytes(_format: str) -> bytes:
                return b"png"

        class _LargerThanEstimatedPage:
            rect = pymupdf.Rect(0, 0, 1, 1)

            @staticmethod
            def get_pixmap(**_kwargs):
                return _LargerThanEstimatedPixmap()

        page = _LargerThanEstimatedPage()
        for render in (
            lambda budget: pdf_adapter_module._render_pdf_page(
                page=page,
                scale=1.0,
                max_pixels=None,
                resource_budget=budget,
            ),
            lambda budget: pdf_adapter_module._render_pdf_crop(
                page=page,
                bbox=(0.0, 0.0, 1.0, 1.0),
                scale=1.0,
                max_pixels=10,
                resource_budget=budget,
            ),
        ):
            with self.subTest(render=render):
                with (
                    mock.patch.object(
                        pdf_adapter_module,
                        "_MAX_PDF_VISUAL_RENDER_BYTES",
                        6,
                    ),
                    self.assertRaises(
                        pdf_adapter_module._PdfResourceBudgetExceeded
                    ),
                ):
                    render(pdf_adapter_module._PdfResourceBudget())

    def test_mrc_page_above_render_pixel_policy_preserves_original_layers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "mrc-pixel-policy.pdf"
            _write_pdf_with_mrc_scan_page(source, stencil_count=1)

            with mock.patch.object(
                pdf_adapter_module,
                "_MRC_PAGE_RENDER_MAX_PIXELS",
                1,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                2,
            )
            self.assertEqual(len(snapshot.assets), 2)
            self.assertIn(
                "pdf_mrc_page_composite_render_failed",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertNotIn(
                "pdf_mrc_scan_page_composited",
                {item.code for item in snapshot.diagnostics},
            )

    def test_whole_page_image_with_visible_native_text_is_not_sent_remotely(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native-overlay.pdf"
            _write_pdf_with_full_page_image_and_visible_text(source)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(PdfSourceIRAdapter(visual_text_provider=provider),),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(provider.page_inputs, [])
            self.assertEqual(snapshot.visual_text_snapshots, ())
            self.assertNotIn(
                "pdf_full_page_image_without_visible_text",
                {item.code for item in snapshot.diagnostics},
            )

    def test_dense_vector_glyph_page_is_partial_and_uses_scoped_remote_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vector-text.pdf"
            _write_pdf_with_dense_small_fill_paths(source)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=provider,
                        visual_text_page_numbers={1},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            diagnostic_codes = {item.code for item in snapshot.diagnostics}
            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertIn(
                "pdf_vector_text_with_low_native_unicode_coverage",
                diagnostic_codes,
            )
            self.assertNotIn(
                "pdf_full_page_image_without_visible_text",
                diagnostic_codes,
            )
            self.assertIn("pdf_visual_text_observations_captured", diagnostic_codes)
            self.assertEqual(
                tuple(item.page_number for item in provider.page_inputs),
                (1,),
            )
            self.assertEqual(len(snapshot.visual_text_snapshots), 1)

    def test_dense_vector_paths_with_native_cjk_coverage_do_not_trigger_remote_provider(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native-cjk.pdf"
            _write_pdf_with_dense_small_fill_paths(
                source,
                native_cjk_text=True,
            )
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=provider,
                        visual_text_page_numbers={1},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(provider.page_inputs, [])
            self.assertNotIn(
                "pdf_vector_text_with_low_native_unicode_coverage",
                {item.code for item in snapshot.diagnostics},
            )

    def test_sparse_vector_artwork_does_not_trigger_remote_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sparse-vector.pdf"
            _write_pdf_with_dense_small_fill_paths(source, path_count=40)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=provider,
                        visual_text_page_numbers={1},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(provider.page_inputs, [])
            self.assertNotIn(
                "pdf_vector_text_with_low_native_unicode_coverage",
                {item.code for item in snapshot.diagnostics},
            )

    def test_dense_vector_page_outside_scope_is_not_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vector-out-of-scope.pdf"
            _write_pdf_with_dense_small_fill_paths(source)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=provider,
                        visual_text_page_numbers={2},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(provider.page_inputs, [])
            out_of_scope = tuple(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_visual_text_page_out_of_scope"
            )
            self.assertEqual(len(out_of_scope), 1)
            self.assertEqual(out_of_scope[0].native_locator.page_number, 1)

    def test_dense_vector_page_remote_provider_failure_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "vector-provider-error.pdf"
            _write_pdf_with_dense_small_fill_paths(source)

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=_UnavailableVisualTextProvider(),
                        visual_text_page_numbers={1},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.visual_text_snapshots, ())
            diagnostic_codes = {item.code for item in snapshot.diagnostics}
            self.assertIn(
                "pdf_vector_text_with_low_native_unicode_coverage",
                diagnostic_codes,
            )
            self.assertIn("pdf_visual_text_unavailable", diagnostic_codes)

    def test_explicit_visual_page_scope_prevents_unregistered_page_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-scans.pdf"
            _write_pdf_with_full_page_images(source, page_count=2)
            provider = _RecordingVisualTextProvider()

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=provider,
                        visual_text_page_numbers={2},
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(
                [page_input.page_number for page_input in provider.page_inputs],
                [2],
            )
            self.assertEqual(
                [item.page_number for item in snapshot.visual_text_snapshots],
                [2],
            )
            out_of_scope = [
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_visual_text_page_out_of_scope"
            ]
            self.assertEqual(len(out_of_scope), 1)
            self.assertEqual(out_of_scope[0].native_locator.page_number, 1)

    def test_empty_visual_page_scope_needs_no_provider_and_blocks_every_upload(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-scans.pdf"
            _write_pdf_with_full_page_images(source, page_count=2)
            adapter = PdfSourceIRAdapter(
                visual_text_page_numbers=frozenset(),
            )

            snapshot = build_source_ir(
                source,
                adapters=(adapter,),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(adapter.adapter_version, PDF_SOURCE_IR_ADAPTER_VERSION)
            self.assertEqual(snapshot.visual_text_snapshots, ())
            out_of_scope = tuple(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_visual_text_page_out_of_scope"
            )
            self.assertEqual(
                tuple(item.native_locator.page_number for item in out_of_scope),
                (1, 2),
            )
            self.assertNotIn(
                "pdf_visual_text_provider_not_configured",
                {item.code for item in snapshot.diagnostics},
            )

    def test_non_empty_visual_page_scope_still_requires_a_provider(self) -> None:
        with self.assertRaisesRegex(TypeError, "non-empty"):
            PdfSourceIRAdapter(visual_text_page_numbers={1})

    def test_visual_provider_unavailable_is_explicit_and_never_erases_native_text(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hidden-layer.pdf"
            _write_pdf_with_full_page_image_and_invisible_text(source)

            snapshot = build_source_ir(
                source,
                adapters=(
                    PdfSourceIRAdapter(
                        visual_text_provider=_UnavailableVisualTextProvider()
                    ),
                ),
                clock=lambda: CREATED_AT,
            )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.visual_text_snapshots, ())
            self.assertIn(
                "pdf_visual_text_unavailable",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertEqual(
                next(
                    node.raw_text
                    for node in snapshot.nodes
                    if node.kind is SourceNodeKind.PARAGRAPH
                ),
                "hidden OCR layer",
            )

    def test_visual_capture_cancellation_crosses_the_adapter_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "scan.pdf"
            _write_pdf_with_full_page_image(source)

            def cancel_progress(
                _completed: int,
                _authorized_upper_bound: int,
                _page_number: int,
            ) -> None:
                raise LlmShadowPreviewProgressCancelled("job replaced")

            with self.assertRaisesRegex(
                LlmShadowPreviewProgressCancelled,
                "job replaced",
            ):
                build_source_ir(
                    source,
                    adapters=(
                        PdfSourceIRAdapter(
                            visual_text_provider=_VisualCaptureProgressProvider(
                                _RecordingVisualTextProvider(),
                                authorized_upper_bound=1,
                                callback=cancel_progress,
                            ),
                            visual_text_page_numbers={1},
                        ),
                    ),
                    clock=lambda: CREATED_AT,
                )

    def test_default_adapter_only_diagnoses_visual_risk_until_shadow_ocr_is_explicit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hidden-layer.pdf"
            _write_pdf_with_full_page_image_and_invisible_text(source)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.visual_text_snapshots, ())
            self.assertIn(
                "pdf_visual_text_provider_not_configured",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertEqual(
                PdfSourceIRAdapter().adapter_version,
                PDF_SOURCE_IR_ADAPTER_VERSION,
            )
            explicit = PdfSourceIRAdapter(
                visual_text_provider=_RecordingVisualTextProvider()
            )
            self.assertNotEqual(
                explicit.adapter_version,
                PDF_SOURCE_IR_ADAPTER_VERSION,
            )
            self.assertTrue(
                explicit.adapter_version.startswith(
                    f"{PDF_SOURCE_IR_ADAPTER_VERSION}+visual."
                )
            )

    def test_visual_page_render_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "hidden-layer.pdf"
            _write_pdf_with_full_page_image_and_invisible_text(source)

            with mock.patch.object(
                pdf_adapter_module, "_MAX_PDF_VISUAL_RENDER_BYTES", 1
            ):
                snapshot = build_source_ir(
                    source,
                    adapters=(
                        PdfSourceIRAdapter(
                            visual_text_provider=_RecordingVisualTextProvider()
                        ),
                    ),
                    clock=lambda: CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(snapshot.visual_text_snapshots, ())
            self.assertEqual(
                snapshot.diagnostics[0].code, "pdf_resource_budget_exceeded"
            )

    def test_missing_native_bbox_keeps_fact_but_marks_snapshot_partial(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithoutBlockBBox()

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(snapshot.nodes), 1)
            self.assertIsNone(snapshot.nodes[0].native_locator.bbox)
            self.assertIsNone(snapshot.nodes[0].visual_locator)
            self.assertIn(
                "pdf_block_bbox_missing", {item.code for item in snapshot.diagnostics}
            )
            self.assertIn(
                "pdf_table_detector_unavailable",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertTrue(fake_document.closed)

    def test_tiling_pattern_tile_is_not_a_standalone_image_placement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pattern.pdf"
            _write_pdf_with_tiling_pattern_image(source)
            document = pymupdf.open(source)
            try:
                raw_blocks = document[0].get_text("rawdict", sort=False)["blocks"]
                pattern_block = next(
                    block for block in raw_blocks if block.get("type") == 1
                )
                expected_evidence_sha256 = hashlib.sha256(
                    pattern_block["image"]
                ).hexdigest()
            finally:
                document.close()

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.COMPLETE)
            self.assertEqual(
                tuple(node.kind for node in snapshot.nodes),
                (SourceNodeKind.PARAGRAPH,),
            )
            self.assertEqual(snapshot.assets, ())
            dispositions = tuple(
                item
                for item in snapshot.diagnostics
                if item.code == "pdf_tiling_pattern_tile_excluded"
            )
            self.assertEqual(len(dispositions), 1)
            self.assertEqual(dispositions[0].severity.value, "info")
            self.assertIsNone(dispositions[0].source_node_id)
            self.assertEqual(
                dispositions[0].evidence_sha256,
                expected_evidence_sha256,
            )
            self.assertEqual(dispositions[0].native_locator.page_number, 1)
            self.assertEqual(dispositions[0].native_locator.block_index, 1)
            self.assertIsNone(dispositions[0].native_locator.bbox)
            self.assertNotIn(
                "pdf_block_bbox_missing",
                {item.code for item in snapshot.diagnostics},
            )

    def test_pattern_tile_shared_with_visible_image_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "ambiguous-pattern.pdf"
            _write_pdf_with_tiling_pattern_image(source, include_direct_image=True)

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                2,
            )
            self.assertEqual(
                sum(
                    item.code == "pdf_block_bbox_missing"
                    for item in snapshot.diagnostics
                ),
                1,
            )
            self.assertNotIn(
                "pdf_tiling_pattern_tile_excluded",
                {item.code for item in snapshot.diagnostics},
            )

    def test_non_uniform_pattern_image_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "semantic-pattern.pdf"
            _write_pdf_with_tiling_pattern_image(
                source,
                image_samples=(bytes([255, 255, 255]) * 3 + bytes([0, 0, 0])),
            )

            snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                1,
            )
            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(
                sum(
                    item.code == "pdf_block_bbox_missing"
                    for item in snapshot.diagnostics
                ),
                1,
            )
            self.assertNotIn(
                "pdf_tiling_pattern_tile_excluded",
                {item.code for item in snapshot.diagnostics},
            )

    def test_pattern_paint_requires_an_active_pattern_color_space(self) -> None:
        valid_operations = (
            ([NameObject("/Pattern")], b"cs"),
            ([NameObject("/P0")], b"scn"),
            ([], b"f"),
        )
        invalid_operations = (
            ([NameObject("/DeviceRGB")], b"cs"),
            ([NameObject("/P0")], b"scn"),
            ([], b"f"),
        )

        self.assertEqual(
            pdf_adapter_module._pattern_paint_counts(
                operations=valid_operations,
                pattern_names=frozenset({"/P0"}),
                pattern_color_space_names=frozenset({"/Pattern"}),
            ),
            {"/P0": 1},
        )
        self.assertEqual(
            pdf_adapter_module._pattern_paint_counts(
                operations=invalid_operations,
                pattern_names=frozenset({"/P0"}),
                pattern_color_space_names=frozenset({"/Pattern"}),
            ),
            {},
        )

    def test_pattern_analysis_stream_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "over-budget-pattern.pdf"
            _write_pdf_with_tiling_pattern_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_PATTERN_ANALYSIS_STREAM_BYTES",
                0,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertIn(
                "pdf_block_bbox_missing",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertNotIn(
                "pdf_tiling_pattern_tile_excluded",
                {item.code for item in snapshot.diagnostics},
            )

    def test_cross_parser_stream_mismatch_rejects_pattern_proof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "stream-mismatch-pattern.pdf"
            _write_pdf_with_tiling_pattern_image(source)
            source_bytes = source.read_bytes()
            reader = PdfReader(io.BytesIO(source_bytes), strict=False)
            pypdf_page = reader.pages[0]
            document = pymupdf.open(stream=source_bytes, filetype="pdf")
            try:
                page = document[0]
                mismatched_page_document = mock.Mock(wraps=document)
                mismatched_page_document.xref_stream.return_value = b"mismatch"
                self.assertFalse(
                    pdf_adapter_module._pdf_page_content_streams_match(
                        document=mismatched_page_document,
                        page=page,
                        pypdf_page=pypdf_page,
                        budget=pdf_adapter_module._PdfPatternAnalysisBudget(),
                    )
                )

                pattern_reference = pypdf_page["/Resources"]["/Pattern"].raw_get(
                    NameObject("/P0")
                )
                pattern_xref = pattern_reference.idnum
                real_xref_stream = document.xref_stream
                mismatched_pattern_document = mock.Mock(wraps=document)
                mismatched_pattern_document.xref_stream.side_effect = (
                    lambda xref: b"mismatch"
                    if xref == pattern_xref
                    else real_xref_stream(xref)
                )
                self.assertIsNone(
                    pdf_adapter_module._direct_pattern_image_hashes(
                        document=mismatched_pattern_document,
                        reader=reader,
                        pattern_reference=pattern_reference,
                        budget=pdf_adapter_module._PdfPatternAnalysisBudget(),
                        image_evidence_by_xref={},
                        uniform_opaque_by_xref={},
                        pattern_image_hashes_by_xref={},
                    )
                )
            finally:
                document.close()

    def test_pattern_analysis_budget_is_document_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two-page-pattern.pdf"
            _write_pdf_with_tiling_pattern_image(source, page_count=2)
            source_bytes = source.read_bytes()
            document = pymupdf.open(stream=source_bytes, filetype="pdf")
            try:
                page_stream_bytes = sum(
                    len(document.xref_stream(xref))
                    for xref in document[0].get_contents()
                )
                reader = PdfReader(io.BytesIO(source_bytes), strict=False)
                pattern_reference = reader.pages[0]["/Resources"]["/Pattern"].raw_get(
                    NameObject("/P0")
                )
                pattern_stream_bytes = len(
                    document.xref_stream(pattern_reference.idnum)
                )
            finally:
                document.close()

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_PATTERN_ANALYSIS_STREAM_BYTES",
                page_stream_bytes + pattern_stream_bytes,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(
                sum(
                    item.code == "pdf_tiling_pattern_tile_excluded"
                    for item in snapshot.diagnostics
                ),
                1,
            )
            self.assertEqual(
                sum(
                    item.code == "pdf_block_bbox_missing"
                    for item in snapshot.diagnostics
                ),
                1,
            )

    def test_exhausted_pattern_budget_short_circuits_before_stream_access(
        self,
    ) -> None:
        document = mock.Mock()
        budget = pdf_adapter_module._PdfPatternAnalysisBudget(exhausted=True)

        outcome = pdf_adapter_module._direct_pattern_image_hashes(
            document=document,
            reader=mock.Mock(),
            pattern_reference=SimpleNamespace(idnum=17),
            budget=budget,
            image_evidence_by_xref={},
            uniform_opaque_by_xref={},
            pattern_image_hashes_by_xref={},
        )

        self.assertIsNone(outcome)
        document.xref_get_key.assert_not_called()
        document.xref_stream.assert_not_called()

    def test_excluded_pattern_tile_consumes_image_placement_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pattern-placement-budget.pdf"
            _write_pdf_with_tiling_pattern_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_MAX_PDF_IMAGE_PLACEMENTS",
                0,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code,
                "pdf_resource_budget_exceeded",
            )

    def test_optional_pattern_proof_exception_preserves_fail_closed_image(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pattern-proof-error.pdf"
            _write_pdf_with_tiling_pattern_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_proven_tiling_pattern_tile_hashes_by_block_index",
                side_effect=RuntimeError("synthetic parser failure"),
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(
                sum(node.kind is SourceNodeKind.IMAGE for node in snapshot.nodes),
                1,
            )
            self.assertIn(
                "pdf_block_bbox_missing",
                {item.code for item in snapshot.diagnostics},
            )

    def test_optional_pattern_proof_memory_error_uses_resource_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pattern-proof-memory-error.pdf"
            _write_pdf_with_tiling_pattern_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "_proven_tiling_pattern_tile_hashes_by_block_index",
                side_effect=MemoryError,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code,
                "pdf_resource_budget_exceeded",
            )

    def test_pattern_content_stream_memory_error_uses_resource_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "pattern-content-memory-error.pdf"
            _write_pdf_with_tiling_pattern_image(source)

            with mock.patch.object(
                pdf_adapter_module,
                "ContentStream",
                side_effect=MemoryError,
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code,
                "pdf_resource_budget_exceeded",
            )

    def test_non_bytes_non_null_image_mask_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithInvalidMask()

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertIn(
                "pdf_image_mask_invalid_bytes",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertEqual(len(snapshot.assets), 1)
            self.assertTrue(fake_document.closed)

    def test_table_detector_failure_is_partial_without_erasing_raw_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithTableDetectionFailure()

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(len(snapshot.nodes), 1)
            self.assertEqual(snapshot.nodes[0].raw_text, "raw")
            self.assertIn(
                "pdf_table_detection_failed",
                {item.code for item in snapshot.diagnostics},
            )
            self.assertTrue(fake_document.closed)

    def test_native_page_load_failure_is_partial_instead_of_escaping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithPageLoadFailure()

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                return_value=fake_document,
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.PARTIAL)
            self.assertEqual(snapshot.nodes, ())
            self.assertIn(
                "pdf_page_load_failed", {item.code for item in snapshot.diagnostics}
            )
            self.assertTrue(fake_document.closed)

    def test_native_block_budget_exceeded_fails_closed_and_closes_document(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithoutBlockBBox()

            with (
                mock.patch.object(
                    pdf_adapter_module.pymupdf,
                    "open",
                    return_value=fake_document,
                ),
                mock.patch.object(pdf_adapter_module, "_MAX_PDF_BLOCKS_PER_PAGE", 0),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code, "pdf_resource_budget_exceeded"
            )
            self.assertTrue(fake_document.closed)

    def test_page_furniture_prepass_budget_exceeded_fails_closed_and_closes_document(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            fake_document = _FakeDocumentWithoutBlockBBox()

            with (
                mock.patch.object(
                    pdf_adapter_module.pymupdf,
                    "open",
                    return_value=fake_document,
                ),
                mock.patch.object(
                    pdf_adapter_module,
                    "_MAX_PDF_PAGE_FURNITURE_SCAN_BLOCKS",
                    0,
                ),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code, "pdf_resource_budget_exceeded"
            )
            self.assertTrue(fake_document.closed)

    def test_table_cell_budget_is_checked_before_materializing_table_cells(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "table.pdf"
            _write_pdf_with_bordered_table(source)

            with mock.patch.object(pdf_adapter_module, "_MAX_PDF_TABLE_CELLS", 3):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code, "pdf_resource_budget_exceeded"
            )

    def test_unique_embedded_asset_byte_budget_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "image.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_image(pymupdf.Rect(20, 20, 100, 100), stream=_PNG_BYTES)
            document.save(source)
            document.close()

            with mock.patch.object(
                pdf_adapter_module, "_MAX_PDF_UNIQUE_ASSET_BYTES", 0
            ):
                snapshot = build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(
                snapshot.diagnostics[0].code, "pdf_resource_budget_exceeded"
            )

    def test_preflight_rejects_extension_signature_mismatch_before_adapter(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "misdeclared.txt"
            _write_pdf_with_text_and_image(source)

            with self.assertRaises(SourceIRBuildRejected) as caught:
                build_source_ir(source, clock=lambda: CREATED_AT)

            self.assertEqual(
                caught.exception.code,
                SourceIRBuildRejectionCode.PREFLIGHT_REJECTED,
            )
            self.assertIn(
                "extension_signature_mismatch",
                caught.exception.preflight_diagnostic_codes,
            )

    def test_adapter_rejects_source_changed_after_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "changed.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)
            source.write_bytes(source.read_bytes() + b"\n")

            with self.assertRaises(SourceIRAdapterRejected) as caught:
                PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(
                caught.exception.code,
                SourceIRAdapterRejectionCode.SOURCE_CHANGED,
            )

    def test_native_open_failure_returns_failed_snapshot_without_path_leak(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)
            preflight = LocalDocumentPreflight().inspect(source)

            with mock.patch.object(
                pdf_adapter_module.pymupdf,
                "open",
                side_effect=RuntimeError("sensitive parser detail"),
            ):
                snapshot = PdfSourceIRAdapter().extract(
                    source,
                    preflight=preflight,
                    created_at=CREATED_AT,
                )

            self.assertEqual(snapshot.status, SourceIRStatus.FAILED)
            self.assertEqual(snapshot.nodes, ())
            self.assertEqual(snapshot.diagnostics[0].code, "pdf_native_open_failed")
            self.assertNotIn(str(source), snapshot.diagnostics[0].message)
            self.assertNotIn("sensitive parser detail", snapshot.diagnostics[0].message)

    def test_invalid_clock_and_missing_adapter_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            _write_pdf_with_text_and_image(source)

            with self.assertRaises(SourceIRBuildRejected) as invalid_clock:
                build_source_ir(source, clock=lambda: datetime(2026, 7, 20, 9, 30))
            self.assertEqual(
                invalid_clock.exception.code,
                SourceIRBuildRejectionCode.INVALID_TIMESTAMP,
            )

            with self.assertRaises(SourceIRBuildRejected) as missing_adapter:
                build_source_ir(source, adapters=(), clock=lambda: CREATED_AT)
            self.assertEqual(
                missing_adapter.exception.code,
                SourceIRBuildRejectionCode.ADAPTER_UNAVAILABLE,
            )


class _FakeRectangle:
    x0 = 0.0
    y0 = 0.0
    x1 = 100.0
    y1 = 100.0


class _FakePageWithoutBlockBBox:
    rect = _FakeRectangle()

    def get_text(self, mode: str, *, sort: bool) -> dict[str, object]:
        if mode != "rawdict" or sort:
            raise AssertionError("adapter must request unsorted rawdict")
        return {
            "blocks": [
                {
                    "type": 0,
                    "lines": [
                        {
                            "spans": [
                                {
                                    "chars": [
                                        {"c": "r"},
                                        {"c": "a"},
                                        {"c": "w"},
                                    ]
                                }
                            ]
                        }
                    ],
                }
            ]
        }


class _FakePageWithTableDetectionFailure(_FakePageWithoutBlockBBox):
    def find_tables(self) -> object:
        raise RuntimeError("table detector unavailable")


class _FakePageWithInvalidMask(_FakePageWithoutBlockBBox):
    def get_text(self, mode: str, *, sort: bool) -> dict[str, object]:
        if mode != "rawdict" or sort:
            raise AssertionError("adapter must request unsorted rawdict")
        return {
            "blocks": [
                {
                    "type": 1,
                    "bbox": (10.0, 10.0, 50.0, 50.0),
                    "width": 2,
                    "height": 2,
                    "ext": "png",
                    "image": _PNG_BYTES,
                    "mask": b"",
                }
            ]
        }


class _FakeTableCandidate:
    def __init__(
        self,
        *,
        bbox: tuple[float, float, float, float],
        row_count: int,
        column_count: int,
        marker: str,
    ) -> None:
        self.bbox = bbox
        self.row_count = row_count
        self.col_count = column_count
        x0, y0, x1, y1 = bbox
        cell_width = (x1 - x0) / column_count
        cell_height = (y1 - y0) / row_count
        self.rows = tuple(
            SimpleNamespace(
                cells=tuple(
                    (
                        x0 + column * cell_width,
                        y0 + row * cell_height,
                        x0 + (column + 1) * cell_width,
                        y0 + (row + 1) * cell_height,
                    )
                    for column in range(column_count)
                )
            )
            for row in range(row_count)
        )
        self._matrix = tuple(
            tuple(f"{marker}-{row}-{column}" for column in range(column_count))
            for row in range(row_count)
        )

    def extract(self) -> tuple[tuple[str, ...], ...]:
        return self._matrix


def _fake_quadrilateral_figure_table_candidate(
    *,
    bbox: tuple[float, float, float, float],
) -> object:
    x0, y0, x1, y1 = bbox
    middle_x = (x0 + x1) / 2.0
    middle_y = (y0 + y1) / 2.0
    return SimpleNamespace(
        bbox=bbox,
        row_count=2,
        col_count=2,
        rows=(
            SimpleNamespace(
                cells=((x0, y0, x1, middle_y), None),
            ),
            SimpleNamespace(
                cells=(
                    (x0, middle_y, middle_x, y1),
                    (middle_x, middle_y, x1, y1),
                ),
            ),
        ),
        extract=lambda: (("header", None), ("left", "right")),
    )


def _fake_merged_table_candidate(*, anchor_right: float) -> object:
    rows = (
        SimpleNamespace(
            bbox=(0.0, 0.0, 90.0, 20.0),
            cells=((0.0, 0.0, anchor_right, 40.0), None, (60.0, 0.0, 90.0, 20.0)),
        ),
        SimpleNamespace(
            bbox=(0.0, 20.0, 90.0, 40.0),
            cells=(None, None, (60.0, 20.0, 90.0, 40.0)),
        ),
        SimpleNamespace(
            bbox=(0.0, 40.0, 90.0, 60.0),
            cells=(
                (0.0, 40.0, 30.0, 60.0),
                (30.0, 40.0, 60.0, 60.0),
                (60.0, 40.0, 90.0, 60.0),
            ),
        ),
    )
    matrix = (
        ("merged", None, "top-right"),
        (None, None, "middle-right"),
        ("bottom-left", "bottom-middle", "bottom-right"),
    )
    return SimpleNamespace(
        bbox=(0.0, 0.0, 90.0, 60.0),
        row_count=3,
        col_count=3,
        rows=rows,
        extract=lambda: matrix,
    )


def _fake_captioned_table_candidate(
    *,
    bbox: tuple[float, float, float, float],
    body_row_count: int,
    column_count: int,
    marker: str,
) -> object:
    x0, y0, x1, y1 = bbox
    caption_height = 10.0
    body_y0 = y0 + caption_height
    cell_width = (x1 - x0) / column_count
    cell_height = (y1 - body_y0) / body_row_count
    caption_cells = ((x0, y0, x1, body_y0),) + (None,) * (column_count - 1)
    rows = [SimpleNamespace(bbox=(x0, y0, x1, body_y0), cells=caption_cells)]
    matrix = [["caption"] + [""] * (column_count - 1)]
    for row_index in range(body_row_count):
        row_y0 = body_y0 + row_index * cell_height
        row_y1 = body_y0 + (row_index + 1) * cell_height
        rows.append(
            SimpleNamespace(
                bbox=(x0, row_y0, x1, row_y1),
                cells=tuple(
                    (
                        x0 + column_index * cell_width,
                        row_y0,
                        x0 + (column_index + 1) * cell_width,
                        row_y1,
                    )
                    for column_index in range(column_count)
                ),
            )
        )
        matrix.append(
            [
                f"{marker}-{row_index}-{column_index}"
                for column_index in range(column_count)
            ]
        )
    return SimpleNamespace(
        bbox=bbox,
        row_count=body_row_count + 1,
        col_count=column_count,
        rows=tuple(rows),
        extract=lambda: tuple(tuple(row) for row in matrix),
    )


def _fake_ruled_first_row_bboxlog(
    *,
    bbox: tuple[float, float, float, float],
) -> tuple[object, ...]:
    x0, y0, x1, y1 = bbox
    return (
        ("fill-path", (x0, y0 - 0.5, x1, y0 + 0.5)),
        ("fill-path", (x0, y1 - 0.5, x1, y1 + 0.5)),
        ("fill-path", (x0 + 2.0, y0 + 2.0, x0 + 5.0, y0 + 6.0)),
    )


def _fake_ruled_axis_tradeoff_candidates(
    *,
    strict_character_count: int = 17,
) -> tuple[object, object, tuple[object, ...]]:
    default_candidate = _FakeTableCandidate(
        bbox=(20.0, 20.0, 80.0, 80.0),
        row_count=10,
        column_count=2,
        marker="default",
    )
    default_candidate._matrix = (("A", ""),) * 9 + (("", ""),)

    strict_candidate = _FakeTableCandidate(
        bbox=(10.0, 10.0, 90.0, 80.0),
        row_count=5,
        column_count=4,
        marker="strict",
    )
    x0, y0, x1, y1 = strict_candidate.bbox
    row_height = (y1 - y0) / strict_candidate.row_count
    rows = []
    for row_index, native_row in enumerate(strict_candidate.rows):
        row_y0 = y0 + row_index * row_height
        row_y1 = row_y0 + row_height
        cells = native_row.cells
        if row_index == 0:
            cells = ((x0, row_y0, x1, row_y1), None, None, None)
        rows.append(SimpleNamespace(bbox=(x0, row_y0, x1, row_y1), cells=cells))
    strict_candidate.rows = tuple(rows)
    remaining_characters = strict_character_count
    strict_matrix: list[tuple[str, ...]] = []
    for row_index in range(strict_candidate.row_count):
        row: list[str] = []
        for column_index in range(strict_candidate.col_count):
            if row_index == 0 and column_index > 0:
                row.append("")
                continue
            if remaining_characters > 0:
                row.append("A")
                remaining_characters -= 1
            else:
                row.append("")
        strict_matrix.append(tuple(row))
    strict_candidate._matrix = tuple(strict_matrix)
    page_bboxlog = _fake_ruled_first_row_bboxlog(
        bbox=(x0, y0, x1, y0 + row_height),
    )
    return default_candidate, strict_candidate, page_bboxlog


def _fake_two_row_strict_table_candidate(
    *,
    incompatible_merged_text: bool = False,
) -> object:
    bbox = (10.0, 10.0, 90.0, 50.0)
    return SimpleNamespace(
        bbox=bbox,
        row_count=2,
        col_count=3,
        rows=(
            SimpleNamespace(
                bbox=(10.0, 10.0, 90.0, 25.0),
                cells=((10.0, 10.0, 90.0, 25.0), None, None),
            ),
            SimpleNamespace(
                bbox=(10.0, 25.0, 90.0, 50.0),
                cells=(
                    (10.0, 25.0, 40.0, 50.0),
                    (40.0, 25.0, 65.0, 50.0),
                    (65.0, 25.0, 90.0, 50.0),
                ),
            ),
        ),
        extract=lambda: (
            ("header", "phantom" if incompatible_merged_text else "", ""),
            ("left", "middle", ""),
        ),
    )


def _fake_two_row_table_bboxlog() -> tuple[object, ...]:
    return (
        ("fill-path", (10.0, 9.5, 90.0, 10.5)),
        ("fill-path", (10.0, 24.5, 50.0, 25.5)),
        ("fill-path", (50.0, 24.5, 90.0, 25.5)),
        ("fill-path", (10.0, 49.5, 45.0, 50.5)),
        ("fill-path", (45.5, 49.5, 90.0, 50.5)),
    )


def _fake_mixed_layout_parent_candidate() -> object:
    row_boundaries = tuple(float(index * 3) for index in range(12)) + (65.0,)
    rows = []
    matrix = []
    for row_index in range(12):
        y0 = row_boundaries[row_index]
        y1 = row_boundaries[row_index + 1]
        rows.append(
            SimpleNamespace(
                bbox=(0.0, y0, 100.0, y1),
                cells=tuple(
                    (
                        float(column_index * 20),
                        y0,
                        float((column_index + 1) * 20),
                        y1,
                    )
                    for column_index in range(5)
                ),
            )
        )
        if 3 <= row_index <= 5:
            matrix.append(
                [""]
                + [f"dense-{row_index}-{column_index}" for column_index in range(1, 5)]
            )
        else:
            matrix.append([f"paragraph-{row_index}", "", "", "", ""])
    return SimpleNamespace(
        bbox=(0.0, 0.0, 100.0, 65.0),
        row_count=12,
        col_count=5,
        rows=tuple(rows),
        extract=lambda: tuple(tuple(row) for row in matrix),
    )


class _FakePageWithTableCandidates:
    rect = _FakeRectangle()

    def __init__(
        self,
        *,
        default_candidates: tuple[object, ...],
        strict_candidates: tuple[object, ...],
        bboxlog: tuple[object, ...] = (),
        raw_blocks: tuple[dict[str, object], ...] | None = None,
    ) -> None:
        self._default_candidates = default_candidates
        self._strict_candidates = strict_candidates
        self._bboxlog = bboxlog
        self._raw_blocks = raw_blocks
        self.strategies: list[str | None] = []

    def get_text(self, mode: str, *, sort: bool) -> dict[str, object]:
        if mode != "rawdict" or sort:
            raise AssertionError("adapter must request unsorted rawdict")
        if self._raw_blocks is not None:
            return {"blocks": list(self._raw_blocks)}
        return {
            "blocks": [
                {
                    "type": 0,
                    "bbox": (2.0, 2.0, 18.0, 8.0),
                    "lines": [
                        {
                            "spans": [
                                {
                                    "alpha": 255,
                                    "chars": [
                                        {"c": "r"},
                                        {"c": "a"},
                                        {"c": "w"},
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ]
        }

    def get_bboxlog(self) -> list[object]:
        return list(self._bboxlog)

    def find_tables(self, *, strategy: str | None = None) -> object:
        self.strategies.append(strategy)
        if strategy is None:
            return SimpleNamespace(tables=self._default_candidates)
        if strategy == "lines_strict":
            return SimpleNamespace(tables=self._strict_candidates)
        raise AssertionError(strategy)


def _fake_table_suppression_text_block(
    *,
    bbox: tuple[float, float, float, float],
    text: str,
    watermark: bool = False,
) -> dict[str, object]:
    line: dict[str, object] = {
        "spans": [
            {
                "alpha": 100 if watermark else 255,
                "chars": [{"c": character} for character in text],
            }
        ]
    }
    if watermark:
        line["dir"] = (0.8, -0.6)
    return {
        "type": 0,
        "bbox": bbox,
        "lines": [line],
    }


class _FakeDocumentWithPage:
    needs_pass = False
    page_count = 1

    def __init__(self, page: _FakePageWithTableCandidates) -> None:
        self._page = page
        self.closed = False

    def load_page(self, page_index: int) -> _FakePageWithTableCandidates:
        if page_index != 0:
            raise IndexError(page_index)
        return self._page

    def close(self) -> None:
        self.closed = True


class _FakeDocumentWithoutBlockBBox:
    needs_pass = False
    page_count = 1

    def __init__(self) -> None:
        self.closed = False

    def load_page(self, page_index: int) -> _FakePageWithoutBlockBBox:
        if page_index != 0:
            raise IndexError(page_index)
        return _FakePageWithoutBlockBBox()

    def close(self) -> None:
        self.closed = True


class _FakeDocumentWithTableDetectionFailure(_FakeDocumentWithoutBlockBBox):
    def load_page(self, page_index: int) -> _FakePageWithTableDetectionFailure:
        if page_index != 0:
            raise IndexError(page_index)
        return _FakePageWithTableDetectionFailure()


class _FakeDocumentWithInvalidMask(_FakeDocumentWithoutBlockBBox):
    def load_page(self, page_index: int) -> _FakePageWithInvalidMask:
        if page_index != 0:
            raise IndexError(page_index)
        return _FakePageWithInvalidMask()


class _FakeDocumentWithPageLoadFailure:
    needs_pass = False
    page_count = 2

    def __init__(self) -> None:
        self.closed = False

    def load_page(self, page_index: int) -> _FakePageWithoutBlockBBox:
        raise RuntimeError(f"page {page_index} is unavailable")

    def close(self) -> None:
        self.closed = True


class _RecordingVisualTextProvider:
    provider_id = "test-visual-text"
    provider_version = "1.0.0"

    def __init__(self) -> None:
        self.page_inputs: list[VisualTextPageInput] = []

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        self.page_inputs.append(page_input)
        page_x0, page_y0, page_x1, page_y1 = page_input.page_bbox
        bbox = (
            page_x0 + (page_x1 - page_x0) * 0.1,
            page_y0 + (page_y1 - page_y0) * 0.1,
            page_x0 + (page_x1 - page_x0) * 0.5,
            page_y0 + (page_y1 - page_y0) * 0.2,
        )
        observation = VisualTextObservation(
            observation_id=stable_visual_text_observation_id(
                source_sha256=page_input.source_sha256,
                page_image_sha256=page_input.page_image_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=page_input.page_number,
                observation_index=0,
                bbox=bbox,
            ),
            observation_index=0,
            source_sha256=page_input.source_sha256,
            page_number=page_input.page_number,
            bbox=bbox,
            raw_text="visual page fact",
            confidence=0.91,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_image_sha256=page_input.page_image_sha256,
        )
        return VisualTextSnapshot(
            visual_text_snapshot_id=stable_visual_text_snapshot_id(
                source_sha256=page_input.source_sha256,
                page_image_sha256=page_input.page_image_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=page_input.page_number,
                page_bbox=page_input.page_bbox,
                page_image_width_px=page_input.page_image_width_px,
                page_image_height_px=page_input.page_image_height_px,
            ),
            source_sha256=page_input.source_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=page_input.page_number,
            page_bbox=page_input.page_bbox,
            page_image_sha256=page_input.page_image_sha256,
            page_image_width_px=page_input.page_image_width_px,
            page_image_height_px=page_input.page_image_height_px,
            observations=(observation,),
        )


class _UnavailableVisualTextProvider:
    provider_id = "unavailable-visual-text"
    provider_version = "1.0.0"

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        raise VisualTextProviderRejected(
            VisualTextProviderRejectionCode.UNAVAILABLE,
            "The optional test provider is unavailable.",
        )


class _RecordingFullLayoutVisualTextProvider(_RecordingVisualTextProvider):
    provider_id = "test-full-layout"

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        snapshot = super().recognize_page(page_input)
        return VisualTextSnapshot(
            **{
                **snapshot.model_dump(mode="python"),
                "evaluated_content_types": (
                    "TEXT",
                    "TABLE",
                    "FIGURE",
                    "HEADER",
                    "FOOTER",
                    "WATERMARK",
                ),
            }
        )


def _write_single_page_pdf(path: Path) -> None:
    document = pymupdf.open()
    document.new_page()
    document.save(path)
    document.close()


def _write_pdf_with_text_and_image(path: Path) -> None:
    document = pymupdf.open()
    text_page = document.new_page()
    text_page.insert_text((72, 72), "raw   text")
    text_page.insert_text((72, 300), "raw   text")
    image_page = document.new_page()
    image_page.insert_image(pymupdf.Rect(20, 20, 100, 100), stream=_PNG_BYTES)
    document.save(path)
    document.close()


def _write_pdf_with_repeated_native_text(
    path: Path,
    *,
    texts: tuple[str, ...],
    baseline_y: float,
) -> None:
    document = pymupdf.open()
    for page_number, text in enumerate(texts, start=1):
        page = document.new_page(width=400, height=600)
        page.insert_text((40, 120), f"Body {page_number}", fontsize=10)
        page.insert_text((40, baseline_y), text, fontsize=9)
    document.save(path)
    document.close()


def _write_pdf_with_repeated_margin_image(path: Path) -> None:
    document = pymupdf.open()
    for page_number in range(1, 5):
        page = document.new_page(width=600, height=800)
        page.insert_image(
            pymupdf.Rect(48, 16, 128, 56),
            stream=_PNG_BYTES,
        )
        page.insert_text((72, 140), f"{page_number} body requirement")
        if page_number == 2:
            # Reusing the exact same asset bytes in the body must not let the
            # repeated-header proof erase a legitimate content placement.
            page.insert_image(
                pymupdf.Rect(240, 280, 320, 320),
                stream=_PNG_BYTES,
            )
    document.save(path)
    document.close()


def _write_pdf_with_separate_current_and_total_page_numbers(path: Path) -> None:
    document = pymupdf.open()
    for page_number in range(1, 5):
        page = document.new_page(width=400, height=600)
        page.insert_text((40, 120), f"Body {page_number}", fontsize=10)
        page.insert_text((185, 550), str(page_number), fontsize=9)
        page.insert_text((184, 580), "28", fontsize=9)
    document.save(path)
    document.close()


def _write_pdf_with_rotated_page_numbers(path: Path, *, rotation: int) -> None:
    document = pymupdf.open()
    for page_number in range(1, 5):
        page = document.new_page(width=400, height=600)
        page.set_rotation(rotation)
        visual_baseline = pymupdf.Point(
            page.rect.width / 2.0,
            page.rect.height - 20.0,
        )
        native_baseline = visual_baseline * page.derotation_matrix
        page.insert_text(native_baseline, str(page_number), fontsize=9)
    document.save(path)
    document.close()


def _write_pdf_with_mrc_scan_page(
    path: Path,
    *,
    stencil_count: int,
    visible_text: bool = False,
    stencil_image_mask: bool = True,
    stencil_before_base: bool = False,
    invisible_text: bool = False,
    one_pixel_base: bool = False,
    same_transform_stencil: bool = False,
    include_drawing: bool = False,
    page_count: int = 1,
    page_width: int = 100,
    page_height: int = 100,
    rotation: int = 0,
    base_device_gray: bool = False,
    drawing_kind: str = "small_white_rectangle",
    drawing_page_numbers: tuple[int, ...] | None = None,
) -> None:
    """Write a scan raster with explicit 1-bit paint-stencil occurrences."""

    if stencil_count < 1:
        raise ValueError("stencil_count must be positive")
    if page_count < 1:
        raise ValueError("page_count must be positive")
    if page_width < 1 or page_height < 1:
        raise ValueError("page dimensions must be positive")
    if rotation not in {0, 90, 180, 270}:
        raise ValueError("rotation must be a right angle")
    writer = PdfWriter()

    base_image = DecodedStreamObject()
    base_size = 1 if one_pixel_base else 10
    base_samples = (
        bytes([224])
        if base_device_gray
        else bytes([224, 224, 224])
    )
    base_image.set_data(base_samples * (base_size * base_size))
    base_image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(base_size),
            NameObject("/Height"): NumberObject(base_size),
            NameObject("/ColorSpace"): NameObject(
                "/DeviceGray" if base_device_gray else "/DeviceRGB"
            ),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    base_reference = writer._add_object(base_image)

    stencil_image = DecodedStreamObject()
    stencil_image.set_data(bytes([0b10101010, 0b01010101]) * 4)
    stencil_dictionary = {
        NameObject("/Type"): NameObject("/XObject"),
        NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(8),
        NameObject("/Height"): NumberObject(8),
        NameObject("/BitsPerComponent"): NumberObject(1),
    }
    if stencil_image_mask:
        stencil_dictionary[NameObject("/ImageMask")] = BooleanObject(True)
        stencil_dictionary[NameObject("/Decode")] = ArrayObject(
            [NumberObject(0), NumberObject(1)]
        )
    else:
        stencil_dictionary[NameObject("/ColorSpace")] = NameObject("/DeviceGray")
    stencil_image.update(stencil_dictionary)
    stencil_reference = writer._add_object(stencil_image)

    font_reference = None
    if visible_text or invisible_text:
        font_reference = writer._add_object(
            DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
        )

    base_command = (
        f"q {page_width} 0 0 {page_height} 0 0 cm /Base Do Q\n"
    ).encode("ascii")
    stencil_commands = b"".join(
        (
            f"0 g q {page_width} 0 0 {page_height} 0 0 cm /Stencil Do Q\n"
            if same_transform_stencil
            else (
                f"0 g q {page_width * (0.8 - index * 0.2)} 0 0 "
                f"{page_height * (0.8 - index * 0.2)} "
                f"{10 + index * 10} {10 + index * 10} cm /Stencil Do Q\n"
            )
        ).encode("ascii")
        for index in range(stencil_count)
    )
    image_commands = (
        stencil_commands + base_command
        if stencil_before_base
        else base_command + stencil_commands
    )
    text_command = (
        (
            f"BT /F1 12 Tf 3 Tr 10 {page_height - 10} Td "
            "(hidden native text) Tj ET\n"
        ).encode("ascii")
        if invisible_text
        else (
            f"BT /F1 12 Tf 10 {page_height - 10} Td "
            "(trusted native text) Tj ET\n"
        ).encode("ascii")
        if visible_text
        else b""
    )
    drawing_commands = {
        "small_white_rectangle": b"1 1 1 rg 20 20 20 20 re f\n",
        "small_gray_rectangle": b"0.5 0.5 0.5 rg 20 20 20 20 re f\n",
        "large_white_rectangle": (
            f"1 1 1 rg 0 0 {page_width} {page_height} re f\n"
        ).encode("ascii"),
        "white_triangle": b"1 1 1 rg 20 20 m 40 20 l 30 40 l h f\n",
    }
    if drawing_kind not in drawing_commands:
        raise ValueError("unsupported drawing kind")
    selected_drawing_pages = set(
        drawing_page_numbers
        if drawing_page_numbers is not None
        else range(1, page_count + 1)
    )
    for page_number in range(1, page_count + 1):
        page = writer.add_blank_page(width=page_width, height=page_height)
        if rotation:
            page[NameObject("/Rotate")] = NumberObject(rotation)
        drawing_command = (
            drawing_commands[drawing_kind]
            if include_drawing and page_number in selected_drawing_pages
            else b""
        )
        content = DecodedStreamObject()
        content.set_data(image_commands + drawing_command + text_command)
        content_reference = writer._add_object(content)
        resources = DictionaryObject(
            {
                NameObject("/XObject"): DictionaryObject(
                    {
                        NameObject("/Base"): base_reference,
                        NameObject("/Stencil"): stencil_reference,
                    }
                )
            }
        )
        if font_reference is not None:
            resources[NameObject("/Font")] = DictionaryObject(
                {NameObject("/F1"): font_reference}
            )
        page[NameObject("/Contents")] = content_reference
        page[NameObject("/Resources")] = resources

    with path.open("wb") as handle:
        writer.write(handle)


def _write_pdf_with_tiling_pattern_image(
    path: Path,
    *,
    include_direct_image: bool = False,
    image_samples: bytes | None = None,
    page_count: int = 1,
) -> None:
    """Write a minimal real tiling pattern that MuPDF exposes as an image block."""

    writer = PdfWriter()
    pages = tuple(
        writer.add_blank_page(width=100, height=100) for _ in range(page_count)
    )

    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)

    image = DecodedStreamObject()
    image.set_data(image_samples or bytes([255, 255, 255]) * 4)
    image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(2),
            NameObject("/Height"): NumberObject(2),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
        }
    )
    image_reference = writer._add_object(image)

    pattern = DecodedStreamObject()
    pattern.set_data(b"q 2 0 0 2 0 0 cm /Im0 Do Q\n")
    pattern.update(
        {
            NameObject("/Type"): NameObject("/Pattern"),
            NameObject("/PatternType"): NumberObject(1),
            NameObject("/PaintType"): NumberObject(1),
            NameObject("/TilingType"): NumberObject(1),
            NameObject("/BBox"): ArrayObject(
                [NumberObject(0), NumberObject(0), NumberObject(2), NumberObject(2)]
            ),
            NameObject("/XStep"): NumberObject(2),
            NameObject("/YStep"): NumberObject(2),
            NameObject("/Matrix"): ArrayObject(
                [
                    FloatObject(0.24),
                    NumberObject(0),
                    NumberObject(0),
                    FloatObject(0.24),
                    NumberObject(0),
                    NumberObject(100),
                ]
            ),
            NameObject("/Resources"): DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im0"): image_reference}
                    )
                }
            ),
        }
    )
    pattern_reference = writer._add_object(pattern)

    content = DecodedStreamObject()
    direct_image_command = (
        b"q 10 0 0 10 70 70 cm /ImDirect Do Q\n" if include_direct_image else b""
    )
    content.set_data(
        b"BT /F1 12 Tf 10 80 Td (fixture text) Tj ET\n"
        + direct_image_command
        + b"/Pattern cs /P0 scn 10 10 20 20 re f\n"
    )
    content_reference = writer._add_object(content)
    resources = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference}),
            NameObject("/Pattern"): DictionaryObject(
                {NameObject("/P0"): pattern_reference}
            ),
        }
    )
    if include_direct_image:
        resources[NameObject("/XObject")] = DictionaryObject(
            {NameObject("/ImDirect"): image_reference}
        )
    for page in pages:
        page[NameObject("/Contents")] = content_reference
        page[NameObject("/Resources")] = resources

    with path.open("wb") as handle:
        writer.write(handle)


def _png_bytes_from_pixels(
    mode: str,
    pixels: list[int] | list[tuple[int, int, int, int]],
) -> bytes:
    image = PILImage.new(mode, (2, 2))
    image.putdata(pixels)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _write_pdf_with_alpha_image(path: Path) -> None:
    pixmap = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 4, 4),
        True,
    )
    pixmap.clear_with(0x336699)
    pixmap.set_alpha(bytes([0, 64, 128, 255] * 4))
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(
        pymupdf.Rect(20, 20, 100, 100),
        stream=bytes(pixmap.tobytes("png")),
    )
    document.save(path)
    document.close()


def _write_pdf_with_inline_header_fragments(
    path: Path,
    *,
    exact_wrappers: bool,
) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    scanline = bytes([51] * 18)
    content_parts: list[bytes] = []
    for occurrence_index in range(128):
        x = 20.0 + (occurrence_index % 16) * 1.8
        y = 820.0 - (occurrence_index // 16) * 0.3
        content_parts.extend(
            (
                b"q\n",
                f"1.44 0 0 0.24 {x:.3f} {y:.3f} cm\n".encode("ascii"),
            )
        )
        if not exact_wrappers:
            content_parts.append(b"n\n")
        content_parts.extend(
            (
                b"BI /CS /RGB /W 6 /H 1 /BPC 8 ID\n",
                scanline,
                b"\nEI\nQ\n",
            )
        )
    content = DecodedStreamObject()
    content.set_data(b"".join(content_parts))
    page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_pdf_with_explicit_masks(
    path: Path,
    *,
    mask_values: tuple[int, ...],
) -> None:
    base = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 4, 4),
        False,
    )
    base.clear_with(0x336699)
    base_bytes = bytes(base.tobytes("png"))
    document = pymupdf.open()
    for value in mask_values:
        mask = pymupdf.Pixmap(
            pymupdf.csGRAY,
            pymupdf.IRect(0, 0, 4, 4),
            False,
        )
        mask.clear_with(value)
        page = document.new_page()
        page.insert_image(
            pymupdf.Rect(20, 20, 100, 100),
            stream=base_bytes,
            mask=bytes(mask.tobytes("png")),
        )
    document.save(path)
    document.close()


def _write_pdf_with_matte_soft_mask(
    path: Path,
    *,
    mask_values: tuple[int, ...],
    matte_values: tuple[int, int, int] = (0, 0, 0),
) -> int:
    """Write one direct RGB image with an 8-bit SMask and explicit `/Matte`."""

    if len(mask_values) != 16 or any(
        isinstance(value, bool) or value < 0 or value > 255 for value in mask_values
    ):
        raise ValueError("fixture requires sixteen 8-bit mask samples")
    if any(isinstance(value, bool) or value not in (0, 1) for value in matte_values):
        raise ValueError("fixture matte values must be zero or one")
    writer = PdfWriter()
    page = writer.add_blank_page(width=100, height=100)

    soft_mask = DecodedStreamObject()
    soft_mask.set_data(bytes(mask_values))
    soft_mask.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(4),
            NameObject("/Height"): NumberObject(4),
            NameObject("/ColorSpace"): NameObject("/DeviceGray"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/Matte"): ArrayObject(
                [NumberObject(value) for value in matte_values]
            ),
        }
    )
    soft_mask_reference = writer._add_object(soft_mask)

    base_image = DecodedStreamObject()
    foreground = (0x33, 0x66, 0x99)
    matte_samples = tuple(value * 255 for value in matte_values)
    premultiplied_samples = bytes(
        (foreground_component * alpha + matte_component * (255 - alpha) + 127) // 255
        for alpha in mask_values
        for foreground_component, matte_component in zip(
            foreground,
            matte_samples,
            strict=True,
        )
    )
    base_image.set_data(premultiplied_samples)
    base_image.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Image"),
            NameObject("/Width"): NumberObject(4),
            NameObject("/Height"): NumberObject(4),
            NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
            NameObject("/BitsPerComponent"): NumberObject(8),
            NameObject("/SMask"): soft_mask_reference,
        }
    )
    base_reference = writer._add_object(base_image)

    content = DecodedStreamObject()
    content.set_data(b"q 80 0 0 80 10 10 cm /Im0 Do Q\n")
    content_reference = writer._add_object(content)
    page[NameObject("/Contents")] = content_reference
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): base_reference})}
    )

    with path.open("wb") as handle:
        writer.write(handle)
    return base_reference.idnum


def _write_pdf_with_higher_resolution_mask(path: Path) -> None:
    base = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 4, 4),
        False,
    )
    base.clear_with(0x336699)
    mask = pymupdf.Pixmap(
        pymupdf.csGRAY,
        pymupdf.IRect(0, 0, 16, 16),
        False,
    )
    mask.clear_with(0)
    for coordinate in range(16):
        mask.set_pixel(coordinate, coordinate, (255,))
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(
        pymupdf.Rect(20, 20, 100, 100),
        stream=bytes(base.tobytes("png")),
        mask=bytes(mask.tobytes("png")),
    )
    document.save(path)
    document.close()


def _write_pdf_with_lower_resolution_mask(path: Path) -> None:
    base = pymupdf.Pixmap(
        pymupdf.csRGB,
        pymupdf.IRect(0, 0, 16, 16),
        False,
    )
    base.clear_with(0x336699)
    mask = pymupdf.Pixmap(
        pymupdf.csGRAY,
        pymupdf.IRect(0, 0, 4, 4),
        False,
    )
    mask.clear_with(192)
    document = pymupdf.open()
    page = document.new_page()
    page.insert_image(
        pymupdf.Rect(20, 20, 100, 100),
        stream=bytes(base.tobytes("png")),
        mask=bytes(mask.tobytes("png")),
    )
    document.save(path)
    document.close()


def _write_pdf_with_full_page_image_and_invisible_text(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_image(page.rect, stream=_PNG_BYTES)
    page.insert_text((20, 40), "hidden OCR layer", render_mode=3)
    document.save(path)
    document.close()


def _write_pdf_with_full_page_image(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_image(page.rect, stream=_PNG_BYTES)
    document.save(path)
    document.close()


def _write_pdf_with_full_page_images(path: Path, *, page_count: int) -> None:
    document = pymupdf.open()
    for _ in range(page_count):
        page = document.new_page(width=200, height=200)
        page.insert_image(page.rect, stream=_PNG_BYTES)
    document.save(path)
    document.close()


def _write_pdf_with_full_page_image_and_visible_text(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=200, height=200)
    page.insert_image(page.rect, stream=_PNG_BYTES)
    page.insert_text((20, 40), "visible native layer")
    document.save(path)
    document.close()


def _write_pdf_with_dense_small_fill_paths(
    path: Path,
    *,
    path_count: int = 140,
    native_cjk_text: bool = False,
) -> None:
    document = pymupdf.open()
    page = document.new_page(width=240, height=240)
    for index in range(path_count):
        column = index % 14
        row = index // 14
        x0 = 10 + column * 14
        y0 = 20 + row * 14
        page.draw_rect(
            pymupdf.Rect(x0, y0, x0 + 4, y0 + 4),
            color=None,
            fill=(0, 0, 0),
        )
    if native_cjk_text:
        page.insert_text(
            (10, 220),
            "测试文本" * 20,
            fontname="china-s",
            fontsize=8,
        )
    else:
        page.insert_text((10, 220), "native text", fontsize=8)
    document.save(path)
    document.close()


def _write_pdf_with_fragmented_image_mosaic(
    path: Path,
    *,
    include_oversized_strip: bool = False,
) -> None:
    strip_image = PILImage.new("RGB", (4, 1))
    strip_image.putdata(
        [
            (0, 0, 0),
            (64, 96, 128),
            (128, 96, 64),
            (0, 0, 0),
        ]
    )
    strip_output = io.BytesIO()
    strip_image.save(strip_output, format="PNG")
    strip_bytes = strip_output.getvalue()

    document = pymupdf.open()
    page = document.new_page(width=320, height=240)
    for row in range(50):
        y0 = 80.0 + row * 0.5
        for column in range(5):
            x0 = 50.0 + column * 12.0
            page.insert_image(
                pymupdf.Rect(x0, y0, x0 + 2.0, y0 + 0.5),
                stream=strip_bytes,
            )
    if include_oversized_strip:
        oversized_image = PILImage.new("RGB", (33, 1), (0, 0, 0))
        oversized_output = io.BytesIO()
        oversized_image.save(oversized_output, format="PNG")
        page.insert_image(
            pymupdf.Rect(50.0, 106.0, 66.5, 106.5),
            stream=oversized_output.getvalue(),
        )
    document.save(path, deflate=True)
    document.close()


def _write_pdf_with_compound_vector_figure(
    path: Path,
    *,
    include_embedded_fragment: bool,
    include_masked_fragment: bool = False,
    include_table_candidate: bool = False,
    include_following_page: bool = False,
) -> None:
    document = pymupdf.open()
    page = document.new_page(width=320, height=240)
    page.draw_oval(
        pymupdf.Rect(40, 40, 260, 160),
        color=(0.1, 0.2, 0.6),
        fill=(0.7, 0.8, 1.0),
        width=1.5,
    )
    page.draw_rect(
        pymupdf.Rect(82, 76, 218, 126),
        color=(0.1, 0.2, 0.6),
        width=1.5,
    )
    if include_table_candidate:
        for x in (82, 127, 172, 218):
            page.draw_line((x, 76), (x, 126), color=(0.1, 0.2, 0.6), width=1)
        for y in (76, 101, 126):
            page.draw_line((82, y), (218, y), color=(0.1, 0.2, 0.6), width=1)
    page.insert_text((112, 106), "MODULE", fontsize=12)
    if include_embedded_fragment:
        page.insert_image(
            pymupdf.Rect(145, 112, 151, 118),
            stream=_PNG_BYTES,
        )
    if include_masked_fragment:
        page.insert_image(
            pymupdf.Rect(145, 112, 151, 118),
            stream=_PNG_BYTES,
            mask=_png_bytes_from_pixels("L", [0, 85, 170, 255]),
        )
    if include_following_page:
        following_page = document.new_page(width=320, height=240)
        following_page.insert_text((40, 80), "FOLLOWING PAGE", fontsize=12)
    document.save(path)
    document.close()


def _write_pdf_with_rotated_vector_figure(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=595, height=842)
    page.draw_oval(
        pymupdf.Rect(100, 300, 220, 380),
        color=(0.1, 0.2, 0.6),
        fill=(0.7, 0.8, 1.0),
        width=1.5,
    )
    page.set_rotation(90)
    document.save(path)
    document.close()


def _write_pdf_with_bordered_table(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=320, height=220)
    left = 40
    top = 40
    cell_width = 120
    cell_height = 50
    for row in range(3):
        y = top + row * cell_height
        page.draw_line(
            (left, y),
            (left + 2 * cell_width, y),
            color=(0, 0, 0),
            width=1,
        )
    for column in range(3):
        x = left + column * cell_width
        page.draw_line(
            (x, top),
            (x, top + 2 * cell_height),
            color=(0, 0, 0),
            width=1,
        )
    values = (("Material", "Threshold"), ("Steel", "100MPa"))
    for row, values_in_row in enumerate(values):
        for column, value in enumerate(values_in_row):
            page.insert_text(
                (
                    left + column * cell_width + 8,
                    top + row * cell_height + 28,
                ),
                value,
                fontsize=10,
            )
    document.save(path)
    document.close()


def _write_pdf_with_stroked_cell_rectangles_and_ellipse(path: Path) -> None:
    document = pymupdf.open()
    page = document.new_page(width=320, height=240)
    for row in range(2):
        for column in range(2):
            x0 = 40 + column * 120
            y0 = 40 + row * 70
            page.draw_rect(
                pymupdf.Rect(x0, y0, x0 + 120, y0 + 70),
                color=(0, 0, 0),
                width=1,
            )
            page.insert_text(
                (x0 + 50, y0 + 42),
                f"{row}-{column}",
                fontsize=9,
            )
    page.draw_oval(
        pymupdf.Rect(55, 52, 115, 92),
        color=(0.1, 0.2, 0.6),
        fill=(0.7, 0.8, 1.0),
        width=1.5,
    )
    document.save(path)
    document.close()


def _fake_vector_drawing(
    bbox: tuple[float, float, float, float],
    *,
    operation: str,
) -> dict[str, object]:
    return {
        "rect": bbox,
        "type": "s",
        "items": [(operation,)],
    }


def _fake_stroked_quadrilateral_drawing(
    bbox: tuple[float, float, float, float],
) -> dict[str, object]:
    return {
        "rect": bbox,
        "type": "s",
        "color": (0.0, 0.0, 0.0),
        "width": 1.0,
        "stroke_opacity": 1.0,
        "items": [("qu",)],
    }


def _fake_stroked_line_drawing(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    width: float = 1.0,
) -> dict[str, object]:
    return {
        "rect": (
            min(start[0], end[0]),
            min(start[1], end[1]),
            max(start[0], end[0]),
            max(start[1], end[1]),
        ),
        "type": "s",
        "color": (0.0, 0.0, 0.0),
        "width": width,
        "stroke_opacity": 1.0,
        "items": [("l", start, end)],
    }


def _fake_filled_triangle_arrowhead(
    vertices: tuple[
        tuple[float, float],
        tuple[float, float],
        tuple[float, float],
    ],
) -> dict[str, object]:
    x_values = tuple(vertex[0] for vertex in vertices)
    y_values = tuple(vertex[1] for vertex in vertices)
    return {
        "rect": (
            min(x_values),
            min(y_values),
            max(x_values),
            max(y_values),
        ),
        "type": "f",
        "fill": (0.0, 0.0, 0.0),
        "fill_opacity": 1.0,
        "items": [
            ("l", vertices[0], vertices[1]),
            ("l", vertices[1], vertices[2]),
            ("l", vertices[2], vertices[0]),
        ],
    }


def _fake_connector_diagram_drawings(
    *,
    pair_count: int,
    x_offset: float = 0.0,
    y_offset: float = 0.0,
) -> tuple[dict[str, object], ...]:
    left_x = 40.0 + x_offset
    right_x = 260.0 + x_offset
    top_y = 20.0 + y_offset
    bottom_y = 130.0 + y_offset
    drawings: list[dict[str, object]] = [
        _fake_stroked_line_drawing(
            (left_x, top_y),
            (left_x, bottom_y),
        ),
        _fake_stroked_line_drawing(
            (right_x, top_y),
            (right_x, bottom_y),
        ),
        _fake_stroked_quadrilateral_drawing(
            (
                95.0 + x_offset,
                25.0 + y_offset,
                195.0 + x_offset,
                43.0 + y_offset,
            )
        ),
        {
            **_fake_stroked_line_drawing(
                (115.0 + x_offset, 32.0 + y_offset),
                (125.0 + x_offset, 37.0 + y_offset),
            ),
            "items": [
                (
                    "l",
                    (115.0 + x_offset, 32.0 + y_offset),
                    (120.0 + x_offset, 37.0 + y_offset),
                ),
                (
                    "l",
                    (120.0 + x_offset, 37.0 + y_offset),
                    (125.0 + x_offset, 32.0 + y_offset),
                ),
            ],
        },
    ]
    for index in range(pair_count):
        y = 50.0 + y_offset + index * 20.0
        drawings.extend(
            (
                _fake_stroked_line_drawing(
                    (left_x, y),
                    (250.0 + x_offset, y),
                ),
                _fake_filled_triangle_arrowhead(
                    (
                        (250.0 + x_offset, y),
                        (right_x, y - 5.0),
                        (right_x, y + 5.0),
                    )
                ),
            )
        )
    return tuple(drawings)


def _fake_caption_anchored_connector_diagram_inputs(
    *,
    pair_count: int,
    caption_text: str | None = "Figure 7 synthetic connector diagram",
    adjacent_text_blocks: tuple[dict[str, object], ...] = (),
    extra_text_blocks: tuple[dict[str, object], ...] = (),
    table_candidates: tuple[object, ...] = (),
) -> tuple[object, dict[str, object]]:
    blocks: list[dict[str, object]] = [
        *adjacent_text_blocks,
        *extra_text_blocks,
    ]
    if caption_text is not None:
        blocks.append(
            _fake_table_suppression_text_block(
                bbox=(95.0, 140.0, 205.0, 150.0),
                text=caption_text,
            )
        )
    page = SimpleNamespace(
        get_drawings=lambda: _fake_connector_diagram_drawings(
            pair_count=pair_count,
        ),
        find_tables=lambda: SimpleNamespace(tables=table_candidates),
    )
    return page, {"blocks": blocks}


def _fake_connector_bitfield_candidate(
    *,
    bbox: tuple[float, float, float, float],
    marker: str,
    column_widths: tuple[float, float, float, float] | None = None,
) -> object:
    x0, y0, x1, y1 = bbox
    header_y1 = y0 + 10.0
    if column_widths is None:
        cell_width = (x1 - x0) / 4.0
        column_widths = (cell_width,) * 4
    if abs(sum(column_widths) - (x1 - x0)) > 0.001:
        raise ValueError("column widths must span the candidate bbox")
    boundaries = [x0]
    for width in column_widths:
        boundaries.append(boundaries[-1] + width)
    rows = (
        SimpleNamespace(
            cells=((x0, y0, x1, header_y1), None, None, None),
        ),
        SimpleNamespace(
            cells=tuple(
                (
                    boundaries[index],
                    header_y1,
                    boundaries[index + 1],
                    y1,
                )
                for index in range(4)
            ),
        ),
    )
    matrix = (
        ("", None, None, None),
        tuple(f"{marker}-Bit{7 - index}" for index in range(4)),
    )
    return SimpleNamespace(
        bbox=bbox,
        row_count=2,
        col_count=4,
        rows=rows,
        extract=lambda: matrix,
    )


def _fake_quadrilateral_figure_inputs(
    *,
    reference_bbox: tuple[float, float, float, float] = (
        20.0,
        28.0,
        280.0,
        40.0,
    ),
    reference_text: str = "See Figure 7.",
) -> tuple[object, dict[str, object]]:
    quadrilateral_bboxes = (
        (40.0, 50.0, 260.0, 130.0),
        (55.0, 60.0, 105.0, 85.0),
        (105.0, 60.0, 155.0, 85.0),
        (55.0, 85.0, 105.0, 110.0),
        (155.0, 60.0, 205.0, 85.0),
        (205.0, 60.0, 245.0, 85.0),
    )
    page = SimpleNamespace(
        get_drawings=lambda: tuple(
            _fake_stroked_quadrilateral_drawing(bbox) for bbox in quadrilateral_bboxes
        ),
        get_bboxlog=lambda: tuple(
            (
                "stroke-path",
                (
                    bbox[0] - 0.5,
                    bbox[1] - 0.5,
                    bbox[2] + 0.5,
                    bbox[3] + 0.5,
                ),
            )
            for bbox in quadrilateral_bboxes
        ),
        find_tables=lambda: SimpleNamespace(tables=()),
    )
    raw_page: dict[str, object] = {
        "blocks": [
            _fake_table_suppression_text_block(
                bbox=reference_bbox,
                text=reference_text,
            ),
            _fake_table_suppression_text_block(
                bbox=(100.0, 140.0, 200.0, 150.0),
                text="Figure 7 synthetic layout",
            ),
        ]
    }
    return page, raw_page


class _OversizedDrawingItems(list[object]):
    def __len__(self) -> int:
        return pdf_adapter_module._MAX_PDF_DRAWING_ITEMS_PER_PAGE + 1


def _relative_inventory(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(item.relative_to(root)) for item in root.rglob("*") if item.is_file()
        )
    )


if __name__ == "__main__":
    unittest.main()
