from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import Mock, patch

import fitz
from fastapi import Response

from app.application.errors import ApplicationError as HTTPException

from app import db

if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from app.adapters.documents.pdf_visual_text_runtime import (
    PdfVisualTextPageBinding,
    PdfVisualTextRoute,
    PdfVisualTextRuntime,
    visual_text_provider_id,
)
from app.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    SHADOW_PREVIEW_SCHEMA_VERSION,
    ShadowPreviewRejected,
    ShadowPreviewRejectionCode,
    _context_blocks,
    build_document_view,
    build_extraction_shadow_preview,
    build_page_comparison_view,
)
from app.application.extraction.build_source_ir import build_source_ir
from app.application.extraction.classify_content import classify_content
from app.application.extraction.llm_provision_batch import (
    LlmRequirementDocumentProgress,
)
from app.application.extraction.build_llm_shadow_preview import (
    LlmShadowPreviewRejected,
    LlmShadowPreviewRejectionCode,
)
from app.application.extraction.project_requirements import project_requirements
from app.application.extraction.resolve_structure import (
    StructureResolutionRejected,
    build_structure_tree,
)
from app.domain.extraction.semantics import SemanticClass
from app.domain.extraction.processing_profile import (
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceDiagnosticSeverity,
    SourceNodeKind,
)
from app.domain.extraction.visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualLayoutTable,
    VisualLayoutTableCell,
    VisualTextGeometryReceipt,
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.ports.visual_text_provider import VisualTextPageInput
from app.ports.job_queue import JobExecutionContext
from app.services.visual_text_snapshot_cache import VisualTextProviderTelemetryV1


main.app = http_app


class ExtractionShadowPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "native-requirements.pdf"
        _write_native_pdf(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_builds_frontend_dto_without_writing_next_to_source(self) -> None:
        before = sorted(path.name for path in self.root.iterdir())
        source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()

        result = build_extraction_shadow_preview(
            self.source,
            expected_source_sha256=source_sha256,
            offset=0,
            limit=2,
        )

        self.assertEqual(result["schema_version"], SHADOW_PREVIEW_SCHEMA_VERSION)
        self.assertEqual(result["mode"], "READ_ONLY_SHADOW")
        self.assertEqual(result["serving_impact"], "NONE")
        self.assertIsNone(result["requirement_set_id"])
        self.assertFalse(result["requirement_set_created"])
        self.assertFalse(result["requirement_set_activated"])
        self.assertEqual(result["ocr"]["mode"], "OFF")
        self.assertFalse(result["ocr"]["local_fallback"])
        self.assertEqual(
            result["source_identity"]["source_sha256"],
            source_sha256,
        )
        self.assertEqual(result["pipeline"]["source_ir"]["status"], "COMPLETE")
        self.assertGreater(result["pagination"]["total"], 0)
        self.assertLessEqual(result["pagination"]["returned"], 2)
        self.assertEqual(result["document"]["page_count"], 1)
        self.assertEqual(result["document"]["max_page_number"], 1)
        self.assertEqual(result["document"]["locatable_page_start"], 1)
        self.assertEqual(result["document"]["locatable_page_end"], 1)
        comparison = result["page_comparison"]
        self.assertEqual(
            comparison["schema_version"],
            PAGE_COMPARISON_SCHEMA_VERSION,
        )
        self.assertEqual(
            comparison["coordinate_space"],
            "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
        )
        self.assertEqual(comparison["interaction"], "PERSISTENT_LABELED_BOXES")
        self.assertEqual(
            comparison["content_type_contract"],
            ["TEXT", "TABLE", "FIGURE", "HEADER", "FOOTER", "WATERMARK"],
        )
        self.assertEqual(len(comparison["pages"]), 1)
        page = comparison["pages"][0]
        self.assertEqual(page["page_number"], 1)
        self.assertGreater(page["bounds"][2], page["bounds"][0])
        self.assertGreater(page["bounds"][3], page["bounds"][1])
        self.assertTrue(page["blocks"])
        self.assertTrue(all(block["locatable"] for block in page["blocks"]))
        self.assertEqual(
            len({block["label"] for block in page["blocks"]}),
            len(page["blocks"]),
        )
        self.assertTrue(
            all(
                block["source_ref"]["source_id"] == block["id"]
                for block in page["blocks"]
            )
        )
        self.assertEqual(
            result["presentation"]["default_collection"],
            "document_outline",
        )
        self.assertTrue(result["document_outline"])
        self.assertTrue(result["review_groups"])
        self.assertEqual(
            result["review_counts"]["recognized_requirement_groups"],
            len(result["requirement_groups"]),
        )
        first_group = result["review_groups"][0]
        self.assertEqual(first_group["review_status"], "REQUIREMENT")
        self.assertNotIn("role", first_group)
        self.assertNotIn("role", first_group["assessment_points"][0])
        self.assertEqual(
            [block["source_order"] for block in first_group["context_blocks"]],
            sorted(
                block["source_order"]
                for block in first_group["context_blocks"]
                if block["source_order"] is not None
            ),
        )
        first = result["items"][0]
        self.assertTrue(first["title"])
        self.assertTrue(first["text"])
        self.assertEqual(first["page_start"], 1)
        self.assertTrue(first["source_refs"])
        self.assertNotIn("storage_key", first)
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            before,
        )

    def test_document_view_rejects_tampered_structure_catalog(self) -> None:
        source_ir = build_source_ir(self.source)
        structure = build_structure_tree(source_ir)
        tampered = structure.model_copy(update={"source_node_ids": ()})

        with self.assertRaises(StructureResolutionRejected):
            build_document_view(source_ir, tampered)

    def test_page_comparison_exposes_remote_visual_fact_provenance(self) -> None:
        source_ir = build_source_ir(self.source)
        visual_snapshot = _visual_text_snapshot(
            source_sha256=source_ir.source_sha256,
            page_number=1,
            page_image_sha256="a" * 64,
            raw_text="远程视觉识别到的补充文字",
            confidence=0.93,
            with_geometry_receipt=True,
        )
        altered_snapshot = source_ir.model_copy(
            update={"visual_text_snapshots": (visual_snapshot,)}
        )
        structure = build_structure_tree(altered_snapshot)

        comparison = build_page_comparison_view(
            altered_snapshot,
            structure,
            page_bounds=((0.0, 0.0, 595.0, 842.0),),
        )

        page = comparison["pages"][0]
        visual = next(
            block
            for block in page["blocks"]
            if block["id"] == visual_snapshot.observations[0].observation_id
        )
        native = next(
            block
            for block in page["blocks"]
            if block["provenance"]["channel"] == "NATIVE"
        )
        self.assertEqual(comparison["downstream_identity"], "SOURCE_IR_FACT_ID")
        self.assertEqual(visual["source_ref"]["kind"], "visual_text_observation")
        self.assertTrue(visual["locatable"])
        self.assertEqual(visual["bbox"], [80.0, 120.0, 420.0, 140.0])
        self.assertEqual(visual["provenance"]["channel"], "REMOTE_VISUAL")
        self.assertEqual(visual["provenance"]["provider"]["id"], "fixture-visual-text")
        self.assertEqual(visual["provenance"]["provider"]["version"], "p3-test-v1")
        self.assertEqual(visual["provenance"]["confidence"], 0.93)
        self.assertEqual(visual["provenance"]["source_sha256"], source_ir.source_sha256)
        self.assertEqual(visual["provenance"]["page_image_sha256"], "a" * 64)
        self.assertEqual(
            visual["provenance"]["coordinate_mapping"]["status"],
            "GEOMETRY_TRANSFORM_VALIDATED",
        )
        self.assertEqual(native["provenance"]["channel"], "NATIVE")
        self.assertEqual(page["counts"]["by_channel"]["REMOTE_VISUAL"], 1)
        self.assertGreater(page["counts"]["by_channel"]["NATIVE"], 0)
        self.assertEqual(page["coverage"]["remote_text"]["status"], "OBSERVED")
        self.assertTrue(page["coverage"]["remote_text"]["evaluated"])

    def test_page_layout_projects_six_types_table_grid_and_cell_image(self) -> None:
        source_ir = build_source_ir(self.source)
        visual_snapshot = _visual_layout_snapshot(
            source_sha256=source_ir.source_sha256,
            page_number=1,
            page_image_sha256="b" * 64,
        )
        altered_snapshot = source_ir.model_copy(
            update={"visual_text_snapshots": (visual_snapshot,)}
        )
        structure = build_structure_tree(altered_snapshot)

        document_view = build_document_view(altered_snapshot, structure)
        comparison = build_page_comparison_view(
            altered_snapshot,
            structure,
            page_bounds=((0.0, 0.0, 595.0, 842.0),),
        )

        page = comparison["pages"][0]
        remote_blocks = [
            block
            for block in page["blocks"]
            if block["provenance"]["channel"] == "REMOTE_VISUAL"
        ]
        self.assertEqual(
            {block["type"] for block in remote_blocks},
            set(VISUAL_CONTENT_TYPES),
        )
        toc = next(block for block in remote_blocks if block["type"] == "TEXT")
        self.assertEqual(toc["role"], "TOC")
        table_block = next(block for block in remote_blocks if block["type"] == "TABLE")
        self.assertTrue(table_block["table_id"].startswith("table-"))
        table = next(
            item
            for item in document_view["tables"]
            if item["id"] == table_block["table_id"]
        )
        self.assertEqual((table["row_count"], table["column_count"]), (2, 1))
        self.assertEqual(table["reconstruction"]["mode"], "REMOTE_STRUCTURED_GRID")
        self.assertEqual(table["cells"][1]["horizontal_alignment"], "CENTER")
        self.assertEqual(len(table["cells"][1]["images"]), 1)
        self.assertEqual(
            table["cells"][1]["images"][0]["rendering"]["mode"],
            "SOURCE_PDF_CROP",
        )
        coverage = {item["type"]: item for item in page["coverage"]["content_types"]}
        self.assertTrue(
            all(
                coverage[content_type]["status"] == "OBSERVED"
                for content_type in VISUAL_CONTENT_TYPES
            )
        )
        self.assertTrue(page["coverage"]["remote_visual"]["full_layout_evaluated"])
        structure_ref_ids = {
            ref.source_id
            for node in structure.nodes
            for ref in node.source_refs
            if ref.kind.value == "visual_text_observation"
        }
        self.assertEqual(
            structure_ref_ids,
            {visual_snapshot.observations[0].observation_id},
        )

    def test_scan_page_raster_is_not_presented_as_content_figure(self) -> None:
        scanned_source = self.root / "scanned-page-native-only.pdf"
        _write_full_page_image_pdf(scanned_source)

        result = build_extraction_shadow_preview(
            scanned_source,
            collection="document_outline",
            limit=5_000,
        )

        comparison = result["page_comparison"]
        page = comparison["pages"][0]
        raster = next(
            block for block in page["blocks"] if block["type"] == "PAGE_RASTER"
        )
        self.assertEqual(page["counts"]["by_type"], {"PAGE_RASTER": 1})
        self.assertEqual(page["counts"]["content_blocks"], 0)
        self.assertEqual(page["counts"]["support_blocks"], 1)
        self.assertEqual(raster["role"], "SCAN_LAYER")
        self.assertFalse(raster["default_visible"])
        self.assertFalse(raster["overlay_default"])
        self.assertEqual(
            raster["classification_basis"],
            "presenter:full_page_scan_layer",
        )
        coverage_by_type = {
            item["type"]: item for item in page["coverage"]["content_types"]
        }
        self.assertTrue(
            all(
                coverage_by_type[content_type]["status"] == "NOT_EVALUATED"
                for content_type in (
                    "TEXT",
                    "TABLE",
                    "FIGURE",
                    "HEADER",
                    "FOOTER",
                    "WATERMARK",
                )
            )
        )
        self.assertEqual(
            coverage_by_type["TABLE"]["basis"],
            "RASTER_LAYOUT_NOT_EVALUATED",
        )
        self.assertEqual(comparison["coverage"]["page_raster"]["pages"], 1)
        self.assertEqual(comparison["coverage"]["page_raster"]["blocks"], 1)
        self.assertEqual(
            comparison["coverage"]["remote_text"]["pages_evaluated"],
            0,
        )
        self.assertEqual(
            result["pipeline"]["requirement_view"]["status"],
            "REVIEW_REQUIRED",
        )

    def test_authorized_remote_runtime_flows_through_capture_and_presenter(
        self,
    ) -> None:
        scanned_source = self.root / "scanned-page.pdf"
        _write_full_page_image_pdf(scanned_source)
        source_sha256 = hashlib.sha256(scanned_source.read_bytes()).hexdigest()
        page_image_sha256 = _rendered_page_sha256(scanned_source)
        provider = _FixtureRemoteVisualTextProvider()
        binding = PdfVisualTextPageBinding(
            page_number=1,
            input_rotation_cw_degrees=0,
            source_asset_sha256=None,
            page_image_sha256=page_image_sha256,
        )
        route = PdfVisualTextRoute(
            source_sha256=source_sha256,
            page_numbers=frozenset({1}),
            input_rotations_by_page=MappingProxyType({1: 0}),
            page_bindings=MappingProxyType({1: binding}),
            allowed_visual_provider_ids=frozenset({provider.provider_id}),
        )
        runtime = PdfVisualTextRuntime(
            pdf_adapter=PdfSourceIRAdapter(
                visual_text_provider=provider,
                visual_text_page_numbers=frozenset({1}),
            ),
            visual_text_provider=provider,
            visual_text_page_numbers=frozenset({1}),
            visual_table_provider=None,
            visual_table_page_numbers=frozenset(),
            route=route,
        )
        authorization = {
            "authorization_status": "AUTHORIZED",
            "source_sha256": source_sha256,
            "route_id": "pvr-preview-fixture",
            "route_sha256": "a" * 64,
            "authorized_pages": [
                {
                    "page_number": 1,
                    "input_rotation_cw_degrees": 0,
                    "page_binding_kind": "PAGE_IMAGE_SHA256",
                    "page_binding_sha256": page_image_sha256,
                }
            ],
        }

        with self.assertRaises(ShadowPreviewRejected) as missing_authorization:
            build_extraction_shadow_preview(
                scanned_source,
                expected_source_sha256=source_sha256,
                visual_provider="bailian",
                visual_runtime=runtime,
                collection="document_outline",
                limit=5_000,
            )
        self.assertEqual(
            missing_authorization.exception.code,
            ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
        )

        result = build_extraction_shadow_preview(
            scanned_source,
            expected_source_sha256=source_sha256,
            visual_provider="bailian",
            visual_runtime=runtime,
            visual_authorization=authorization,
            collection="document_outline",
            limit=5_000,
        )

        ocr = result["ocr"]
        self.assertEqual(ocr["mode"], "REMOTE_VISUAL")
        self.assertEqual(ocr["status"], "CAPTURED")
        self.assertEqual(ocr["provider"]["id"], provider.provider_id)
        self.assertEqual(ocr["provider"]["version"], provider.provider_version)
        self.assertEqual(ocr["captured_pages"], [1])
        self.assertEqual(
            ocr["routing"],
            {
                "policy_id": "native-first-simple/v1",
                "status": "APPLIED",
                "native_page_count": 0,
                "visual_page_count": 1,
                "visual_pages": [
                    {
                        "page_number": 1,
                        "reasons": ["FULL_PAGE_IMAGE_UNTRUSTED_TEXT"],
                    }
                ],
            },
        )
        self.assertEqual(ocr["route"]["route_id"], "pvr-preview-fixture")
        self.assertEqual(ocr["confidence"]["mean"], 0.94)
        self.assertEqual(
            ocr["coordinate_mapping"]["status"],
            "LEGACY_BBOX_ONLY",
        )
        self.assertFalse(ocr["local_fallback"])
        self.assertFalse(ocr["implicit_fallback"])
        self.assertEqual(ocr["failure_policy"], "BLOCKING")
        self.assertEqual(ocr["visual_table"]["mode"], "OFF")
        remote_blocks = [
            block
            for block in result["page_comparison"]["pages"][0]["blocks"]
            if block["provenance"]["channel"] == "REMOTE_VISUAL"
        ]
        self.assertEqual(len(remote_blocks), 1)
        self.assertEqual(remote_blocks[0]["text"], "Remote captured text")
        self.assertEqual(
            remote_blocks[0]["provenance"]["page_image_sha256"],
            page_image_sha256,
        )
        self.assertEqual(
            remote_blocks[0]["provenance"]["coordinate_mapping"]["status"],
            "LEGACY_BBOX_ONLY",
        )
        page = result["page_comparison"]["pages"][0]
        self.assertEqual(
            result["page_comparison"]["coverage"]["remote_text"]["pages_evaluated"],
            1,
        )
        self.assertEqual(page["coverage"]["remote_text"]["status"], "OBSERVED")
        self.assertEqual(page["coverage"]["remote_text"]["observation_count"], 1)
        self.assertEqual(
            {
                item["type"]: item["status"]
                for item in page["coverage"]["content_types"]
                if item["type"] in {"TABLE", "FIGURE"}
            },
            {"TABLE": "NOT_EVALUATED", "FIGURE": "NOT_EVALUATED"},
        )

    def test_page_comparison_keeps_unlocatable_fact_without_inventing_bbox(
        self,
    ) -> None:
        source_ir = build_source_ir(self.source)
        original = source_ir.nodes[0]
        unlocatable = original.model_copy(
            update={
                "native_locator": NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    page_number=1,
                    block_index=original.native_locator.block_index or 0,
                ),
                "visual_locator": None,
            }
        )
        altered_snapshot = source_ir.model_copy(
            update={
                "nodes": (unlocatable, *source_ir.nodes[1:]),
            }
        )
        structure = build_structure_tree(altered_snapshot)

        comparison = build_page_comparison_view(
            altered_snapshot,
            structure,
            page_bounds=((0.0, 0.0, 595.0, 842.0),),
        )

        block = next(
            item
            for item in comparison["pages"][0]["blocks"]
            if item["id"] == original.source_node_id
        )
        self.assertFalse(block["locatable"])
        self.assertIsNone(block["bbox"])
        self.assertEqual(block["location_status"], "BBOX_MISSING")
        self.assertEqual(block["source_location"]["block_index"], 0)

    def test_page_comparison_only_overlays_actionable_diagnostics(self) -> None:
        source_ir = build_source_ir(self.source)
        locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            bbox=(60.0, 60.0, 220.0, 90.0),
        )
        altered_snapshot = source_ir.model_copy(
            update={
                "diagnostics": (
                    SourceDiagnostic(
                        code="geometry_evidence_recorded",
                        severity=SourceDiagnosticSeverity.INFO,
                        message="Geometry evidence was recorded.",
                        native_locator=locator,
                    ),
                    SourceDiagnostic(
                        code="geometry_needs_review",
                        severity=SourceDiagnosticSeverity.WARNING,
                        message="Geometry needs review.",
                        native_locator=locator,
                    ),
                )
            }
        )
        structure = build_structure_tree(altered_snapshot)

        comparison = build_page_comparison_view(
            altered_snapshot,
            structure,
            page_bounds=((0.0, 0.0, 595.0, 842.0),),
        )

        issues = {issue["code"]: issue for issue in comparison["pages"][0]["issues"]}
        self.assertTrue(issues["geometry_evidence_recorded"]["locatable"])
        self.assertFalse(issues["geometry_evidence_recorded"]["overlay"])
        self.assertTrue(issues["geometry_needs_review"]["overlay"])

    def test_page_comparison_preserves_fact_without_a_page_assignment(self) -> None:
        source_ir = build_source_ir(self.source)
        original = source_ir.nodes[0]
        unassigned = original.model_copy(
            update={
                "native_locator": NativeLocator(
                    format=NativeDocumentFormat.PDF,
                    whole_document=True,
                ),
                "visual_locator": None,
            }
        )
        altered_snapshot = source_ir.model_copy(
            update={"nodes": (unassigned, *source_ir.nodes[1:])}
        )
        structure = build_structure_tree(altered_snapshot)

        comparison = build_page_comparison_view(
            altered_snapshot,
            structure,
            page_bounds=((0.0, 0.0, 595.0, 842.0),),
        )

        block = next(
            item
            for item in comparison["unassigned_blocks"]
            if item["id"] == original.source_node_id
        )
        self.assertIsNone(block["page_number"])
        self.assertFalse(block["locatable"])
        self.assertEqual(block["location_status"], "BBOX_MISSING")
        self.assertFalse(
            any(
                item["id"] == original.source_node_id
                for page in comparison["pages"]
                for item in page["blocks"]
            )
        )

    def test_rejects_visual_provider_and_changed_source_identity(self) -> None:
        with self.assertRaises(ShadowPreviewRejected) as visual_context:
            build_extraction_shadow_preview(
                self.source,
                visual_provider="local-tesseract",
            )
        self.assertEqual(
            visual_context.exception.code,
            ShadowPreviewRejectionCode.UNSUPPORTED_VISUAL_PROVIDER,
        )

        with self.assertRaises(ShadowPreviewRejected) as hash_context:
            build_extraction_shadow_preview(
                self.source,
                expected_source_sha256="0" * 64,
            )
        self.assertEqual(
            hash_context.exception.code,
            ShadowPreviewRejectionCode.SOURCE_HASH_MISMATCH,
        )

    def test_legacy_source_without_stored_hash_is_labeled_not_matched(self) -> None:
        result = build_extraction_shadow_preview(self.source, limit=1)

        self.assertIsNone(result["source_identity_match"])
        self.assertEqual(result["source_identity"]["status"], "COMPUTED_FOR_RUN")
        self.assertFalse(result["source_identity"]["stored_hash_available"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_document_outline_collection_omits_heavy_semantic_payloads(self) -> None:
        with (
            patch(
                "app.application.extraction.build_shadow_preview.classify_content",
                side_effect=AssertionError(
                    "outline view must not run semantic classification"
                ),
            ) as classify,
            patch(
                "app.application.extraction.build_shadow_preview.project_requirements",
                side_effect=AssertionError(
                    "outline view must not run requirement projection"
                ),
            ) as project,
        ):
            result = build_extraction_shadow_preview(
                self.source,
                collection="document_outline",
                limit=5_000,
            )

        self.assertEqual(result["collection"], "document_outline")
        self.assertTrue(result["document_outline"])
        self.assertEqual(result["items"], [])
        self.assertEqual(result["review_groups"], [])
        self.assertEqual(result["requirement_groups"], [])
        self.assertEqual(
            result["presentation"]["omitted_collections"],
            ["items", "review_groups", "requirement_groups"],
        )
        self.assertEqual(
            result["pipeline"]["semantic"]["status"],
            "SKIPPED_NOT_REQUESTED",
        )
        classify.assert_not_called()
        project.assert_not_called()

    def test_document_outline_exposes_server_validated_llm_scope_paths(self) -> None:
        result = build_extraction_shadow_preview(
            self.source,
            collection="document_outline",
            limit=5_000,
        )

        selectors = {
            item["outline_label"]: item["llm_scope_selector"]
            for item in result["document_outline"]
            if item["document_region"] == "BODY"
        }
        self.assertEqual(
            selectors["1"],
            {"normalized_section_path": ["1"]},
        )
        self.assertEqual(
            selectors["1.1"],
            {"normalized_section_path": ["1", "1.1"]},
        )
        self.assertEqual(
            selectors["1.2"],
            {"normalized_section_path": ["1", "1.2"]},
        )

    def test_document_outline_includes_preface_before_body(self) -> None:
        source = self.root / "preface.pdf"
        _write_pdf_with_preface(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(
            [item["document_region"] for item in result["document_outline"]],
            ["PREFACE", "BODY", "BODY"],
        )
        preface = result["document_outline"][0]
        self.assertEqual(preface["outline_label"], "封面")
        self.assertEqual((preface["page_start"], preface["page_end"]), (1, 1))
        self.assertIn("Product development specification", preface["context_text"])

    def test_serializes_native_table_once_and_links_context_by_table_id(self) -> None:
        source = self.root / "table-requirements.pdf"
        _write_native_pdf_with_table(source)

        result = build_extraction_shadow_preview(source)

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        self.assertEqual((table["row_count"], table["column_count"]), (2, 2))
        self.assertEqual(table["header_regions"], [])
        self.assertEqual(table["merged_regions"], [])
        self.assertEqual(
            table["geometry"],
            {
                "status": "PRESERVED",
                "width_points": 260.0,
                "height_points": 100.0,
                "column_widths_points": [130.0, 130.0],
                "row_heights_points": [50.0, 50.0],
            },
        )
        self.assertEqual(
            table["reconstruction"],
            {
                "mode": "STRUCTURED_TEXT_GRID",
                "exact_visual_layout": False,
                "merge_semantics": "NO_MERGES_DETECTED",
                "cell_geometry": "PRESERVED",
                "visual_asset_available": False,
                "text_authority": "NATIVE",
            },
        )
        self.assertEqual(
            [
                (
                    cell["row"],
                    cell["column"],
                    cell["row_span"],
                    cell["column_span"],
                    cell["text"],
                )
                for cell in table["cells"]
            ],
            [
                (0, 0, 1, 1, "Material"),
                (0, 1, 1, 1, "Threshold"),
                (1, 0, 1, 1, "Steel"),
                (1, 1, 1, 1, "100MPa"),
            ],
        )
        linked_groups = [
            group
            for group in result["review_groups"]
            if table["id"] in group["table_ids"]
        ]
        self.assertTrue(linked_groups)
        self.assertEqual(
            sum(
                block["kind"] == "table"
                for group in linked_groups
                for block in group["context_blocks"]
                if block.get("table_id") == table["id"]
            ),
            len(linked_groups),
        )
        self.assertTrue(
            all(group["context_text"].count("Material") == 1 for group in linked_groups)
        )
        self.assertTrue(
            any(table["id"] in item["table_ids"] for item in result["items"])
        )
        page_table = next(
            block
            for block in result["page_comparison"]["pages"][0]["blocks"]
            if block["type"] == "TABLE"
        )
        self.assertEqual(page_table["table_id"], table["id"])
        self.assertTrue(page_table["locatable"])
        self.assertTrue(page_table["label"].startswith("TBL-"))

    def test_serializes_grounded_cell_image_for_inline_source_crop(self) -> None:
        source = self.root / "table-cell-image.pdf"
        _write_native_pdf_with_table_cell_image(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
            limit=5_000,
        )

        self.assertEqual(len(result["tables"]), 1)
        table = result["tables"][0]
        image_cells = [cell for cell in table["cells"] if cell["images"]]
        self.assertEqual(len(image_cells), 1)
        self.assertEqual((image_cells[0]["row"], image_cells[0]["column"]), (1, 1))
        image = image_cells[0]["images"][0]
        self.assertEqual(
            image["rendering"], {"mode": "SOURCE_PDF_CROP", "available": True}
        )
        self.assertEqual(image["location"]["page_number"], 1)
        self.assertEqual(len(image["location"]["bbox"]), 4)
        self.assertNotIn("storage_key", image)
        self.assertTrue(table["reconstruction"]["visual_asset_available"])
        page_image = next(
            block
            for block in result["page_comparison"]["pages"][0]["blocks"]
            if block["type"] == "FIGURE"
        )
        self.assertTrue(page_image["locatable"])
        self.assertTrue(page_image["label"].startswith("FIG-"))
        self.assertIsNone(page_image["role"])
        self.assertTrue(page_image["default_visible"])
        self.assertIsNotNone(page_image["asset"])

    def test_serializes_cross_page_tables_as_one_continuation_chain(self) -> None:
        source = self.root / "continued-table.pdf"
        _write_pdf_with_cross_page_table(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
            limit=5_000,
        )

        self.assertEqual(len(result["tables"]), 2)
        tables = sorted(result["tables"], key=lambda table: table["page_start"])
        first, second = tables
        self.assertEqual(
            [table["continuation"]["status"] for table in tables],
            ["CONTINUED", "CONTINUED"],
        )
        self.assertEqual(
            [table["continuation"]["segment_index"] for table in tables],
            [0, 1],
        )
        self.assertTrue(
            all(table["continuation"]["segment_count"] == 2 for table in tables)
        )
        self.assertEqual(
            first["continuation"]["logical_table_id"],
            second["continuation"]["logical_table_id"],
        )
        self.assertEqual(first["continuation"]["next_table_id"], second["id"])
        self.assertEqual(second["continuation"]["previous_table_id"], first["id"])

    def test_table_overlap_does_not_hide_text_missing_from_reconstructed_cells(
        self,
    ) -> None:
        location = {
            "format": "pdf",
            "page_number": 1,
            "bbox": [40.0, 70.0, 300.0, 170.0],
        }
        blocks = _context_blocks(
            [
                {
                    "kind": "source_node",
                    "source_id": "table-source",
                    "source_order": 1,
                    "table_id": "table-1",
                    "text": "Material | Threshold",
                    "location": location,
                },
                {
                    "kind": "source_node",
                    "source_kind": "paragraph",
                    "source_id": "overlay-note",
                    "source_order": 2,
                    "text": "This note is not represented by any cell",
                    "location": {
                        **location,
                        "bbox": [60.0, 90.0, 260.0, 110.0],
                    },
                },
            ],
            tables_by_id={
                "table-1": {
                    "source_node_id": "table-source",
                    "source_order": 1,
                    "text": "Material | Threshold",
                    "location": location,
                    "page_start": 1,
                    "page_end": 1,
                }
            },
        )

        self.assertEqual([block["kind"] for block in blocks], ["table", "text"])
        self.assertIn("not represented", blocks[1]["text"])

    def test_document_outline_folds_toc_and_excludes_changing_page_numbers(
        self,
    ) -> None:
        source = self.root / "toc-and-page-numbers.pdf"
        _write_pdf_with_toc_and_changing_page_numbers(source)

        source_ir = build_source_ir(source)
        result = build_extraction_shadow_preview(source)

        self.assertEqual(
            [
                node.raw_text.strip()
                for node in source_ir.nodes
                if node.raw_text.strip() in {"101", "102", "103", "104"}
            ],
            ["101", "102", "103", "104"],
        )
        self.assertTrue(
            all(
                node.kind is SourceNodeKind.FOOTER
                for node in source_ir.nodes
                if node.raw_text.strip() in {"101", "102", "103", "104"}
            )
        )
        self.assertEqual(len(result["document_regions"]["toc"]), 1)
        toc = result["document_regions"]["toc"][0]
        self.assertEqual(toc["page_start"], 1)
        self.assertEqual(toc["page_end"], 2)
        self.assertEqual(
            toc["entries"],
            [
                "1 Overview ........ 3",
                "1.1 Schedule ...... 3",
                "2 Requirements .... 4",
                "2.1 Performance ... 4",
            ],
        )
        self.assertEqual(
            [item["outline_label"] for item in result["document_outline"]],
            ["目录", "1", "1.1", "2", "2.1"],
        )
        self.assertTrue(
            all(
                item["page_start"] >= 3
                for item in result["document_outline"]
                if item["document_region"] == "BODY"
            )
        )
        self.assertFalse(
            any(
                block["text"].strip() in {"101", "102", "103", "104"}
                for item in result["document_outline"]
                for block in item["context_blocks"]
            )
        )
        self.assertFalse(
            any(group.get("page_start") in {1, 2} for group in result["review_groups"])
        )
        self.assertFalse(
            any(item.get("page_start") in {1, 2} for item in result["items"])
        )
        page_blocks = [
            block
            for page in result["page_comparison"]["pages"]
            for block in page["blocks"]
        ]
        self.assertTrue(
            all(
                block["type"] == "TEXT" and block["role"] == "TOC"
                for block in page_blocks
                if block["page_number"] in {1, 2}
                and block["source_kind"] not in {"footer", "header"}
            )
        )
        self.assertEqual(
            [
                block["type"]
                for block in page_blocks
                if block["text"].strip() in {"101", "102", "103", "104"}
            ],
            ["FOOTER", "FOOTER", "FOOTER", "FOOTER"],
        )

    def test_midpage_number_sequence_is_not_classified_as_page_furniture(self) -> None:
        source = self.root / "midpage-numbers.pdf"
        _write_pdf_with_midpage_numbers(source)

        source_ir = build_source_ir(source)
        numbers = [
            node
            for node in source_ir.nodes
            if node.raw_text.strip() in {"11", "12", "13"}
        ]

        self.assertEqual(len(numbers), 3)
        self.assertTrue(all(node.kind is SourceNodeKind.PARAGRAPH for node in numbers))

    def test_mirrored_arabic_and_roman_page_markers_are_furniture(self) -> None:
        source = self.root / "mirrored-and-roman-page-numbers.pdf"
        _write_pdf_with_mirrored_and_roman_page_numbers(source)

        source_ir = build_source_ir(source)
        markers = [
            node
            for node in source_ir.nodes
            if node.raw_text.strip() in {"I", "II", "III", "IV", "1", "2", "3", "4"}
        ]

        self.assertEqual(len(markers), 8)
        self.assertTrue(all(node.kind is SourceNodeKind.FOOTER for node in markers))

    def test_combined_page_number_and_repeated_footer_stays_out_of_body(self) -> None:
        source = self.root / "combined-pagination-footer.pdf"
        _write_pdf_with_combined_pagination_footer(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        body = [
            item
            for item in result["document_outline"]
            if item["document_region"] == "BODY"
        ]
        self.assertEqual([item["outline_label"] for item in body], ["1", "2", "3", "4"])
        self.assertFalse(
            any(
                "DOC-CODE-2026" in block["text"]
                for item in body
                for block in item["context_blocks"]
            )
        )
        self.assertFalse(
            any(
                block["text"].strip() in {"1", "2", "3", "4"}
                and block.get("page_start") == item.get("page_start")
                for item in body
                for block in item["context_blocks"]
                if block["kind"] == "text"
            )
        )

        source_ir = build_source_ir(source)
        structure = build_structure_tree(source_ir)
        semantics = classify_content(source_ir, structure)
        requirement_view = project_requirements(source_ir, structure, semantics)
        footer_source_ids = {
            node.source_node_id
            for node in source_ir.nodes
            if "DOC-CODE-2026" in node.raw_text
        }
        footer_structure_ids = {
            node.structure_node_id
            for node in structure.nodes
            if footer_source_ids
            & {source_ref.source_id for source_ref in node.source_refs}
        }
        noise_structure_ids = {
            annotation.target_structure_node_id
            for annotation in semantics.annotations
            if annotation.semantic_class is SemanticClass.NOISE
        }

        self.assertEqual(len(footer_source_ids), 4)
        self.assertTrue(footer_structure_ids)
        self.assertTrue(footer_structure_ids.issubset(noise_structure_ids))
        self.assertFalse(
            any(
                footer_source_ids
                & {
                    source_ref.source_id
                    for source_ref in (
                        *node.source_refs,
                        *node.full_context_source_refs,
                    )
                }
                for node in requirement_view.nodes
            )
        )

    def test_multiline_native_blocks_extend_a_cross_page_toc(self) -> None:
        source = self.root / "multiline-toc-blocks.pdf"
        _write_pdf_with_multiline_toc_blocks(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )
        toc = result["document_regions"]["toc"][0]

        self.assertEqual((toc["page_start"], toc["page_end"]), (1, 2))
        self.assertEqual(
            toc["entries"],
            [
                "Preface ............ III",
                "1 Scope ............ 1",
                "2 References ....... 2",
                "3 Terms ............ 3",
                "4 Requirements ..... 4",
                "Appendix A ......... 8",
            ],
        )
        self.assertEqual(
            [
                item["outline_label"]
                for item in result["document_outline"]
                if item["document_region"] == "BODY"
            ],
            ["1", "1.1"],
        )

    def test_strong_target_page_lines_seed_toc_when_title_is_not_extractable(
        self,
    ) -> None:
        source = self.root / "headingless-multiline-toc.pdf"
        _write_pdf_with_headingless_multiline_toc(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(len(result["document_regions"]["toc"]), 1)
        toc = result["document_regions"]["toc"][0]
        self.assertEqual((toc["page_start"], toc["page_end"]), (1, 2))
        self.assertEqual(
            toc["entries"],
            [
                "1 Scope ............ 3",
                "2 References ....... 4",
                "3 Terms ............ 5",
                "4 Requirements ..... 6",
                "Appendix A ......... 8",
            ],
        )
        self.assertEqual(
            [
                item["outline_label"]
                for item in result["document_outline"]
                if item["document_region"] == "BODY"
            ],
            ["1", "1.1"],
        )

    def test_two_target_page_lines_do_not_seed_headingless_toc(self) -> None:
        source = self.root / "short-cross-reference-list.pdf"
        _write_pdf_with_short_headingless_cross_reference_list(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(result["document_regions"]["toc"], [])
        self.assertEqual(
            [item["outline_label"] for item in result["document_outline"]],
            ["1", "3"],
        )
        self.assertIn(
            "2 Supporting topic",
            result["document_outline"][0]["context_text"],
        )

    def test_body_before_three_target_lines_does_not_seed_headingless_toc(self) -> None:
        source = self.root / "body-before-target-list.pdf"
        _write_pdf_with_body_before_three_target_lines(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(result["document_regions"]["toc"], [])
        self.assertTrue(
            any(
                "2 Related topic" in item["context_text"]
                for item in result["document_outline"]
            )
        )

    def test_page_top_standard_year_references_do_not_seed_headingless_toc(
        self,
    ) -> None:
        source = self.root / "standard-year-references.pdf"
        _write_pdf_with_page_top_standard_year_references(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(result["document_regions"]["toc"], [])
        self.assertTrue(
            any(
                "ISO 9001" in item["context_text"]
                for item in result["document_outline"]
            )
        )

    def test_headingless_target_prefix_before_same_page_body_is_not_toc(self) -> None:
        source = self.root / "headingless-prefix-before-body.pdf"
        _write_pdf_with_headingless_target_prefix_before_body(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(result["document_regions"]["toc"], [])
        self.assertTrue(
            any(item["outline_label"] == "4" for item in result["document_outline"])
        )

    def test_dotted_outline_labels_seed_headingless_toc_with_fixed_page_offset(
        self,
    ) -> None:
        source = self.root / "headingless-label-offset-toc.pdf"
        _write_pdf_with_headingless_label_offset_toc(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(len(result["document_regions"]["toc"]), 1)
        self.assertEqual(
            (
                result["document_regions"]["toc"][0]["page_start"],
                result["document_regions"]["toc"][0]["page_end"],
            ),
            (1, 1),
        )

    def test_full_page_calibration_cross_reference_is_not_headingless_toc(
        self,
    ) -> None:
        source = self.root / "full-page-calibration-cross-reference.pdf"
        _write_pdf_with_full_page_calibration_cross_reference(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(result["document_regions"]["toc"], [])
        self.assertTrue(
            any(
                "1 Calibration point A" in item["context_text"]
                for item in result["document_outline"]
            )
        )

    def test_explicit_toc_keeps_contiguous_entries_without_page_targets(self) -> None:
        source = self.root / "mixed-target-explicit-toc.pdf"
        _write_pdf_with_mixed_target_explicit_toc(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )

        self.assertEqual(len(result["document_regions"]["toc"]), 1)
        self.assertEqual(
            result["document_regions"]["toc"][0]["entries"],
            ["1 Scope ........ 2", "2 References", "3 Terms"],
        )
        self.assertEqual(
            [
                item["outline_label"]
                for item in result["document_outline"]
                if item["document_region"] == "BODY"
            ],
            ["1"],
        )

    def test_same_page_body_after_toc_remains_authoritative(self) -> None:
        source = self.root / "same-page-toc-body.pdf"
        _write_pdf_with_same_page_toc_and_body(source)

        result = build_extraction_shadow_preview(source)
        toc = result["document_regions"]["toc"][0]

        self.assertEqual(toc["entries"], ["1 Overview", "1.1 Details"])
        self.assertEqual(
            [item["outline_label"] for item in result["document_outline"]],
            ["目录", "2", "2.1"],
        )
        body = next(
            item for item in result["document_outline"] if item["outline_label"] == "2"
        )
        self.assertIn("2.1 Supplier shall comply.", body["context_text"])

    def test_toc_continuation_splits_only_at_large_layout_gap(self) -> None:
        source = self.root / "toc-continuation-with-body.pdf"
        _write_pdf_with_toc_continuation_and_body(source)

        result = build_extraction_shadow_preview(source)
        toc = result["document_regions"]["toc"][0]

        self.assertEqual(
            toc["entries"],
            [
                "1 Overview",
                "1.1 Details",
                "2 Requirements .... 3",
                "2.1 Performance ... 3",
            ],
        )
        self.assertEqual(toc["page_end"], 2)
        self.assertEqual(
            [item["outline_label"] for item in result["document_outline"]],
            ["目录", "3", "3.1"],
        )
        section = next(
            item for item in result["document_outline"] if item["outline_label"] == "3"
        )
        self.assertIn("3.1 Supplier shall comply.", section["context_text"])

    def test_two_numbered_body_headings_do_not_extend_a_weak_toc_seed(self) -> None:
        source = self.root / "weak-toc-then-body.pdf"
        _write_pdf_with_weak_toc_then_body(source)

        result = build_extraction_shadow_preview(
            source,
            collection="document_outline",
        )
        toc = result["document_regions"]["toc"][0]

        self.assertEqual(toc["entries"], ["1 Overview", "1.1 Details"])
        self.assertEqual(toc["page_end"], 1)
        self.assertEqual(
            [
                item["outline_label"]
                for item in result["document_outline"]
                if item["document_region"] == "BODY"
            ],
            ["2", "2.1"],
        )

    def test_embedded_numbered_sections_keep_exact_independent_contexts(
        self,
    ) -> None:
        source = self.root / "embedded-numbered-sections.pdf"
        _write_pdf_with_embedded_numbered_sections(source)

        result = build_extraction_shadow_preview(source)
        groups = {
            str(group.get("outline_label") or ""): group
            for group in result["review_groups"]
        }

        for label in ("4.1.1", "4.1.2", "4.1.3"):
            self.assertIn(label, groups)
            context = groups[label]["context_text"]
            self.assertTrue(context.startswith(label), context)
            for sibling in {"4.1.1", "4.1.2", "4.1.3"} - {label}:
                self.assertNotIn(sibling, context)
            self.assertTrue(
                all(
                    block.get("start_offset") is not None
                    and block.get("end_offset") is not None
                    for block in groups[label]["context_blocks"]
                )
            )

    def test_groups_native_blocks_under_unprojected_numbered_section(self) -> None:
        source = self.root / "numbered-boundary.pdf"
        _write_numbered_section_with_native_blocks(source)
        source_ir = build_source_ir(source)
        structure = build_structure_tree(source_ir)
        semantics = classify_content(source_ir, structure)
        boundary = next(
            node for node in structure.nodes if node.explicit_label == "2.4"
        )
        modified_semantics = semantics.model_copy(
            update={
                "annotations": tuple(
                    annotation.model_copy(
                        update={
                            "semantic_class": SemanticClass.HEADING,
                            "requirement_subtype": None,
                        }
                    )
                    if annotation.target_structure_node_id == boundary.structure_node_id
                    else annotation
                    for annotation in semantics.annotations
                )
            }
        )

        with patch(
            "app.application.extraction.build_shadow_preview.classify_content",
            return_value=modified_semantics,
        ):
            result = build_extraction_shadow_preview(source)

        groups = [
            group
            for group in result["review_groups"]
            if group["outline_label"] == "2.4"
        ]
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["review_status"], "NEEDS_CLASSIFICATION")
        self.assertEqual(group["semantic_class"], "MIXED")
        self.assertEqual(len(group["assessment_points"]), 2)
        self.assertTrue(group["contains_recognized_requirement"])
        self.assertNotIn("role", group)
        assigned_item_ids = [
            item_id
            for requirement_group in result["requirement_groups"]
            for item_id in requirement_group["item_ids"]
        ]
        self.assertEqual(len(assigned_item_ids), len(set(assigned_item_ids)))
        self.assertEqual(
            set(assigned_item_ids),
            {item["id"] for item in result["items"]},
        )
        self.assertEqual(result["review_counts"]["recognized_requirement_groups"], 1)
        self.assertEqual(result["review_counts"]["needs_classification"], 1)


class ExtractionShadowPreviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        self.exit_stack.enter_context(patch.object(main, "STORAGE_DIR", self.root))
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        db.init_db()

        self.source = self.root / "api-native.pdf"
        _write_native_pdf(self.source)
        source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.project = db.create_project(
            name="Shadow Preview Test",
            customer_name="Customer",
        )
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=self.source.name,
            file_type="pdf",
            file_path=str(self.source),
            extracted_text="native fixture",
            source_sha256=source_sha256,
            source_size=self.source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )

    def _remote_processing_plan(self):
        profile = build_local_rules_profile(project_id=self.project["id"])
        return build_resolved_processing_plan(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            profile=profile,
            document=profile.document,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=0,
        )

    def tearDown(self) -> None:
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_endpoint_is_no_store_and_cannot_mutate_requirement_sets(self) -> None:
        response = Response()
        request = main.ExtractionShadowPreviewRequest(limit=20)
        with (
            patch.object(
                db,
                "create_requirement_set",
                side_effect=AssertionError("must remain read-only"),
            ) as create_set,
            patch.object(
                db,
                "insert_requirement",
                side_effect=AssertionError("must remain read-only"),
            ) as insert_requirement,
            patch.object(
                db,
                "activate_requirement_set",
                side_effect=AssertionError("must remain read-only"),
            ) as activate_set,
        ):
            payload = main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                request,
                response,
            )

        self.assertEqual(payload["project_id"], self.project["id"])
        self.assertEqual(payload["document_id"], self.document["id"])
        self.assertEqual(payload["serving_impact"], "NONE")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(
            db.list_requirement_sets(self.project["id"], self.document["id"]), []
        )
        self.assertEqual(
            db.list_requirements(self.project["id"], self.document["id"]), []
        )
        create_set.assert_not_called()
        insert_requirement.assert_not_called()
        activate_set.assert_not_called()

    def test_endpoint_rejects_cross_project_document(self) -> None:
        other_project = db.create_project(
            name="Other Project",
            customer_name="Other Customer",
        )
        with self.assertRaises(HTTPException) as context:
            main.build_sor_extraction_shadow_preview(
                other_project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(),
                Response(),
            )
        self.assertEqual(context.exception.status_code, 404)

    def test_endpoint_supports_outline_only_response(self) -> None:
        payload = main.build_sor_extraction_shadow_preview(
            self.project["id"],
            self.document["id"],
            main.ExtractionShadowPreviewRequest(
                collection="document_outline",
                limit=5_000,
            ),
            Response(),
        )

        self.assertEqual(payload["collection"], "document_outline")
        self.assertTrue(payload["document_outline"])
        self.assertEqual(payload["items"], [])
        self.assertEqual(payload["review_groups"], [])

    def test_endpoint_dispatches_selected_sections_to_editable_llm_review(
        self,
    ) -> None:
        response = Response()
        provider = object()
        shadow = {
            "mode": "EDITABLE_LLM_REVIEW",
            "collection": "llm_requirement_batch",
            "serving_impact": "NONE",
            "requirement_set_id": None,
            "requirement_set_created": False,
            "requirement_set_activated": False,
        }
        request = main.ExtractionShadowPreviewRequest(
            collection="llm_requirement_batch",
            section_paths=(("1", "1.1"), ("1", "1.2")),
        )
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ) as provider_factory,
            patch.object(
                main,
                "build_llm_requirement_batch_preview",
                return_value=shadow,
            ) as build_preview,
            patch.object(
                db,
                "create_requirement_set",
                side_effect=AssertionError("must remain read-only"),
            ) as create_set,
            patch.object(
                db,
                "insert_requirement",
                side_effect=AssertionError("must remain read-only"),
            ) as insert_requirement,
            patch.object(
                db,
                "activate_requirement_set",
                side_effect=AssertionError("must remain read-only"),
            ) as activate_set,
        ):
            payload = main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                request,
                response,
            )

        provider_factory.assert_called_once_with()
        build_preview.assert_called_once_with(
            self.source,
            expected_source_sha256=self.document["source_sha256"],
            section_paths=(("1", "1.1"), ("1", "1.2")),
            provider=provider,
        )
        self.assertEqual(payload["mode"], "EDITABLE_LLM_REVIEW")
        self.assertEqual(payload["serving_impact"], "NONE")
        self.assertIsNone(payload["requirement_set_id"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        create_set.assert_not_called()
        insert_requirement.assert_not_called()
        activate_set.assert_not_called()

    def test_endpoint_dispatches_automatic_full_document_llm_review(self) -> None:
        response = Response()
        provider = object()
        shadow = {
            "mode": "EDITABLE_LLM_REVIEW",
            "collection": "llm_requirement_document",
            "serving_impact": "NONE",
            "requirement_set_id": None,
            "requirement_set_created": False,
            "requirement_set_activated": False,
        }
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ) as provider_factory,
            patch.object(
                main,
                "build_llm_requirement_document_preview",
                return_value=shadow,
            ) as build_preview,
        ):
            payload = main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(
                    collection="llm_requirement_document",
                ),
                response,
            )

        provider_factory.assert_called_once_with()
        build_preview.assert_called_once_with(
            self.source,
            expected_source_sha256=self.document["source_sha256"],
            provider=provider,
        )
        self.assertEqual(payload["collection"], "llm_requirement_document")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_background_document_start_is_idempotent_while_job_is_active(
        self,
    ) -> None:
        response = Response()
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread) as thread_type,
        ):
            first = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                response,
            )
            second = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        self.assertEqual(first["job"]["status"], "PENDING")
        self.assertEqual(second["job"]["job_id"], first["job"]["job_id"])
        self.assertIsNone(first["result"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        thread_type.assert_called_once()
        thread.start.assert_called_once_with()

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["job_id"], first["job"]["job_id"])

    def test_exact_cancel_is_idempotent_and_retry_creates_one_new_attempt(self) -> None:
        provider = object()
        first_thread = Mock()
        second_thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", side_effect=[first_thread, second_thread]),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
            job_id = str(started["job"]["job_id"])
            cancelled = main.cancel_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.DocumentExtractionCancelRequest(expected_job_id=job_id),
            )
            replayed = main.cancel_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.DocumentExtractionCancelRequest(expected_job_id=job_id),
            )
            retried = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        self.assertEqual(cancelled["job"]["status"], "CANCELLED")
        self.assertEqual(replayed["job"]["status"], "CANCELLED")
        self.assertNotEqual(retried["job"]["job_id"], job_id)
        first_thread.start.assert_called_once_with()
        second_thread.start.assert_called_once_with()

    def test_legacy_visual_selector_maps_to_frozen_composable_plan(
        self,
    ) -> None:
        response = Response()
        thread = Mock()
        with (
            patch.object(
                main,
                "build_authorized_pdf_visual_runtime",
                side_effect=AssertionError(
                    "the composable entrypoint must not consult the legacy registry"
                ),
            ) as runtime_factory,
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
            ) as provider_factory,
            patch.object(main, "Thread", return_value=thread) as thread_type,
            patch(
                "app.application.processing_profiles.require_capabilities",
            ),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                response,
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )

        runtime_factory.assert_not_called()
        provider_factory.assert_not_called()
        plan = started["job"]["processing_plan"]
        self.assertEqual(
            plan["document"]["enhancement_chain"],
            ["bailian_layout"],
        )
        self.assertEqual(
            plan["document"]["complex_vision"],
            "bailian_vision",
        )
        self.assertEqual(plan["llm_provider"], "none")
        self.assertEqual(plan["source_sha256"], self.document["source_sha256"])
        self.assertRegex(plan["canonical_sha256"], r"^[0-9a-f]{64}$")
        strategy = started["job"]["visual_strategy"]
        self.assertEqual(strategy["mode"], "OFF")
        worker_kwargs = thread_type.call_args.kwargs["kwargs"]
        self.assertEqual(
            worker_kwargs["processing_plan"].canonical_sha256,
            plan["canonical_sha256"],
        )
        self.assertNotIn("authorized_visual_runtime", worker_kwargs)
        thread.start.assert_called_once_with()
        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["visual_strategy"], strategy)

    def test_legacy_visual_registry_rejection_cannot_override_composable_plan(
        self,
    ) -> None:
        thread = Mock()
        with (
            patch.object(
                main,
                "build_authorized_pdf_visual_runtime",
                side_effect=main.ExtractionVisualRouteRejected(
                    "visual_route_not_authorized",
                    "No exact route.",
                ),
            ) as runtime_factory,
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
            ) as provider_factory,
            patch.object(main, "Thread", return_value=thread) as thread_type,
            patch(
                "app.application.processing_profiles.require_capabilities",
            ),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )

        runtime_factory.assert_not_called()
        provider_factory.assert_not_called()
        thread_type.assert_called_once()
        thread.start.assert_called_once_with()
        self.assertEqual(
            started["job"]["processing_plan"]["document"]["complex_vision"],
            "bailian_vision",
        )

    def test_background_document_unconfigured_visual_provider_fails_closed(
        self,
    ) -> None:
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
            ) as provider_factory,
            patch.object(main, "Thread") as thread_type,
            self.assertRaises(HTTPException) as caught,
        ):
            main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "runtime_not_configured",
        )
        self.assertEqual(
            [item["capability_id"] for item in caught.exception.detail["blocked_by"]],
            ["document.bailian_layout", "aliyun.vision"],
        )
        provider_factory.assert_not_called()
        thread_type.assert_not_called()

    def test_active_document_job_rejects_a_different_composable_plan(
        self,
    ) -> None:
        provider = object()
        first_thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=first_thread),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        self.assertEqual(started["job"]["visual_strategy"]["mode"], "OFF")

        with (
            patch(
                "app.application.processing_profiles.require_capabilities",
            ),
            self.assertRaises(HTTPException) as caught,
        ):
            main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["reason_code"],
            "document_extraction_plan_conflict",
        )

    def test_background_document_start_fails_closed_before_worker_when_checkpoint_save_fails(
        self,
    ) -> None:
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread) as thread_type,
            patch.object(
                main.LlmExtractionJobCheckpointCache,
                "save",
                side_effect=OSError("synthetic checkpoint failure"),
            ),
        ):
            with self.assertRaisesRegex(
                OSError,
                "synthetic checkpoint failure",
            ):
                main.start_llm_document_extraction(
                    self.project["id"],
                    self.document["id"],
                    Response(),
                )

        thread_type.assert_not_called()
        thread.start.assert_not_called()
        with main._LLM_EXTRACTION_JOB_LOCK:
            self.assertNotIn(self.document["id"], main._LLM_EXTRACTION_JOBS)

    def test_background_document_start_rejects_document_without_upload_identity(
        self,
    ) -> None:
        untrusted = db.add_sor_document(
            project_id=self.project["id"],
            file_name=self.source.name,
            file_type="pdf",
            file_path=str(self.source),
            extracted_text="pre-identity fixture",
        )
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                side_effect=AssertionError("provider must not be constructed"),
            ) as provider_factory,
            patch.object(main, "Thread", return_value=thread) as thread_type,
            self.assertRaises(HTTPException) as caught,
        ):
            main.start_llm_document_extraction(
                self.project["id"],
                untrusted["id"],
                Response(),
            )

        refreshed = db.get_sor_document(untrusted["id"])
        assert refreshed is not None
        self.assertEqual(refreshed["source_sha256"], "")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_document_source_identity_unavailable",
        )
        provider_factory.assert_not_called()
        thread_type.assert_not_called()
        thread.start.assert_not_called()

    def test_preparation_heartbeat_cannot_revive_terminal_job(self) -> None:
        job_id = "llm-extraction-" + "a" * 32
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": job_id,
                "status": "FAILED",
                "stage": "failed",
                "error_code": "synthetic_failure",
            }
        heartbeat = main._LlmExtractionPreparationHeartbeat(
            self.document["id"],
            job_id,
            interval_seconds=0.01,
        )

        with self.assertRaises(main._LlmExtractionJobCancelled):
            heartbeat.publish("source_ir")

        with main._LLM_EXTRACTION_JOB_LOCK:
            terminal = dict(main._LLM_EXTRACTION_JOBS[self.document["id"]])
        self.assertEqual(terminal["status"], "FAILED")
        self.assertEqual(terminal["stage"], "failed")
        self.assertEqual(terminal["error_code"], "synthetic_failure")

    def test_preparation_heartbeat_repeats_visual_capture_progress(self) -> None:
        job_id = "llm-extraction-" + "b" * 32
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": job_id,
                "project_id": self.project["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "RUNNING",
                "stage": "source_ir",
                "partial_revision": 1,
            }
        heartbeat = main._LlmExtractionPreparationHeartbeat(
            self.document["id"],
            job_id,
            interval_seconds=60.0,
        )

        heartbeat.publish_visual_capture_progress(12, 60, 29)
        first = main._get_llm_extraction_job(
            str(self.project["id"]),
            self.document["id"],
            source_sha256=str(self.document["source_sha256"]),
        )
        with heartbeat._state_lock:
            heartbeat._publish_locked()
        repeated = main._get_llm_extraction_job(
            str(self.project["id"]),
            self.document["id"],
            source_sha256=str(self.document["source_sha256"]),
        )

        assert first is not None
        assert repeated is not None
        self.assertEqual(first["stage"], "source_ir")
        self.assertEqual(
            first["last_progress"],
            "已完成 12 页 PDF 视觉处理（授权上限 60 页，当前 P29）。",
        )
        self.assertEqual(
            first["last_progress_en"],
            "Processed 12 PDF pages (authorized ceiling: 60; current P29).",
        )
        self.assertEqual(
            repeated["last_progress"],
            first["last_progress"],
        )
        self.assertEqual(
            repeated["last_progress_en"],
            first["last_progress_en"],
        )

    def test_preparation_heartbeat_serializes_source_page_progress(self) -> None:
        job_id = "llm-extraction-" + "c" * 32
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": job_id,
                "project_id": self.project["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "RUNNING",
                "stage": "source_ir",
                # Skip the file-checkpoint branch: this test exercises the
                # live status projection, matching the existing visual-page
                # heartbeat coverage immediately above.
                "partial_revision": 1,
            }
        heartbeat = main._LlmExtractionPreparationHeartbeat(
            self.document["id"],
            job_id,
            interval_seconds=60.0,
        )

        heartbeat.publish_source_page_progress(37, 137, 38)
        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(status["job"]["stage"], "source_ir")
        self.assertEqual(status["job"]["progress"]["completed_pages"], 37)
        self.assertEqual(status["job"]["progress"]["total_pages"], 137)
        self.assertEqual(status["job"]["progress"]["current_page"], 38)
        self.assertEqual(status["job"]["progress"]["page_progress_kind"], "SOURCE")
        self.assertTrue(status["job"]["progress"]["page_started_at_utc"])
        self.assertEqual(
            status["job"]["last_progress"],
            "已处理 37/137 页，当前第 38 页。",
        )

    def test_worker_failure_stage_and_cause_survive_checkpoint_restore(self) -> None:
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        processing_plan = main.ResolvedProcessingPlanV1.model_validate(
            started["job"]["processing_plan"]
        )
        rejection = LlmShadowPreviewRejected(
            LlmShadowPreviewRejectionCode.VISUAL_CAPTURE_REJECTED,
            stage="visual_validation",
            cause_code="pdf_visual_text_provider_failed",
        )

        with (
            patch.object(
                main,
                "build_llm_requirement_document_preview",
                side_effect=rejection,
            ),
            patch.object(main.logger, "exception"),
        ):
            main._run_llm_document_extraction_worker(
                job_id=job_id,
                project_id=self.project["id"],
                document=self.document,
                provider=provider,  # type: ignore[arg-type]
                processing_plan=processing_plan,
            )

        terminal = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(terminal["job"]["status"], "FAILED")
        self.assertEqual(
            terminal["job"]["error_code"],
            "llm_shadow_preview_visual_capture_rejected",
        )
        self.assertEqual(terminal["job"]["error_stage"], "visual_validation")
        self.assertEqual(
            terminal["job"]["error_cause_code"],
            "pdf_visual_text_provider_failed",
        )

        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        restored = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(restored["job"]["status"], "FAILED")
        self.assertEqual(
            restored["job"]["error_code"],
            "llm_shadow_preview_visual_capture_rejected",
        )
        self.assertEqual(restored["job"]["error_stage"], "visual_validation")
        self.assertEqual(
            restored["job"]["error_cause_code"],
            "pdf_visual_text_provider_failed",
        )

    def test_background_worker_publishes_progress_and_reviewable_result(
        self,
    ) -> None:
        provider = object()
        thread = Mock()
        remote_plan = self._remote_processing_plan()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
            patch.object(
                main,
                "resolve_processing_plan",
                return_value=remote_plan,
            ),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        processing_plan = main.ResolvedProcessingPlanV1.model_validate(
            started["job"]["processing_plan"]
        )
        shadow = {
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "requirement_tree_projection_runtime_version": (
                main.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "mode": "EDITABLE_LLM_REVIEW",
            "collection": "llm_requirement_document",
            "serving_impact": "NONE",
            "source_sha256": self.document["source_sha256"],
            "pipeline": {
                "source_ir": {
                    "adapter_id": main.PDF_SOURCE_IR_ADAPTER_ID,
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
                "semantic": {
                    "planning_runtime_version": (
                        main.SEMANTIC_PLANNING_RUNTIME_VERSION
                    ),
                    "classifier_runtime_version": (
                        main.MODEL_SEMANTIC_CLASSIFIER_VERSION
                    ),
                    "assembler_runtime_version": (
                        main.MODEL_REQUIREMENT_ASSEMBLER_VERSION
                    ),
                    "document_runtime_version": (
                        main.LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
                    ),
                },
            },
        }

        observed_running_status: dict[str, object] = {}
        observed_preparing_status: dict[str, object] = {}
        observed_visual_status: dict[str, object] = {}

        def build_preview(
            *_args,
            progress_callback,
            preparation_progress_callback,
            visual_capture_progress_callback,
            partial_result_callback,
            **_kwargs,
        ):
            preparation_progress_callback("source_ir")
            observed_preparing_status.update(
                main.get_llm_document_extraction_status(
                    self.project["id"],
                    self.document["id"],
                    Response(),
                )
            )
            visual_capture_progress_callback(12, 60, 29)
            observed_visual_status.update(
                main.get_llm_document_extraction_status(
                    self.project["id"],
                    self.document["id"],
                    Response(),
                )
            )
            progress_callback(
                LlmRequirementDocumentProgress(
                    stage="planned",
                    total_clauses=3,
                    total_targets=8,
                    total_chunks=2,
                    completed_chunks=0,
                    completed_targets=0,
                    successful_chunks=0,
                    unresolved_chunks=0,
                    blocked_chunks=0,
                )
            )
            partial_result_callback(
                {
                    "schema_version": "extraction-llm-document-partial/v1",
                    "mode": "READ_ONLY_LLM_PARTIAL",
                    "revision": 1,
                    "editable": False,
                    "source_identity": {
                        "source_sha256": self.document["source_sha256"],
                    },
                    "coverage": {
                        "completed_chunks": 1,
                        "completed_chunk_indexes": [1],
                    },
                    "review_groups": [{"id": "clause-1"}],
                }
            )
            observed_running_status.update(
                main.get_llm_document_extraction_status(
                    self.project["id"],
                    self.document["id"],
                    Response(),
                )
            )
            progress_callback(
                LlmRequirementDocumentProgress(
                    stage="completed",
                    total_clauses=3,
                    total_targets=8,
                    total_chunks=2,
                    completed_chunks=2,
                    completed_targets=8,
                    successful_chunks=2,
                    unresolved_chunks=0,
                    blocked_chunks=0,
                )
            )
            return shadow

        with (
            patch.object(
                main,
                "build_llm_requirement_document_preview",
                side_effect=build_preview,
            ) as build_document,
            patch.object(
                main,
                "_bind_llm_requirement_review_session",
                return_value={
                    **shadow,
                    "llm_review": {
                        "schema_version": "llm-requirement-review-session/v4",
                        "model_prediction": {
                            "schema_version": "llm-requirement-prediction/v5",
                        },
                        "reviewed_result": {
                            "schema_version": "llm-requirement-review/v4",
                        },
                        "persistence": {},
                    },
                },
            ),
        ):
            main._run_llm_document_extraction_worker(
                job_id=job_id,
                project_id=self.project["id"],
                document=self.document,
                provider=provider,  # type: ignore[arg-type]
                processing_plan=processing_plan,
            )

        self.assertEqual(
            build_document.call_args.kwargs["max_concurrent_chunks"],
            4,
        )
        self.assertEqual(observed_preparing_status["job"]["stage"], "source_ir")
        self.assertIn(
            "页面、文本、表格、图片",
            observed_preparing_status["job"]["last_progress"],
        )
        self.assertIn(
            "pages, text, tables, images",
            observed_preparing_status["job"]["last_progress_en"],
        )
        self.assertNotEqual(
            observed_preparing_status["job"]["updated_at_utc"],
            started["job"]["updated_at_utc"],
        )
        self.assertEqual(
            observed_preparing_status["job"]["progress"]["total_chunks"],
            0,
        )
        self.assertEqual(observed_visual_status["job"]["stage"], "source_ir")
        self.assertEqual(
            observed_visual_status["job"]["last_progress"],
            "已完成 12 页 PDF 视觉处理（授权上限 60 页，当前 P29）。",
        )
        self.assertEqual(
            observed_visual_status["job"]["last_progress_en"],
            "Processed 12 PDF pages (authorized ceiling: 60; current P29).",
        )
        self.assertEqual(observed_running_status["job"]["status"], "RUNNING")
        self.assertIsNone(observed_running_status["result"])
        self.assertEqual(
            observed_running_status["partial_result"]["revision"],
            1,
        )
        self.assertFalse(observed_running_status["partial_result"]["editable"])
        self.assertEqual(
            observed_running_status["partial_result"]["job_id"],
            job_id,
        )
        self.assertEqual(
            observed_running_status["partial_result"]["review_groups"],
            [{"id": "clause-1"}],
        )
        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["status"], "COMPLETED")
        self.assertEqual(status["job"]["progress"]["completed_chunks"], 2)
        self.assertEqual(status["result"]["collection"], "llm_requirement_document")
        self.assertEqual(status["result"]["document_id"], self.document["id"])
        self.assertIsNone(status["partial_result"])

    def test_partial_result_revision_must_strictly_advance(self) -> None:
        job_id = "llm-extraction-" + "3" * 32
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": job_id,
                "project_id": self.project["id"],
                "document_id": self.document["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "RUNNING",
                "stage": "running",
                "total_clauses": 1,
                "total_targets": 1,
                "total_chunks": 2,
                "completed_chunks": 1,
                "completed_targets": 1,
                "successful_chunks": 1,
                "unresolved_chunks": 0,
                "blocked_chunks": 0,
                "current_chunk_index": 1,
                "current_chunk_target_count": 1,
                "error_code": "",
                "result": None,
                "partial_result": None,
                "partial_revision": 0,
                "partial_completed_chunk_indexes": (),
                "final_result_sha256": "",
                "created_at_utc": "2026-07-25T00:00:00+00:00",
                "started_at_utc": "2026-07-25T00:00:01+00:00",
                "finished_at_utc": "",
                "updated_at_utc": "2026-07-25T00:00:02+00:00",
            }
        partial = {
            "schema_version": "extraction-llm-document-partial/v1",
            "mode": "READ_ONLY_LLM_PARTIAL",
            "revision": 1,
            "editable": False,
            "source_identity": {
                "source_sha256": self.document["source_sha256"],
            },
            "coverage": {
                "completed_chunks": 1,
                "completed_chunk_indexes": [2],
            },
        }

        main._record_llm_extraction_partial_result(
            self.document["id"],
            job_id,
            project_id=self.project["id"],
            source_sha256=self.document["source_sha256"],
            partial_result=partial,
        )
        with self.assertRaisesRegex(
            ValueError,
            "partial result revision is not monotonic",
        ):
            main._record_llm_extraction_partial_result(
                self.document["id"],
                job_id,
                project_id=self.project["id"],
                source_sha256=self.document["source_sha256"],
                partial_result=partial,
            )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["partial_revision"], 1)
        self.assertEqual(
            status["partial_result"]["coverage"]["completed_chunk_indexes"],
            [2],
        )

    def test_active_partial_restores_as_interrupted_and_can_restart(self) -> None:
        provider = object()
        first_thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=first_thread),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        first_job_id = started["job"]["job_id"]
        assert first_job_id
        main._record_llm_extraction_progress(
            self.document["id"],
            first_job_id,
            LlmRequirementDocumentProgress(
                stage="running",
                total_clauses=3,
                total_targets=8,
                total_chunks=2,
                completed_chunks=1,
                completed_targets=4,
                successful_chunks=1,
                unresolved_chunks=0,
                blocked_chunks=0,
                current_chunk_index=2,
                current_chunk_target_count=4,
            ),
        )
        main._record_llm_extraction_partial_result(
            self.document["id"],
            first_job_id,
            project_id=self.project["id"],
            source_sha256=self.document["source_sha256"],
            partial_result={
                "schema_version": "extraction-llm-document-partial/v1",
                "mode": "READ_ONLY_LLM_PARTIAL",
                "revision": 1,
                "editable": False,
                "source_identity": {
                    "source_sha256": self.document["source_sha256"],
                },
                "coverage": {
                    "completed_chunks": 1,
                    "completed_chunk_indexes": [1],
                },
                "review_groups": [{"id": "clause-1"}],
            },
        )

        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        restored = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(restored["job"]["status"], "INTERRUPTED")
        self.assertEqual(restored["job"]["job_id"], first_job_id)
        self.assertEqual(
            restored["job"]["error_code"],
            "llm_document_extraction_interrupted",
        )
        self.assertIsNone(restored["result"])
        self.assertEqual(restored["partial_result"]["revision"], 1)
        self.assertFalse(restored["partial_result"]["editable"])
        self.assertEqual(
            restored["partial_result"]["recovery"]["mode"],
            "INTERRUPTED_CHECKPOINT",
        )

        second_thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=second_thread),
        ):
            restarted = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )

        self.assertEqual(restarted["job"]["status"], "PENDING")
        self.assertNotEqual(restarted["job"]["job_id"], first_job_id)
        self.assertIsNone(restarted["partial_result"])
        second_thread.start.assert_called_once_with()

    def test_composable_visual_partial_recovery_preserves_frozen_plan(self) -> None:
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
            ),
            patch.object(main, "Thread", return_value=thread),
            patch(
                "app.application.processing_profiles.require_capabilities",
            ),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )
        job_id = started["job"]["job_id"]
        visual_strategy = started["job"]["visual_strategy"]
        processing_plan = started["job"]["processing_plan"]
        assert job_id
        main._record_llm_extraction_progress(
            self.document["id"],
            job_id,
            LlmRequirementDocumentProgress(
                stage="running",
                total_clauses=1,
                total_targets=1,
                total_chunks=1,
                completed_chunks=1,
                completed_targets=1,
                successful_chunks=1,
                unresolved_chunks=0,
                blocked_chunks=0,
            ),
        )
        main._record_llm_extraction_partial_result(
            self.document["id"],
            job_id,
            project_id=self.project["id"],
            source_sha256=self.document["source_sha256"],
            partial_result={
                "schema_version": "extraction-llm-document-partial/v1",
                "mode": "READ_ONLY_LLM_PARTIAL",
                "revision": 1,
                "editable": False,
                "visual_strategy": visual_strategy,
                "source_identity": {
                    "source_sha256": self.document["source_sha256"],
                },
                "coverage": {
                    "completed_chunks": 1,
                    "completed_chunk_indexes": [1],
                },
            },
        )

        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()
        restored = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(restored["job"]["status"], "INTERRUPTED")
        self.assertEqual(restored["job"]["visual_strategy"], visual_strategy)
        self.assertEqual(
            restored["job"]["processing_plan"]["canonical_sha256"],
            processing_plan["canonical_sha256"],
        )
        self.assertEqual(
            restored["job"]["processing_plan"]["document"]["complex_vision"],
            "bailian_vision",
        )
        self.assertEqual(
            restored["partial_result"]["visual_strategy"],
            visual_strategy,
        )

    def test_composable_worker_publishes_latest_visual_provider_event(self) -> None:
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
            patch("app.application.processing_profiles.require_capabilities"),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
                main.LlmDocumentExtractionStartRequest(visual_provider="bailian"),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        processing_plan = main.ResolvedProcessingPlanV1.model_validate(
            started["job"]["processing_plan"]
        )

        def report_then_stop(*_args, **kwargs):
            callback = kwargs["provider_telemetry_callback"]
            callback(
                VisualTextProviderTelemetryV1(
                    page_number=8,
                    provider_id="bailian-page-layout",
                    provider_version="1.1.0+qwen3.7-flash+deadline.45s",
                    request_count=1,
                    latency_ms=45001,
                    cache_hit=False,
                    outcome="FAILED",
                )
            )
            raise LlmShadowPreviewRejected(
                LlmShadowPreviewRejectionCode.SOURCE_IR_REJECTED,
                stage="source_ir",
                cause_code="synthetic_stop_after_telemetry",
            )

        with (
            patch(
                "app.application.extraction.build_processing_plan_preview."
                "build_processing_plan_document_preview",
                side_effect=report_then_stop,
            ),
            patch.object(main.logger, "exception"),
        ):
            main._run_llm_document_extraction_worker(
                job_id=job_id,
                project_id=self.project["id"],
                document=self.document,
                provider=provider,  # type: ignore[arg-type]
                processing_plan=processing_plan,
            )

        status = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )
        self.assertEqual(status["job"]["status"], "FAILED")
        event = status["job"]["latest_visual_page_event"]
        self.assertEqual(
            {key: value for key, value in event.items() if key != "updated_at"},
            {
                "page_number": 8,
                "provider_id": "bailian-page-layout",
                "provider_version": "1.1.0+qwen3.7-flash+deadline.45s",
                "request_count": 1,
                "latency_ms": 45001,
                "cache_hit": False,
                "outcome": "FAILED",
            },
        )
        self.assertTrue(event["updated_at"])

    def test_visual_provider_status_write_failure_does_not_escape(self) -> None:
        event = VisualTextProviderTelemetryV1(
            page_number=8,
            provider_id="bailian-page-layout",
            provider_version="1.1.0",
            request_count=1,
            latency_ms=45001,
            cache_hit=False,
            outcome="FAILED",
        )

        with (
            patch.object(
                main,
                "_update_llm_extraction_job",
                side_effect=OSError("status transport unavailable"),
            ),
            patch(
                "app.application.extraction.document_jobs.logger.warning"
            ) as warning,
        ):
            main._record_visual_text_provider_telemetry(
                self.document["id"],
                "llm-extraction-" + "e" * 32,
                event,
            )

        warning.assert_called_once()

    def test_visual_provider_event_is_written_to_durable_progress(self) -> None:
        job_id = "llm-extraction-" + "f" * 32
        context = Mock(spec=JobExecutionContext)
        context.heartbeat.return_value = SimpleNamespace(cancel_requested=False)
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS[self.document["id"]] = {
                "job_id": job_id,
                "project_id": self.project["id"],
                "source_sha256": self.document["source_sha256"],
                "status": "RUNNING",
                "stage": "source_ir",
                "partial_revision": 1,
                "_durable_context": context,
            }

        main._record_visual_text_provider_telemetry(
            self.document["id"],
            job_id,
            VisualTextProviderTelemetryV1(
                page_number=7,
                provider_id="bailian-visual-text",
                provider_version="1.4.0+deadline.45s",
                request_count=0,
                latency_ms=4,
                cache_hit=True,
                outcome="CACHE_HIT",
            ),
        )

        progress = context.heartbeat.call_args.kwargs["progress"]
        event = progress["latest_visual_page_event"]
        self.assertEqual(
            {key: value for key, value in event.items() if key != "updated_at"},
            {
                "page_number": 7,
                "provider_id": "bailian-visual-text",
                "provider_version": "1.4.0+deadline.45s",
                "request_count": 0,
                "latency_ms": 4,
                "cache_hit": True,
                "outcome": "CACHE_HIT",
            },
        )
        self.assertEqual(
            set(event),
            {
                "page_number",
                "provider_id",
                "provider_version",
                "request_count",
                "latency_ms",
                "cache_hit",
                "outcome",
                "updated_at",
            },
        )

    def test_matching_final_cache_wins_over_finalizing_checkpoint(self) -> None:
        provider = object()
        thread = Mock()
        remote_plan = self._remote_processing_plan()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
            patch.object(
                main,
                "resolve_processing_plan",
                return_value=remote_plan,
            ),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        processing_plan = main.ResolvedProcessingPlanV1.model_validate(
            started["job"]["processing_plan"]
        )
        cached_result = {
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "requirement_tree_projection_runtime_version": (
                main.REQUIREMENT_TREE_PROJECTION_RUNTIME_VERSION
            ),
            "mode": "EDITABLE_LLM_REVIEW",
            "collection": "llm_requirement_document",
            "status": "READY",
            "source_sha256": self.document["source_sha256"],
            "processing_plan": processing_plan.model_dump(mode="json"),
            "result_runtime_sha256": (
                main._document_extraction_result_runtime_sha256(processing_plan)
            ),
            "pipeline": {
                "source_ir": {
                    "adapter_id": main.PDF_SOURCE_IR_ADAPTER_ID,
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
                "semantic": {
                    "planning_runtime_version": (
                        main.SEMANTIC_PLANNING_RUNTIME_VERSION
                    ),
                    "classifier_runtime_version": (
                        main.MODEL_SEMANTIC_CLASSIFIER_VERSION
                    ),
                    "assembler_runtime_version": (
                        main.MODEL_REQUIREMENT_ASSEMBLER_VERSION
                    ),
                    "document_runtime_version": (
                        main.LLM_REQUIREMENT_DOCUMENT_RUNTIME_VERSION
                    ),
                },
            },
            "counts": {
                "semantic_chunks": 2,
                "successful_provisions": 2,
                "semantic_annotations": 3,
            },
            "llm_review": {
                "schema_version": "llm-requirement-review-session/v4",
                "model_prediction": {
                    "schema_version": "llm-requirement-prediction/v5",
                },
                "reviewed_result": {
                    "schema_version": "llm-requirement-review/v4",
                },
            },
        }
        digest = main.canonical_llm_extraction_payload_sha256(cached_result)
        main._update_llm_extraction_job(
            self.document["id"],
            job_id,
            persist_checkpoint=True,
            status="RUNNING",
            stage="finalizing",
            final_result_sha256=digest,
        )
        main.LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload=cached_result,
        )
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()

        restored = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(restored["job"]["status"], "COMPLETED")
        self.assertEqual(restored["result"]["status"], "READY")
        self.assertIsNone(restored["partial_result"])
        self.assertIsNone(
            main.LlmExtractionJobCheckpointCache(self.root).load(
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.document["source_sha256"],
            )
        )

    def test_active_checkpoint_wins_over_older_completed_cache(self) -> None:
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        older_result = {
            "schema_version": main.LLM_SHADOW_PREVIEW_SCHEMA_VERSION,
            "mode": "EDITABLE_LLM_REVIEW",
            "collection": "llm_requirement_document",
            "status": "READY",
            "pipeline": {
                "source_ir": {
                    "version": main.PDF_SOURCE_IR_ADAPTER_VERSION,
                },
                "structure": {
                    "version": main.DEFAULT_STRUCTURE_RESOLVER_VERSION,
                },
            },
            "counts": {},
            "llm_review": {
                "schema_version": "llm-requirement-review-session/v4",
                "model_prediction": {
                    "schema_version": "llm-requirement-prediction/v5",
                },
                "reviewed_result": {
                    "schema_version": "llm-requirement-review/v4",
                },
            },
        }
        main.LlmExtractionResultCache(self.root).save(
            project_id=self.project["id"],
            document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            payload=older_result,
        )
        with main._LLM_EXTRACTION_JOB_LOCK:
            main._LLM_EXTRACTION_JOBS.clear()

        restored = main.get_llm_document_extraction_status(
            self.project["id"],
            self.document["id"],
            Response(),
        )

        self.assertEqual(restored["job"]["status"], "INTERRUPTED")
        self.assertEqual(restored["job"]["job_id"], job_id)
        self.assertIsNone(restored["result"])
        self.assertIsNone(restored["partial_result"])

    def test_deleted_document_worker_cannot_bind_or_recreate_cached_result(
        self,
    ) -> None:
        provider = object()
        thread = Mock()
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                return_value=provider,
            ),
            patch.object(main, "Thread", return_value=thread),
        ):
            started = main.start_llm_document_extraction(
                self.project["id"],
                self.document["id"],
                Response(),
            )
        job_id = started["job"]["job_id"]
        assert job_id
        processing_plan = main.ResolvedProcessingPlanV1.model_validate(
            started["job"]["processing_plan"]
        )

        def build_then_delete(*_args, **_kwargs):
            main.delete_sor_document(self.project["id"], self.document["id"])
            return {
                "mode": "EDITABLE_LLM_REVIEW",
                "collection": "llm_requirement_document",
                "serving_impact": "NONE",
            }

        with (
            patch.object(
                main,
                "build_llm_requirement_document_preview",
                side_effect=build_then_delete,
            ),
            patch.object(
                main,
                "_bind_llm_requirement_review_session",
                side_effect=AssertionError("deleted document must not bind a review"),
            ) as bind_review,
            patch.object(
                main.LlmExtractionResultCache,
                "save",
                side_effect=AssertionError("deleted document must not recreate cache"),
            ) as save_cache,
        ):
            main._run_llm_document_extraction_worker(
                job_id=job_id,
                project_id=self.project["id"],
                document=self.document,
                provider=provider,  # type: ignore[arg-type]
                processing_plan=processing_plan,
            )

        self.assertIsNone(db.get_sor_document(self.document["id"]))
        bind_review.assert_not_called()
        save_cache.assert_not_called()
        with main._LLM_EXTRACTION_JOB_LOCK:
            self.assertNotIn(self.document["id"], main._LLM_EXTRACTION_JOBS)
        self.assertIsNone(
            main.LlmExtractionJobCheckpointCache(self.root).load(
                project_id=self.project["id"],
                document_id=self.document["id"],
                source_sha256=self.document["source_sha256"],
            )
        )

    def test_llm_endpoint_requires_trusted_hash_before_provider_setup(self) -> None:
        untrusted_document = {**self.document, "source_sha256": ""}
        with (
            patch.object(db, "get_sor_document", return_value=untrusted_document),
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                side_effect=AssertionError("provider must not be constructed"),
            ) as provider_factory,
            self.assertRaises(HTTPException) as caught,
        ):
            main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(
                    collection="llm_requirement_batch",
                    section_paths=(("1", "1.1"),),
                ),
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_document_source_identity_unavailable",
        )
        provider_factory.assert_not_called()

    def test_llm_endpoint_requires_selected_scope_before_provider_setup(self) -> None:
        with (
            patch.object(
                main,
                "_build_llm_shadow_semantic_provider",
                side_effect=AssertionError("provider must not be constructed"),
            ) as provider_factory,
            self.assertRaises(HTTPException) as caught,
        ):
            main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(
                    collection="llm_requirement_batch",
                ),
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_shadow_preview_empty_section_paths",
        )
        provider_factory.assert_not_called()

    def test_non_llm_preview_rejects_section_paths(self) -> None:
        with (
            patch.object(
                main,
                "build_extraction_shadow_preview",
                side_effect=AssertionError("invalid request must not run extraction"),
            ) as build_preview,
            self.assertRaises(HTTPException) as caught,
        ):
            main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(
                    collection="document_outline",
                    section_paths=(("1", "1.1"),),
                ),
                Response(),
            )

        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(
            caught.exception.detail["code"],
            "shadow_preview_unexpected_section_paths",
        )
        build_preview.assert_not_called()

    def test_llm_provider_uses_secret_and_pinned_qwen37_snapshot(self) -> None:
        provider = object()
        config = SimpleNamespace(
            dashscope_api_key="test-secret",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            chat_model="some-other-configured-model",
        )
        with (
            patch.object(
                main,
                "load_ai_copilot_config",
                return_value=config,
            ) as load_config,
            patch.object(
                main,
                "BailianQwenSemanticProvider",
                return_value=provider,
            ) as provider_type,
        ):
            result = main._build_llm_shadow_semantic_provider()

        self.assertIs(result, provider)
        load_config.assert_called_once_with("aliyun")
        provider_type.assert_called_once_with(
            api_key="test-secret",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model=main.DEFAULT_BAILIAN_EVALUATION_MODEL,
            seed=main.DEFAULT_BAILIAN_SEMANTIC_SEED,
            timeout_seconds=90.0,
            max_retries=1,
        )

    def test_llm_provider_rejects_missing_secret_without_constructor(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="",
            dashscope_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        with (
            patch.object(main, "load_ai_copilot_config", return_value=config),
            patch.object(
                main,
                "BailianQwenSemanticProvider",
                side_effect=AssertionError("invalid configuration must stop locally"),
            ) as provider_type,
            self.assertRaises(HTTPException) as caught,
        ):
            main._build_llm_shadow_semantic_provider()

        self.assertEqual(caught.exception.status_code, 503)
        self.assertEqual(
            caught.exception.detail["code"],
            "llm_extraction_provider_not_configured",
        )
        provider_type.assert_not_called()

    def test_formal_workspace_exposes_unified_requirement_tree(
        self,
    ) -> None:
        html = (main.STATIC_DIR / "index.html").read_text(encoding="utf-8")
        frontend_source = Path(__file__).resolve().parents[2] / "packages/workbench-ui/src"
        extraction = (
            frontend_source / "features" / "extraction" / "RequirementTreePanel.tsx"
        ).read_text(encoding="utf-8")
        routes = (Path(__file__).resolve().parents[2] / "apps/web/src/app/App.tsx").read_text(encoding="utf-8")

        self.assertIn('<div id="root"></div>', html)
        self.assertIn("需求结构树", extraction)
        self.assertIn("function CompleteContent(", extraction)
        self.assertIn("<CompleteContent", extraction)
        self.assertIn('value: "content"', extraction)
        self.assertIn('value: "source"', extraction)
        self.assertIn("source_refs", extraction)
        self.assertIn("repairExtractionVisualPages", extraction)
        self.assertIn("只重试降级的", extraction)
        self.assertNotIn("confirmCurrentAndAdvance", extraction)
        self.assertIn('location.pathname === "/workspace"', routes)
        self.assertIn('location.pathname === "/extraction-review"', routes)
        self.assertIn("<LegacyWorkspaceRedirect", routes)

    def test_source_crop_endpoint_returns_a_bounded_png(self) -> None:
        response = main.get_document_source_crop(
            self.project["id"],
            self.document["id"],
            source_sha256=self.document["source_sha256"],
            page=1,
            x0=60.0,
            y0=50.0,
            x1=330.0,
            y1=175.0,
        )

        self.assertEqual(response.media_type, "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_legacy_upload_crop_is_bound_to_the_preview_run_hash(self) -> None:
        legacy_document = {**self.document, "source_sha256": ""}
        with patch.object(db, "get_sor_document", return_value=legacy_document):
            payload = main.build_sor_extraction_shadow_preview(
                self.project["id"],
                self.document["id"],
                main.ExtractionShadowPreviewRequest(
                    collection="document_outline",
                    limit=5_000,
                ),
                Response(),
            )
            preview_sha256 = payload["source_identity"]["source_sha256"]
            self.source.write_bytes(self.source.read_bytes() + b"\n")

            with self.assertRaises(HTTPException) as context:
                main.get_document_source_crop(
                    self.project["id"],
                    self.document["id"],
                    source_sha256=preview_sha256,
                    page=1,
                    x0=60.0,
                    y0=50.0,
                    x1=330.0,
                    y1=175.0,
                )

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(
            context.exception.detail["code"],
            "pdf_source_crop_source_unavailable",
        )

    def test_pdf_viewer_endpoint_serves_the_registered_source_bytes_inline(
        self,
    ) -> None:
        response = main.get_sor_document_file(
            self.project["id"],
            self.document["id"],
        )

        self.assertEqual(response.media_type, "application/pdf")
        self.assertIn("inline", response.headers["Content-Disposition"])
        served_path = Path(str(response.path))
        self.assertEqual(
            hashlib.sha256(served_path.read_bytes()).hexdigest(),
            self.document["source_sha256"],
        )


def _visual_text_snapshot(
    *,
    source_sha256: str,
    page_number: int,
    page_image_sha256: str,
    raw_text: str,
    confidence: float | None,
    with_geometry_receipt: bool = False,
) -> VisualTextSnapshot:
    provider_id = "fixture-visual-text"
    provider_version = "p3-test-v1"
    page_bbox = (0.0, 0.0, 595.0, 842.0)
    bbox = (80.0, 120.0, 420.0, 140.0)
    observation = VisualTextObservation(
        observation_id=stable_visual_text_observation_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            observation_index=0,
            bbox=bbox,
        ),
        observation_index=0,
        source_sha256=source_sha256,
        page_number=page_number,
        bbox=bbox,
        raw_text=raw_text,
        confidence=confidence,
        provider_id=provider_id,
        provider_version=provider_version,
        page_image_sha256=page_image_sha256,
        geometry_receipt=(
            VisualTextGeometryReceipt(
                provider_quadrilateral_px=(
                    (160.0, 240.0),
                    (840.0, 240.0),
                    (840.0, 280.0),
                    (160.0, 280.0),
                ),
                original_page_quadrilateral_px=(
                    (160.0, 240.0),
                    (840.0, 240.0),
                    (840.0, 280.0),
                    (160.0, 280.0),
                ),
                provider_image_size_px=(1190, 1684),
                original_page_image_size_px=(1190, 1684),
                input_rotation_degrees=0,
                target_bbox=bbox,
            )
            if with_geometry_receipt
            else None
        ),
    )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1190,
            page_image_height_px=1684,
        ),
        source_sha256=source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1190,
        page_image_height_px=1684,
        observations=(observation,),
    )


def _visual_layout_snapshot(
    *,
    source_sha256: str,
    page_number: int,
    page_image_sha256: str,
) -> VisualTextSnapshot:
    provider_id = "fixture-page-layout"
    provider_version = "p3.2-layout-test-v1"
    page_bbox = (0.0, 0.0, 595.0, 842.0)
    specs = (
        {
            "content_type": "TEXT",
            "bbox": (50.0, 50.0, 500.0, 120.0),
            "raw_text": "目录\n一、总体说明",
            "role": "TOC",
        },
        {
            "content_type": "TABLE",
            "bbox": (50.0, 150.0, 500.0, 500.0),
            "raw_text": "",
            "layout_table": VisualLayoutTable(
                row_count=2,
                column_count=1,
                header_row_count=1,
                continues_next_page=True,
                cells=(
                    VisualLayoutTableCell(row=0, column=0, raw_text="项目"),
                    VisualLayoutTableCell(
                        row=1,
                        column=0,
                        raw_text="图示\n要求",
                        horizontal_alignment="CENTER",
                        vertical_alignment="MIDDLE",
                        indent_level=1,
                    ),
                ),
            ),
        },
        {
            "content_type": "FIGURE",
            "bbox": (100.0, 260.0, 260.0, 430.0),
            "raw_text": "",
            "parent_observation_index": 1,
            "parent_table_cell": (1, 0),
        },
        {
            "content_type": "HEADER",
            "bbox": (50.0, 10.0, 500.0, 30.0),
            "raw_text": "受控文件",
        },
        {
            "content_type": "FOOTER",
            "bbox": (280.0, 810.0, 310.0, 830.0),
            "raw_text": "24",
        },
        {
            "content_type": "WATERMARK",
            "bbox": (200.0, 300.0, 400.0, 400.0),
            "raw_text": "内部",
        },
    )
    observations = []
    for index, spec in enumerate(specs):
        bbox = spec["bbox"]
        observation_id = stable_visual_text_observation_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            observation_index=index,
            bbox=bbox,
        )
        observations.append(
            VisualTextObservation(
                observation_id=observation_id,
                observation_index=index,
                source_sha256=source_sha256,
                page_number=page_number,
                bbox=bbox,
                raw_text=spec["raw_text"],
                content_type=spec["content_type"],
                role=spec.get("role"),
                layout_table=spec.get("layout_table"),
                parent_observation_index=spec.get("parent_observation_index"),
                parent_table_cell=spec.get("parent_table_cell"),
                confidence=None,
                provider_id=provider_id,
                provider_version=provider_version,
                page_image_sha256=page_image_sha256,
            )
        )
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=source_sha256,
            page_image_sha256=page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_number,
            page_bbox=page_bbox,
            page_image_width_px=1190,
            page_image_height_px=1684,
        ),
        source_sha256=source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_number,
        page_bbox=page_bbox,
        page_image_sha256=page_image_sha256,
        page_image_width_px=1190,
        page_image_height_px=1684,
        evaluated_content_types=VISUAL_CONTENT_TYPES,
        observations=tuple(observations),
    )


class _FixtureRemoteVisualTextProvider:
    provider_id = visual_text_provider_id("bailian")
    provider_version = "fixture-p3-v1"

    def recognize_page(self, page_input: VisualTextPageInput) -> VisualTextSnapshot:
        bbox = (80.0, 120.0, 420.0, 140.0)
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
            raw_text="Remote captured text",
            confidence=0.94,
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
            page_image_media_type=page_input.page_image_media_type,
            page_image_width_px=page_input.page_image_width_px,
            page_image_height_px=page_input.page_image_height_px,
            observations=(observation,),
        )


def _write_full_page_image_pdf(path: Path) -> None:
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), False)
    pixmap.clear_with(0xFFFFFF)
    image_bytes = pixmap.tobytes("png")
    document = fitz.open()
    page = document.new_page()
    page.insert_image(page.rect, stream=image_bytes)
    document.save(path)
    document.close()


def _rendered_page_sha256(path: Path) -> str:
    document = fitz.open(path)
    try:
        pixmap = document[0].get_pixmap(
            matrix=fitz.Matrix(2.0, 2.0),
            alpha=False,
            annots=False,
        )
        return hashlib.sha256(pixmap.tobytes("png")).hexdigest()
    finally:
        document.close()


def _write_native_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "1 Product requirements", fontsize=16)
    page.insert_text(
        (72, 120),
        "1.1 The supplier shall provide a validated sensor assembly.",
        fontsize=11,
    )
    page.insert_text(
        (72, 155),
        "1.2 The sensor response time shall be less than 100 ms.",
        fontsize=11,
    )
    document.save(path)
    document.close()


def _write_pdf_with_preface(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Product development specification", fontsize=18)
    page.insert_text((72, 115), "Document number: ABC-001", fontsize=11)
    page = document.new_page()
    page.insert_text((72, 72), "1 Scope", fontsize=16)
    page.insert_text((72, 120), "1.1 Supplier shall provide the product.", fontsize=11)
    document.save(path)
    document.close()


def _write_numbered_section_with_native_blocks(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "2.4 Sensor performance", fontsize=16)
    page.insert_text(
        (72, 120),
        "The supplier shall provide a validated sensor assembly.",
        fontsize=11,
    )
    page.insert_text(
        (72, 155),
        "The sensor response time shall be less than 100 ms.",
        fontsize=11,
    )
    document.save(path)
    document.close()


def _write_pdf_with_embedded_numbered_sections(path: Path) -> None:
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "4.1 Product tests", fontsize=16)
    page.insert_textbox(
        fitz.Rect(72, 110, 530, 250),
        (
            "4.1.1 Initial conditions shall be recorded.\n"
            "4.1.2 At least 3 samples shall be tested.\n"
            "4.1.3 The final report shall contain every result."
        ),
        fontsize=11,
    )
    document.save(path)
    document.close()


def _write_native_pdf_with_table(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=360, height=280)
    page.insert_text(
        (40, 30),
        "3.1 The supplier shall meet all values in Table 1.",
        fontsize=11,
    )
    left = 40
    top = 70
    cell_width = 130
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


def _write_native_pdf_with_table_cell_image(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=360, height=300)
    page.insert_text((40, 30), "3.1 Table image requirement", fontsize=11)
    left = 40
    top = 70
    cell_width = 130
    cell_height = 70
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
    for row, values in enumerate((("Item", "Evidence"), ("A", ""))):
        for column, value in enumerate(values):
            if value:
                page.insert_text(
                    (
                        left + column * cell_width + 6,
                        top + row * cell_height + 22,
                    ),
                    value,
                    fontsize=9,
                )
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pixmap.clear_with(0x00A060)
    page.insert_image(
        fitz.Rect(
            left + cell_width + 35,
            top + cell_height + 18,
            left + cell_width + 75,
            top + cell_height + 58,
        ),
        stream=pixmap.tobytes("png"),
    )
    document.save(path)
    document.close()


def _write_pdf_with_cross_page_table(path: Path) -> None:
    document = fitz.open()
    first_page = document.new_page(width=360, height=320)
    first_page.insert_text((40, 28), "4.1 Continued table requirement", fontsize=13)
    _draw_two_column_table(
        first_page,
        top=140,
        row_height=55,
        rows=(("Item", "Value"), ("A", "Page 1"), ("B", "Tail 1")),
    )
    second_page = document.new_page(width=360, height=320)
    _draw_two_column_table(
        second_page,
        top=8,
        row_height=55,
        rows=(("Item", "Value"), ("C", "Page 2"), ("D", "Tail 2")),
    )
    document.save(path)
    document.close()


def _draw_two_column_table(
    page: fitz.Page,
    *,
    top: float,
    row_height: float,
    rows: tuple[tuple[str, str], ...],
) -> None:
    left = 40
    cell_width = 130
    for row in range(len(rows) + 1):
        y = top + row * row_height
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
            (x, top + len(rows) * row_height),
            color=(0, 0, 0),
            width=1,
        )
    for row, values in enumerate(rows):
        for column, value in enumerate(values):
            page.insert_text(
                (
                    left + column * cell_width + 6,
                    top + row * row_height + 24,
                ),
                value,
                fontsize=9,
            )


def _write_pdf_with_toc_and_changing_page_numbers(path: Path) -> None:
    document = fitz.open()
    pages = (
        (
            (72, 55, "CONTENTS", 18),
            (72, 100, "1 Overview ........ 3", 12),
            (88, 128, "1.1 Schedule ...... 3", 11),
        ),
        (
            (72, 80, "2 Requirements .... 4", 12),
            (88, 108, "2.1 Performance ... 4", 11),
        ),
        (
            (72, 70, "1 Overview", 16),
            (72, 110, "1.1 Schedule", 13),
            (88, 145, "The project schedule is provided for planning.", 11),
        ),
        (
            (72, 70, "2 Requirements", 16),
            (72, 110, "2.1 Performance", 13),
            (88, 145, "The supplier shall meet the stated performance target.", 11),
        ),
    )
    for page_index, entries in enumerate(pages, start=1):
        page = document.new_page(width=595, height=800)
        for x, y, text, size in entries:
            page.insert_text((x, y), text, fontsize=size)
        page.insert_text((290, 780), str(100 + page_index), fontsize=9)
    document.save(path)
    document.close()


def _write_pdf_with_midpage_numbers(path: Path) -> None:
    document = fitz.open()
    for page_index, value in enumerate(("11", "12", "13")):
        page = document.new_page(width=595, height=800)
        if page_index == 0:
            page.insert_text((72, 70), "1 Scope", fontsize=16)
        page.insert_text((290, 300), value, fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_mirrored_and_roman_page_numbers(path: Path) -> None:
    document = fitz.open()
    for page_index, marker in enumerate(("I", "II", "III", "IV"), start=1):
        page = document.new_page(width=595, height=800)
        page.insert_text((72, 80), f"Front matter {page_index}", fontsize=11)
        page.insert_text((535, 780), marker, fontsize=9)
    for printed_page in range(1, 5):
        page = document.new_page(width=595, height=800)
        page.insert_text((72, 80), f"{printed_page} Body", fontsize=14)
        x = 30 if printed_page % 2 == 0 else 553
        page.insert_text((x, 780), str(printed_page), fontsize=9)
    document.save(path)
    document.close()


def _write_pdf_with_combined_pagination_footer(path: Path) -> None:
    document = fitz.open()
    for page_number in range(1, 5):
        page = document.new_page(width=595, height=800)
        page.insert_text((72, 70), f"{page_number} Body section", fontsize=14)
        page.insert_text((72, 110), "Supplier shall comply.", fontsize=11)
        page.insert_textbox(
            fitz.Rect(72, 765, 530, 798),
            f"{page_number}\nDOC-CODE-2026",
            fontsize=9,
        )
    document.save(path)
    document.close()


def _write_pdf_with_multiline_toc_blocks(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((260, 55), "CONTENTS", fontsize=18)
    page.insert_textbox(
        fitz.Rect(72, 90, 530, 220),
        "Preface ............ III\n1 Scope ............ 1\n2 References ....... 2",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 80, 530, 220),
        "3 Terms ............ 3\n4 Requirements ..... 4\nAppendix A ......... 8",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "1 Scope", fontsize=16)
    page.insert_text((72, 110), "1.1 Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_headingless_multiline_toc(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 90, 530, 220),
        "1 Scope ............ 3\n2 References ....... 4\n3 Terms ............ 5",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 80, 530, 180),
        "4 Requirements ..... 6\nAppendix A ......... 8",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "1 Scope", fontsize=16)
    page.insert_text((72, 110), "1.1 Supplier shall comply.", fontsize=11)
    for page_number in range(4, 9):
        page = document.new_page(width=595, height=800)
        page.insert_text(
            (72, 90),
            f"Supporting content on page {page_number}.",
            fontsize=11,
        )
    document.save(path)
    document.close()


def _write_pdf_with_short_headingless_cross_reference_list(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 70, 530, 150),
        "1 Related topic ........ 8\n2 Supporting topic ..... 9",
        fontsize=11,
    )
    page.insert_text((72, 220), "3 Body", fontsize=16)
    page.insert_text((72, 260), "Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_body_before_three_target_lines(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "1 Body", fontsize=16)
    page.insert_text((72, 110), "Supplier shall comply.", fontsize=11)
    page.insert_textbox(
        fitz.Rect(72, 360, 530, 470),
        "2 Related topic ........ 4\n3 Supporting topic ..... 5\n4 Final topic .......... 6",
        fontsize=11,
    )
    for page_number in range(2, 7):
        page = document.new_page(width=595, height=800)
        page.insert_text((72, 90), f"Page {page_number} body content.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_page_top_standard_year_references(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 70, 530, 180),
        "ISO 9001 ........ 2015\nISO 26262 ...... 2018\nGB 7258 ........ 2017",
        fontsize=11,
    )
    page.insert_text((72, 240), "1 Scope", fontsize=16)
    page.insert_text((72, 280), "Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_headingless_target_prefix_before_body(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 70, 530, 180),
        "1 Calibration point A .... 3\n2 Calibration point B .... 4\n3 Calibration point C .... 5",
        fontsize=11,
    )
    page.insert_text((72, 230), "4 Test procedure", fontsize=16)
    page.insert_text((72, 270), "Supplier shall record the result.", fontsize=11)
    for page_number in range(2, 6):
        page = document.new_page(width=595, height=800)
        page.insert_text((72, 90), f"Page {page_number} body content.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_headingless_label_offset_toc(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 70, 530, 220),
        "1.0\n........................ 1\n"
        "1.1\n........................ 1\n"
        "2.0\n........................ 2",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 90), "Front matter", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "1.0", fontsize=16)
    page.insert_text((72, 120), "1.1", fontsize=16)
    page.insert_text((72, 160), "Body content.", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "2.0", fontsize=16)
    page.insert_text((72, 110), "More body content.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_full_page_calibration_cross_reference(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_textbox(
        fitz.Rect(72, 70, 530, 180),
        "1 Calibration point A ........ 3\n"
        "2 Calibration point B ........ 4\n"
        "3 Calibration point C ........ 5",
        fontsize=11,
    )
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "4 Test procedure", fontsize=16)
    page.insert_text((72, 110), "Apply the calibration values above.", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 90), "2 Calibration point A", fontsize=16)
    page.insert_text((72, 130), "Recorded result.", fontsize=11)
    for page_number in range(4, 6):
        page = document.new_page(width=595, height=800)
        page.insert_text(
            (72, 90),
            f"Recorded result on page {page_number}.",
            fontsize=11,
        )
    document.save(path)
    document.close()


def _write_pdf_with_mixed_target_explicit_toc(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((245, 55), "CONTENTS", fontsize=18)
    page.insert_text((72, 100), "1 Scope ........ 2", fontsize=11)
    page.insert_text((72, 128), "2 References", fontsize=11)
    page.insert_text((72, 156), "3 Terms", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "1 Scope", fontsize=16)
    page.insert_text((72, 110), "Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_same_page_toc_and_body(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 55), "CONTENTS", fontsize=18)
    page.insert_text((72, 100), "1 Overview", fontsize=12)
    page.insert_text((88, 128), "1.1 Details", fontsize=11)
    page.insert_text((72, 400), "2 Body", fontsize=16)
    page.insert_text((72, 440), "2.1 Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_toc_continuation_and_body(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 55), "CONTENTS", fontsize=18)
    page.insert_text((72, 100), "1 Overview", fontsize=12)
    page.insert_text((88, 128), "1.1 Details", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 80), "2 Requirements .... 3", fontsize=12)
    page.insert_text((88, 108), "2.1 Performance ... 3", fontsize=11)
    page.insert_text((72, 400), "3 Body", fontsize=16)
    page.insert_text((72, 440), "3.1 Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _write_pdf_with_weak_toc_then_body(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 55), "CONTENTS", fontsize=18)
    page.insert_text((72, 100), "1 Overview", fontsize=12)
    page.insert_text((88, 128), "1.1 Details", fontsize=11)
    page = document.new_page(width=595, height=800)
    page.insert_text((72, 70), "2 Body", fontsize=16)
    page.insert_text((72, 110), "2.1 Supplier shall comply.", fontsize=11)
    document.save(path)
    document.close()


def _authorized_api_visual_runtime(
    source_sha256: str,
) -> main.AuthorizedPdfVisualRuntime:
    adapter = SimpleNamespace(
        adapter_version=(f"{main.PDF_SOURCE_IR_ADAPTER_VERSION}+visual.{'8' * 12}")
    )
    runtime = SimpleNamespace(
        pdf_adapter=adapter,
        visual_text_provider=SimpleNamespace(
            provider_id=visual_text_provider_id("bailian"),
            provider_version="test-visual-provider/v1",
        ),
        route=SimpleNamespace(
            source_sha256=source_sha256,
            page_numbers=frozenset({1}),
        ),
    )
    route = SimpleNamespace(
        source_sha256=source_sha256,
        route_id=f"pvr-{'9' * 32}",
        route_sha256="9" * 64,
    )
    return main.AuthorizedPdfVisualRuntime(
        provider_name="bailian",
        route=route,
        runtime=runtime,
    )


if __name__ == "__main__":
    unittest.main()
