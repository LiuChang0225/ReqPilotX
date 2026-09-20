from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import base64
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from app.adapters.documents import pdf_source_ir_adapter as pdf_adapter_module
from app.adapters.documents.pdf_source_ir_adapter import PdfSourceIRAdapter
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction import processing_visual_chain
from app.application.extraction.processing_pipeline import (
    build_local_structure_document_result,
)
from app.application.extraction.processing_visual_chain import (
    ComposablePageVisualProvider,
    build_composable_pdf_source_ir_adapter,
    page_processing_receipts_from_adapter,
)
from app.domain.extraction.processing_profile import (
    DocumentProcessingProfileV1,
    ResolvedProviderBindingsV1,
    ResolvedProcessingPlanV1,
    build_local_rules_profile,
    build_resolved_processing_plan,
)
from app.domain.extraction.source_ir import SourceIRSnapshot, SourceIRStatus
from app.domain.extraction.visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualTextObservation,
    VisualTextPageInput,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.domain.extraction.visual_table import (
    VisualTableCell,
    VisualTableSnapshot,
    stable_visual_table_cell_id,
    stable_visual_table_snapshot_id,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)
from app.ports.visual_table_provider import (
    VisualTableProviderRejected,
    VisualTableProviderRejectionCode,
)
from app.services.visual_text_snapshot_cache import (
    VisualTextProviderTelemetryV1,
    VisualTextSnapshotCache,
    VisualTextSnapshotCacheBinding,
)


_RASTER_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJRU5ErkJggg=="
)


def _page_input(page_number: int = 1) -> VisualTextPageInput:
    source_bytes = b"source-pdf"
    image_bytes = b"page-image"
    return VisualTextPageInput(
        source_bytes=source_bytes,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        page_number=page_number,
        page_bbox=(0.0, 0.0, 600.0, 800.0),
        page_image_bytes=image_bytes,
        page_image_sha256=hashlib.sha256(image_bytes).hexdigest(),
        page_image_width_px=1200,
        page_image_height_px=1600,
    )


def _write_full_page_raster_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    page.insert_image(page.rect, stream=_RASTER_PNG_BYTES)
    document.save(path)
    document.close()


def _write_two_by_two_table_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=600, height=800)
    for x in (60, 160, 260):
        page.draw_line((x, 100), (x, 200), color=(0, 0, 0))
    for y in (100, 150, 200):
        page.draw_line((60, y), (260, y), color=(0, 0, 0))
    for position, text_value in (
        ((80, 130), "A"),
        ((180, 130), "B"),
        ((80, 180), "C"),
        ((180, 180), "D"),
    ):
        page.insert_text(position, text_value)
    document.save(path)
    document.close()


def _bailian_ocr_plan(*, source_sha256: str) -> ResolvedProcessingPlanV1:
    profile = build_local_rules_profile(project_id="project-a", revision=7)
    return build_resolved_processing_plan(
        project_id="project-a",
        document_id="document-a",
        source_sha256=source_sha256,
        profile=profile,
        document=DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=("bailian_ocr",),
            auto_escalation=False,
        ),
        llm_provider="none",
        provider_bindings=ResolvedProviderBindingsV1(
            primary_parser_provider="local:pymupdf",
            enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
        ),
        connection_revision=22,
        force_visual_pages=(1,),
    )


def _extract_with_test_ocr_route(
    source: Path,
    *,
    page_provider: _VisualProvider,
    table_provider: _VisualTableProvider,
) -> tuple[PdfSourceIRAdapter, SourceIRSnapshot]:
    report = LocalDocumentPreflight().inspect(source)
    plan = _bailian_ocr_plan(source_sha256=report.source_sha256)
    with (
        patch.object(
            processing_visual_chain,
            "_build_enhancement_provider",
            return_value=page_provider,
        ),
        patch.object(
            processing_visual_chain,
            "_build_bailian_visual_table_provider",
            return_value=table_provider,
        ),
    ):
        adapter = build_composable_pdf_source_ir_adapter(plan)
        snapshot = adapter.extract(
            source,
            preflight=report,
            created_at=datetime.now(UTC),
        )
    return adapter, snapshot


def _snapshot(
    provider_id: str,
    provider_version: str,
    page_input: VisualTextPageInput,
    *,
    full_layout: bool,
    confidence: float = 0.9,
) -> VisualTextSnapshot:
    bbox = (40.0, 60.0, 420.0, 110.0)
    observation = VisualTextObservation(
        observation_id=stable_visual_text_observation_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            observation_index=0,
            bbox=bbox,
        ),
        observation_index=0,
        source_sha256=page_input.source_sha256,
        page_number=page_input.page_number,
        bbox=bbox,
        raw_text="Grounded page content",
        confidence=confidence,
        provider_id=provider_id,
        provider_version=provider_version,
        page_image_sha256=page_input.page_image_sha256,
    )
    evaluated = VISUAL_CONTENT_TYPES if full_layout else ("TEXT",)
    return VisualTextSnapshot(
        visual_text_snapshot_id=stable_visual_text_snapshot_id(
            source_sha256=page_input.source_sha256,
            page_image_sha256=page_input.page_image_sha256,
            provider_id=provider_id,
            provider_version=provider_version,
            page_number=page_input.page_number,
            page_bbox=page_input.page_bbox,
            page_image_width_px=page_input.page_image_width_px,
            page_image_height_px=page_input.page_image_height_px,
        ),
        source_sha256=page_input.source_sha256,
        provider_id=provider_id,
        provider_version=provider_version,
        page_number=page_input.page_number,
        page_bbox=page_input.page_bbox,
        page_image_sha256=page_input.page_image_sha256,
        page_image_width_px=page_input.page_image_width_px,
        page_image_height_px=page_input.page_image_height_px,
        evaluated_content_types=evaluated,
        observations=(observation,),
    )


class _VisualProvider:
    def __init__(
        self,
        provider_id: str,
        *,
        full_layout: bool,
        failure: Exception | None = None,
    ) -> None:
        self.provider_id = provider_id
        self.provider_version = "test-v1"
        self.full_layout = full_layout
        self.failure = failure
        self.calls: list[int] = []

    def recognize_page(
        self,
        page_input: VisualTextPageInput,
    ) -> VisualTextSnapshot:
        self.calls.append(page_input.page_number)
        if self.failure is not None:
            raise self.failure
        return _snapshot(
            self.provider_id,
            self.provider_version,
            page_input,
            full_layout=self.full_layout,
        )


class _VisualTableProvider:
    provider_id = "aliyun-bailian-qwen-ocr"
    provider_version = "qwen3.5-ocr:task=table_parsing:fixture-v1"

    def __init__(
        self,
        *,
        failure: Exception | None = None,
        row_count: int = 2,
        column_count: int = 2,
    ) -> None:
        self.calls: list[object] = []
        self.failure = failure
        self.row_count = row_count
        self.column_count = column_count

    def parse_table(self, crop_input: object) -> VisualTableSnapshot:
        self.calls.append(crop_input)
        if self.failure is not None:
            raise self.failure
        snapshot_id = stable_visual_table_snapshot_id(
            source_sha256=crop_input.source_sha256,
            crop_image_sha256=crop_input.crop_image_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=crop_input.page_number,
            table_source_node_id=crop_input.table_source_node_id,
            table_id=crop_input.table_id,
            table_index=crop_input.table_index,
            table_bbox=crop_input.table_bbox,
            crop_image_width_px=crop_input.crop_image_width_px,
            crop_image_height_px=crop_input.crop_image_height_px,
        )
        cells = tuple(
            VisualTableCell(
                visual_table_cell_id=stable_visual_table_cell_id(
                    visual_table_snapshot_id=snapshot_id,
                    row=row,
                    column=column,
                    row_span=1,
                    column_span=1,
                ),
                row=row,
                column=column,
                raw_text=f"visual-{row}-{column}",
            )
            for row in range(self.row_count)
            for column in range(self.column_count)
        )
        return VisualTableSnapshot(
            visual_table_snapshot_id=snapshot_id,
            source_sha256=crop_input.source_sha256,
            provider_id=self.provider_id,
            provider_version=self.provider_version,
            page_number=crop_input.page_number,
            page_bbox=crop_input.page_bbox,
            table_source_node_id=crop_input.table_source_node_id,
            table_id=crop_input.table_id,
            table_index=crop_input.table_index,
            table_bbox=crop_input.table_bbox,
            crop_image_sha256=crop_input.crop_image_sha256,
            crop_image_width_px=crop_input.crop_image_width_px,
            crop_image_height_px=crop_input.crop_image_height_px,
            row_count=self.row_count,
            column_count=self.column_count,
            cells=cells,
        )


class ComposableVisualChainTests(unittest.TestCase):
    def test_bailian_enhancement_uses_frozen_model_and_bounded_runtime(
        self,
    ) -> None:
        telemetry = []
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
            screening_model="qwen3.7-flash",
            chat_model="qwen3.8-max",
            llm_timeout_seconds=37.0,
            openai_max_retries=4,
        )
        with (
            patch.object(
                processing_visual_chain,
                "load_ai_copilot_config",
                return_value=config,
            ),
            patch.object(
                processing_visual_chain,
                "get_runtime_ai_config_value",
                return_value="",
            ),
            patch.object(
                processing_visual_chain,
                "BailianPageLayoutProvider",
            ) as provider_type,
        ):
            processing_visual_chain._build_enhancement_provider(
                "bailian_layout",
                frozen_model="qwen3.7-flash",
                telemetry_callback=telemetry.append,
            )

        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/v1",
            model="qwen3.7-flash",
            timeout_seconds=37.0,
            max_retries=1,
            telemetry_callback=telemetry.append,
        )

        with self.assertRaisesRegex(ValueError, "not frozen"):
            processing_visual_chain._build_enhancement_provider("bailian_layout")

    def test_bailian_ocr_uses_frozen_ocr_setting_without_retries(self) -> None:
        telemetry = []
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
            llm_timeout_seconds=90.0,
        )
        with (
            patch.object(
                processing_visual_chain,
                "load_ai_copilot_config",
                return_value=config,
            ),
            patch.object(
                processing_visual_chain,
                "get_runtime_ai_config_value",
                return_value="qwen3.5-ocr",
            ),
            patch.object(
                processing_visual_chain,
                "BailianVisualTextProvider",
            ) as provider_type,
        ):
            processing_visual_chain._build_enhancement_provider(
                "bailian_ocr",
                frozen_model="qwen3.5-ocr",
                telemetry_callback=telemetry.append,
            )

        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/v1",
            model="qwen3.5-ocr",
            timeout_seconds=45.0,
            max_retries=0,
            telemetry_callback=telemetry.append,
        )

    def test_bailian_ocr_plan_wires_frozen_plan_bound_table_parsing(self) -> None:
        profile = build_local_rules_profile(project_id="project-a", revision=7)
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256="a" * 64,
            profile=profile,
            document=DocumentProcessingProfileV1(
                primary_parser="pymupdf",
                enhancement_chain=("bailian_ocr",),
                auto_escalation=False,
            ),
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
            ),
            connection_revision=22,
        )
        page_provider = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=False,
        )
        table_delegate = SimpleNamespace(
            provider_id="aliyun-bailian-qwen-ocr",
            provider_version=(
                "1.0.0:model=qwen3.5-ocr:task=table_parsing:parser=strict-grid/1"
            ),
        )
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
            llm_timeout_seconds=90.0,
        )

        with (
            patch.object(
                processing_visual_chain,
                "_build_enhancement_provider",
                return_value=page_provider,
            ),
            patch.object(
                processing_visual_chain,
                "load_ai_copilot_config",
                return_value=config,
            ),
            patch.object(
                processing_visual_chain,
                "BailianVisualTableProvider",
                return_value=table_delegate,
                create=True,
            ) as table_provider_type,
        ):
            adapter = build_composable_pdf_source_ir_adapter(plan)

        table_provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/v1",
            model="qwen3.5-ocr",
            timeout_seconds=45.0,
            max_retries=0,
        )
        table_provider = adapter._visual_table_provider
        self.assertIsNotNone(table_provider)
        assert table_provider is not None
        self.assertEqual(
            table_provider.provider_id,
            adapter._visual_text_provider.provider_id,
        )
        self.assertIn(plan.canonical_sha256[:16], table_provider.provider_version)
        self.assertIn("revision=22", table_provider.provider_version)
        self.assertTrue(adapter._visual_table_on_visual_text_pages)
        self.assertIsNone(adapter._visual_table_page_numbers)

    def test_fast_ocr_calls_table_parsing_only_for_routed_native_table_crops(
        self,
    ) -> None:
        page_provider = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=False,
        )
        table_provider = _VisualTableProvider()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "two-table-pages.pdf"
            document = fitz.open()
            for page_number in (1, 2):
                page = document.new_page(width=600, height=800)
                for x in (60, 160, 260):
                    page.draw_line((x, 100), (x, 200), color=(0, 0, 0))
                for y in (100, 150, 200):
                    page.draw_line((60, y), (260, y), color=(0, 0, 0))
                page.insert_text((80, 130), f"A{page_number}")
                page.insert_text((180, 130), f"B{page_number}")
                page.insert_text((80, 180), f"C{page_number}")
                page.insert_text((180, 180), f"D{page_number}")
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            profile = build_local_rules_profile(project_id="project-a", revision=7)
            plan = build_resolved_processing_plan(
                project_id="project-a",
                document_id="document-a",
                source_sha256=report.source_sha256,
                profile=profile,
                document=DocumentProcessingProfileV1(
                    primary_parser="pymupdf",
                    enhancement_chain=("bailian_ocr",),
                    auto_escalation=False,
                ),
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf",
                    enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
                ),
                connection_revision=22,
                force_visual_pages=(1,),
            )
            with (
                patch.object(
                    processing_visual_chain,
                    "_build_enhancement_provider",
                    return_value=page_provider,
                ),
                patch.object(
                    processing_visual_chain,
                    "_build_bailian_visual_table_provider",
                    return_value=table_provider,
                ),
            ):
                adapter = build_composable_pdf_source_ir_adapter(plan)
                snapshot = adapter.extract(
                    source,
                    preflight=report,
                    created_at=datetime.now(UTC),
                )

        self.assertEqual(page_provider.calls, [1])
        self.assertEqual(
            [item.page_number for item in table_provider.calls],
            [1],
        )
        self.assertEqual(snapshot.visual_table_page_numbers, (1,))
        self.assertEqual(len(snapshot.visual_table_snapshots), 1)
        self.assertEqual(
            snapshot.visual_table_snapshots[0].provider_id,
            adapter._visual_text_provider.provider_id,
        )
        self.assertIn(
            plan.canonical_sha256[:16],
            snapshot.visual_table_snapshots[0].provider_version,
        )
        self.assertIn(
            "revision=22",
            snapshot.visual_table_snapshots[0].provider_version,
        )
        self.assertEqual(
            (
                snapshot.visual_table_snapshots[0].row_count,
                snapshot.visual_table_snapshots[0].column_count,
            ),
            (2, 2),
        )
        receipt = page_processing_receipts_from_adapter(adapter)[0]
        self.assertEqual(receipt.visual_table_mode, "NATIVE_TABLE_CROPS")
        self.assertEqual(receipt.visual_table_candidate_count, 1)
        self.assertEqual(receipt.visual_table_attempt_count, 1)
        self.assertEqual(receipt.visual_table_ready_count, 1)
        self.assertEqual(receipt.visual_table_failed_count, 0)
        self.assertEqual(receipt.visual_table_budget_skipped_count, 0)
        self.assertEqual(
            receipt.as_dict()["table_parsing"]["request_ceiling_per_page"],
            8,
        )
        self.assertEqual(
            receipt.as_dict()["table_parsing"]["request_ceiling_per_document"],
            64,
        )

    def test_text_only_ocr_figure_page_is_degraded_in_formal_receipt(self) -> None:
        page_provider = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=False,
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "figure-only-scan.pdf"
            _write_full_page_raster_pdf(source)
            adapter, snapshot = _extract_with_test_ocr_route(
                source,
                page_provider=page_provider,
                table_provider=_VisualTableProvider(),
            )
            report = LocalDocumentPreflight().inspect(source)
            plan = _bailian_ocr_plan(source_sha256=report.source_sha256)
            with (
                patch.object(
                    processing_visual_chain,
                    "_build_enhancement_provider",
                    return_value=_VisualProvider(
                        "aliyun-bailian-qwen-ocr",
                        full_layout=False,
                    ),
                ),
                patch.object(
                    processing_visual_chain,
                    "_build_bailian_visual_table_provider",
                    return_value=_VisualTableProvider(),
                ),
            ):
                result = build_local_structure_document_result(
                    source,
                    plan=plan,
                    result_runtime_sha256="f" * 64,
                )

        self.assertIn(
            "pdf_visual_figure_content_not_evaluated",
            {item.code for item in snapshot.diagnostics},
        )
        receipt = page_processing_receipts_from_adapter(adapter)[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(
            receipt.degraded_reason_codes,
            ("pdf_visual_figure_content_not_evaluated",),
        )
        self.assertEqual(result["degraded_page_numbers"], [1])

    def test_visual_table_provider_failure_degrades_page_with_safe_code(
        self,
    ) -> None:
        failure = VisualTableProviderRejected(
            VisualTableProviderRejectionCode.PROVIDER_FAILED,
            "sensitive upstream response must not enter the receipt",
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "failed-table.pdf"
            _write_two_by_two_table_pdf(source)
            adapter, _ = _extract_with_test_ocr_route(
                source,
                page_provider=_VisualProvider(
                    "aliyun-bailian-qwen-ocr",
                    full_layout=False,
                ),
                table_provider=_VisualTableProvider(failure=failure),
            )

        receipt = page_processing_receipts_from_adapter(adapter)[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(receipt.visual_table_failed_count, 1)
        self.assertEqual(
            receipt.degraded_reason_codes,
            ("pdf_visual_table_provider_failed",),
        )
        self.assertNotIn("sensitive", " ".join(receipt.degraded_reason_codes))

    def test_visual_table_topology_mismatch_degrades_page(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "mismatched-table.pdf"
            _write_two_by_two_table_pdf(source)
            adapter, _ = _extract_with_test_ocr_route(
                source,
                page_provider=_VisualProvider(
                    "aliyun-bailian-qwen-ocr",
                    full_layout=False,
                ),
                table_provider=_VisualTableProvider(
                    row_count=1,
                    column_count=1,
                ),
            )

        receipt = page_processing_receipts_from_adapter(adapter)[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(receipt.visual_table_failed_count, 1)
        self.assertEqual(
            receipt.degraded_reason_codes,
            ("pdf_visual_table_topology_mismatch",),
        )

    def test_fast_ocr_table_request_budget_fails_closed_before_provider(self) -> None:
        page_provider = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=False,
        )
        table_provider = _VisualTableProvider()
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "budgeted-table.pdf"
            document = fitz.open()
            page = document.new_page(width=600, height=800)
            for x in (60, 160, 260):
                page.draw_line((x, 100), (x, 200), color=(0, 0, 0))
            for y in (100, 150, 200):
                page.draw_line((60, y), (260, y), color=(0, 0, 0))
            for position, text_value in (
                ((80, 130), "A"),
                ((180, 130), "B"),
                ((80, 180), "C"),
                ((180, 180), "D"),
            ):
                page.insert_text(position, text_value)
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            profile = build_local_rules_profile(project_id="project-a", revision=7)
            plan = build_resolved_processing_plan(
                project_id="project-a",
                document_id="document-a",
                source_sha256=report.source_sha256,
                profile=profile,
                document=DocumentProcessingProfileV1(
                    primary_parser="pymupdf",
                    enhancement_chain=("bailian_ocr",),
                    auto_escalation=False,
                ),
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf",
                    enhancement_providers=("aliyun:bailian-ocr:qwen3.5-ocr",),
                ),
                connection_revision=22,
                force_visual_pages=(1,),
            )
            with (
                patch.object(
                    processing_visual_chain,
                    "_build_enhancement_provider",
                    return_value=page_provider,
                ),
                patch.object(
                    processing_visual_chain,
                    "_build_bailian_visual_table_provider",
                    return_value=table_provider,
                ),
                patch.object(
                    pdf_adapter_module,
                    "VISUAL_TABLE_MAX_REQUESTS_PER_PAGE",
                    0,
                ),
            ):
                adapter = build_composable_pdf_source_ir_adapter(plan)
                snapshot = adapter.extract(
                    source,
                    preflight=report,
                    created_at=datetime.now(UTC),
                )

        self.assertEqual(table_provider.calls, [])
        self.assertEqual(snapshot.visual_table_page_numbers, (1,))
        self.assertEqual(snapshot.visual_table_snapshots, ())
        self.assertIn(
            "pdf_visual_table_request_budget_exceeded",
            {item.code for item in snapshot.diagnostics},
        )
        receipt = page_processing_receipts_from_adapter(adapter)[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(receipt.visual_table_candidate_count, 1)
        self.assertEqual(receipt.visual_table_attempt_count, 0)
        self.assertEqual(receipt.visual_table_budget_skipped_count, 1)
        self.assertEqual(
            receipt.degraded_reason_codes,
            ("pdf_visual_table_request_budget_exceeded",),
        )

    def test_builder_keeps_frozen_model_after_runtime_settings_change(self) -> None:
        profile = build_local_rules_profile(project_id="project-a", revision=7)
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256="a" * 64,
            profile=profile,
            document=DocumentProcessingProfileV1(
                primary_parser="pymupdf",
                enhancement_chain=("bailian_layout",),
                auto_escalation=False,
            ),
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-layout:qwen3.7-flash",),
            ),
            connection_revision=22,
        )
        changed_config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
            screening_model="qwen-future-layout",
            llm_timeout_seconds=45.0,
            openai_max_retries=0,
        )
        with (
            patch.object(
                processing_visual_chain,
                "load_ai_copilot_config",
                return_value=changed_config,
            ),
            patch.object(
                processing_visual_chain,
                "BailianPageLayoutProvider",
            ) as provider_type,
        ):
            provider_type.return_value.provider_id = "bailian-page-layout"
            provider_type.return_value.provider_version = "frozen-provider/v1"
            build_composable_pdf_source_ir_adapter(plan)

        self.assertEqual(
            provider_type.call_args.kwargs["model"],
            "qwen3.7-flash",
        )

    def test_builder_rejects_missing_or_wrong_frozen_enhancement_binding(
        self,
    ) -> None:
        profile = build_local_rules_profile(project_id="project-a", revision=7)
        document = DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            enhancement_chain=("bailian_layout",),
            auto_escalation=False,
        )
        for bindings in (
            (),
            ("aliyun:bailian-ocr:qwen3.5-ocr",),
            ("aliyun:bailian-layout:",),
        ):
            with self.subTest(bindings=bindings):
                plan = build_resolved_processing_plan(
                    project_id="project-a",
                    document_id="document-a",
                    source_sha256="a" * 64,
                    profile=profile,
                    document=document,
                    llm_provider="none",
                    provider_bindings=ResolvedProviderBindingsV1(
                        primary_parser_provider="local:pymupdf",
                        enhancement_providers=bindings,
                    ),
                    connection_revision=22,
                )
                with self.assertRaises(ValueError):
                    build_composable_pdf_source_ir_adapter(plan)

    def test_builder_caches_each_real_provider_and_preserves_receipt_identity(
        self,
    ) -> None:
        page_input = _page_input()
        profile = build_local_rules_profile(project_id="project-a", revision=7)
        plan = build_resolved_processing_plan(
            project_id="project-a",
            document_id="document-a",
            source_sha256=page_input.source_sha256,
            profile=profile,
            document=DocumentProcessingProfileV1(
                primary_parser="pymupdf",
                enhancement_chain=("bailian_layout",),
                auto_escalation=False,
            ),
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                enhancement_providers=("aliyun:bailian-layout:qwen3.7-flash",),
            ),
            connection_revision=22,
            force_visual_pages=(1,),
        )
        telemetry: list[VisualTextProviderTelemetryV1] = []
        with tempfile.TemporaryDirectory() as temporary:
            binding = VisualTextSnapshotCacheBinding(
                cache=VisualTextSnapshotCache(Path(temporary) / "storage"),
                project_id="project-a",
                document_id="document-a",
            )
            first_delegate = _VisualProvider("bailian-layout", full_layout=True)
            with patch.object(
                processing_visual_chain,
                "_build_enhancement_provider",
                return_value=first_delegate,
            ):
                first_adapter = build_composable_pdf_source_ir_adapter(
                    plan,
                    visual_text_snapshot_cache=binding,
                    provider_telemetry_callback=telemetry.append,
                )
            first_provider = first_adapter._visual_text_provider
            assert first_provider is not None
            first_provider.recognize_page(page_input)

            second_delegate = _VisualProvider("bailian-layout", full_layout=True)
            with patch.object(
                processing_visual_chain,
                "_build_enhancement_provider",
                return_value=second_delegate,
            ):
                second_adapter = build_composable_pdf_source_ir_adapter(
                    plan,
                    visual_text_snapshot_cache=binding,
                    provider_telemetry_callback=telemetry.append,
                )
            second_provider = second_adapter._visual_text_provider
            assert second_provider is not None
            second_provider.recognize_page(page_input)

        self.assertEqual(first_delegate.calls, [1])
        self.assertEqual(second_delegate.calls, [])
        receipt = page_processing_receipts_from_adapter(second_adapter)[0]
        self.assertEqual(receipt.attempted_provider_ids, ("bailian-layout",))
        self.assertEqual(receipt.adopted_provider_id, "bailian-layout")
        self.assertEqual(len(telemetry), 1)
        self.assertTrue(telemetry[0].cache_hit)
        self.assertEqual(telemetry[0].request_count, 0)

    def test_bailian_layout_plan_preserves_real_per_page_source_progress(
        self,
    ) -> None:
        provider = _VisualProvider("bailian-layout", full_layout=True)
        progress: list[tuple[int, int, int]] = []
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "bailian-progress.pdf"
            document = fitz.open()
            for page_number in range(1, 3):
                page = document.new_page()
                page.insert_text((72, 72), f"Requirement page {page_number}")
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            profile = build_local_rules_profile(project_id="project-a", revision=7)
            plan = build_resolved_processing_plan(
                project_id="project-a",
                document_id="document-a",
                source_sha256=report.source_sha256,
                profile=profile,
                document=DocumentProcessingProfileV1(
                    primary_parser="pymupdf",
                    enhancement_chain=("bailian_layout",),
                    auto_escalation=False,
                ),
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf",
                    enhancement_providers=("aliyun:bailian-layout:qwen3.7-flash",),
                ),
                connection_revision=22,
                force_visual_pages=(1, 2),
            )
            with patch.object(
                processing_visual_chain,
                "_build_enhancement_provider",
                return_value=provider,
            ):
                adapter = build_composable_pdf_source_ir_adapter(
                    plan,
                    page_progress_callback=lambda completed, total, current: (
                        progress.append((completed, total, current))
                    ),
                )
                adapter.extract(
                    source,
                    preflight=report,
                    created_at=datetime.now(UTC),
                )

        self.assertEqual(provider.calls, [1, 2])
        self.assertEqual(
            progress,
            [(0, 2, 1), (1, 2, 2), (2, 2, 2)],
        )

    def test_normal_text_page_does_not_call_any_remote_provider(self) -> None:
        enhancement = _VisualProvider("azure-di", full_layout=True)
        complex_provider = _VisualProvider("azure-sol", full_layout=True)
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset(),
            auto_escalation=True,
            plan_sha256="a" * 64,
        )
        adapter = PdfSourceIRAdapter(visual_text_provider=chain)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "normal.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Normal selectable PDF text")
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            adapter.extract(
                source,
                preflight=report,
                created_at=datetime.now(UTC),
            )

        self.assertEqual(enhancement.calls, [])
        self.assertEqual(complex_provider.calls, [])
        self.assertEqual(chain.page_receipts, ())

    def test_di_adequate_layout_stops_before_sol(self) -> None:
        page_input = _page_input()
        enhancement = _VisualProvider("azure-di", full_layout=True)
        complex_provider = _VisualProvider("azure-sol", full_layout=True)
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset(),
            auto_escalation=True,
            plan_sha256="b" * 64,
        )

        chain.recognize_page(page_input)

        self.assertEqual(enhancement.calls, [1])
        self.assertEqual(complex_provider.calls, [])
        receipt = chain.page_receipts[0]
        self.assertEqual(receipt.state, "READY")
        self.assertEqual(receipt.adopted_provider_id, "azure-di")

    def test_incomplete_di_layout_escalates_to_sol(self) -> None:
        page_input = _page_input()
        enhancement = _VisualProvider("azure-di", full_layout=False)
        complex_provider = _VisualProvider("azure-sol", full_layout=True)
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset(),
            auto_escalation=True,
            plan_sha256="c" * 64,
        )

        snapshot = chain.recognize_page(page_input)

        self.assertEqual(enhancement.calls, [1])
        self.assertEqual(complex_provider.calls, [1])
        self.assertEqual(snapshot.provider_id, chain.provider_id)
        self.assertEqual(
            chain.page_receipts[0].adopted_provider_id,
            "azure-sol",
        )

    def test_forced_page_calls_only_sol_even_when_auto_escalation_is_off(
        self,
    ) -> None:
        page_input = _page_input(page_number=8)
        enhancement = _VisualProvider("azure-di", full_layout=True)
        complex_provider = _VisualProvider("azure-sol", full_layout=True)
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset({8}),
            auto_escalation=False,
            plan_sha256="d" * 64,
        )

        chain.recognize_page(page_input)

        self.assertEqual(enhancement.calls, [])
        self.assertEqual(complex_provider.calls, [8])
        self.assertEqual(
            chain.page_receipts[0].attempted_provider_ids,
            ("azure-sol",),
        )

    def test_later_provider_failure_retains_previous_stage_and_marks_degraded(
        self,
    ) -> None:
        page_input = _page_input()
        enhancement = _VisualProvider("azure-di", full_layout=False)
        complex_provider = _VisualProvider(
            "azure-sol",
            full_layout=True,
            failure=RuntimeError("service unavailable"),
        )
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset(),
            auto_escalation=True,
            plan_sha256="e" * 64,
        )

        snapshot = chain.recognize_page(page_input)

        self.assertEqual(snapshot.provider_id, chain.provider_id)
        receipt = chain.page_receipts[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(receipt.adopted_provider_id, "azure-di")
        self.assertEqual(
            receipt.degraded_reason_codes,
            ("azure-sol:provider_failed",),
        )

    def test_all_provider_failures_record_upstream_fallback_before_raising(
        self,
    ) -> None:
        page_input = _page_input()
        enhancement = _VisualProvider(
            "azure-di",
            full_layout=False,
            failure=RuntimeError("DI unavailable"),
        )
        complex_provider = _VisualProvider(
            "azure-sol",
            full_layout=True,
            failure=RuntimeError("Sol unavailable"),
        )
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=complex_provider,
            force_visual_pages=frozenset(),
            auto_escalation=True,
            plan_sha256="f" * 64,
        )

        with self.assertRaisesRegex(RuntimeError, "Sol unavailable"):
            chain.recognize_page(page_input)

        receipt = chain.page_receipts[0]
        self.assertEqual(receipt.state, "DEGRADED")
        self.assertEqual(receipt.adopted_provider_id, "upstream:primary-parser")
        self.assertEqual(
            receipt.attempted_provider_ids,
            ("azure-di", "azure-sol"),
        )

    def test_provider_unavailable_is_attempted_once_for_the_whole_run(
        self,
    ) -> None:
        unavailable = VisualTextProviderRejected(
            VisualTextProviderRejectionCode.UNAVAILABLE,
            "provider authorization failed",
        )
        enhancement = _VisualProvider(
            "bailian-layout",
            full_layout=True,
            failure=unavailable,
        )
        chain = ComposablePageVisualProvider(
            enhancement_providers=(enhancement,),
            complex_provider=None,
            force_visual_pages=frozenset(),
            auto_escalation=False,
            plan_sha256="0" * 64,
        )
        self.assertFalse(chain.supports_concurrent_pages)
        self.assertEqual(chain.max_concurrent_pages, 1)

        with self.assertRaises(VisualTextProviderRejected):
            chain.recognize_page(_page_input(page_number=1))
        with self.assertRaises(VisualTextProviderRejected):
            chain.recognize_page(_page_input(page_number=2))

        self.assertEqual(enhancement.calls, [1])
        self.assertEqual(
            chain.page_receipts[1].degraded_reason_codes,
            ("bailian-layout:unavailable",),
        )

    def test_terminal_failure_is_scoped_to_provider_version_not_shared_id(
        self,
    ) -> None:
        unavailable = VisualTextProviderRejected(
            VisualTextProviderRejectionCode.UNAVAILABLE,
            "provider authorization failed",
        )
        first = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=False,
            failure=unavailable,
        )
        first.provider_version = "ocr-v1"
        second = _VisualProvider(
            "aliyun-bailian-qwen-ocr",
            full_layout=True,
        )
        second.provider_version = "layout-v1"
        chain = ComposablePageVisualProvider(
            enhancement_providers=(first, second),
            complex_provider=None,
            force_visual_pages=frozenset(),
            auto_escalation=False,
            plan_sha256="2" * 64,
        )

        snapshot = chain.recognize_page(_page_input())

        self.assertEqual(snapshot.provider_id, chain.provider_id)
        self.assertEqual(first.calls, [1])
        self.assertEqual(second.calls, [1])
        self.assertEqual(
            chain.page_receipts[0].adopted_provider_id,
            "aliyun-bailian-qwen-ocr",
        )

    def test_pdf_adapter_continues_with_native_upstream_when_forced_sol_fails(
        self,
    ) -> None:
        complex_provider = _VisualProvider(
            "azure-sol",
            full_layout=True,
            failure=RuntimeError("Sol unavailable"),
        )
        chain = ComposablePageVisualProvider(
            enhancement_providers=(),
            complex_provider=complex_provider,
            force_visual_pages=frozenset({1}),
            auto_escalation=True,
            plan_sha256="1" * 64,
        )
        adapter = PdfSourceIRAdapter(
            visual_text_provider=chain,
            force_visual_text_page_numbers=frozenset({1}),
        )
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "forced.pdf"
            document = fitz.open()
            page = document.new_page()
            page.insert_text((72, 72), "Trusted native upstream text")
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            source_ir = adapter.extract(
                source,
                preflight=report,
                created_at=datetime.now(UTC),
            )

        self.assertEqual(source_ir.status, SourceIRStatus.PARTIAL)
        self.assertTrue(
            any(
                item.code == "pdf_visual_text_provider_failed"
                for item in source_ir.diagnostics
            )
        )
        self.assertEqual(
            chain.page_receipts[0].adopted_provider_id,
            "upstream:primary-parser",
        )


if __name__ == "__main__":
    unittest.main()
