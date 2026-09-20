from __future__ import annotations

# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.domain.extraction.source_ir import SourceNodeKind
from app.domain.extraction.semantic_provider import SemanticProviderRunStatus
from app.domain.extraction.structure import (
    StructureSourceRef,
    StructureSourceRefKind,
)
from scripts import llm_extraction_shadow


class LlmExtractionShadowCliTests(unittest.TestCase):
    def test_defaults_lock_qwen_semantics_and_qwen_ocr_separately(self) -> None:
        args = llm_extraction_shadow._parser().parse_args(
            ["--source", "source.pdf", "--out", "report.json"]
        )

        self.assertIsNone(args.model)
        self.assertIsNone(args.ocr_model)
        self.assertFalse(args.evaluation_model)
        self.assertFalse(args.disable_ocr)
        self.assertEqual(args.ocr_page, [])
        self.assertIsNone(args.ocr_timeout_seconds)
        self.assertIsNone(args.ocr_max_retries)
        self.assertIsNone(args.semantic_timeout_seconds)
        self.assertIsNone(args.semantic_max_retries)
        self.assertEqual(args.semantic_seed, 1234)
        self.assertEqual(args.repeatability_runs, 1)
        self.assertEqual(args.max_semantic_page_images, 12)
        self.assertEqual(args.max_semantic_images_per_request, 2)
        self.assertIsNone(args.scope_source_sha256)
        self.assertEqual(args.section_path, [])
        self.assertEqual(
            llm_extraction_shadow._ocr_runtime_from_args(args),
            (45.0, 0),
        )
        self.assertEqual(
            llm_extraction_shadow._semantic_runtime_from_args(args),
            (90.0, 1),
        )
        self.assertEqual(llm_extraction_shadow._semantic_seed_from_args(args), 1234)
        self.assertEqual(llm_extraction_shadow._repeatability_runs_from_args(args), 1)

    def test_scope_arguments_create_a_source_bound_review_intent(self) -> None:
        args = llm_extraction_shadow._parser().parse_args(
            [
                "--source",
                "source.pdf",
                "--out",
                "report.json",
                "--scope-source-sha256",
                "a" * 64,
                "--section-path",
                "4/4.12/4.12.1",
                "--section-path",
                "4/4.13/4.13.3",
            ]
        )

        locator = llm_extraction_shadow._scope_locator_from_args(args)

        self.assertIsNotNone(locator)
        assert locator is not None
        self.assertEqual(locator.source_sha256, "a" * 64)
        self.assertEqual(
            tuple(item.normalized_section_path for item in locator.section_selectors),
            (("4", "4.12", "4.12.1"), ("4", "4.13", "4.13.3")),
        )

    def test_incomplete_or_partial_scope_arguments_fail_closed(self) -> None:
        for extra_args in (
            ["--section-path", "4/4.12/4.12.1"],
            ["--scope-source-sha256", "a" * 64],
            [
                "--scope-source-sha256",
                "a" * 64,
                "--section-path",
                "4/4.12.1",
            ],
            [
                "--scope-source-sha256",
                "a" * 64,
                "--section-path",
                "4//4.12/4.12.1",
            ],
        ):
            with self.subTest(extra_args=extra_args):
                args = llm_extraction_shadow._parser().parse_args(
                    ["--source", "source.pdf", "--out", "report.json", *extra_args]
                )
                with self.assertRaisesRegex(RuntimeError, "llm_scope_locator"):
                    llm_extraction_shadow._scope_locator_from_args(args)

    def test_scoped_ocr_requires_an_explicit_capture_page_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"scope-bound-pdf")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            with mock.patch.object(
                llm_extraction_shadow,
                "_load_bailian_settings",
            ) as load_settings:
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        source_sha256,
                        "--section-path",
                        "4/4.12/4.12.1",
                    ]
                )

        self.assertEqual(code, 2)
        load_settings.assert_not_called()
        self.assertFalse(output.exists())

    def test_preflight_hash_mismatch_stops_before_configuration_and_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"changed-source")
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_preflight_source",
                    return_value=SimpleNamespace(
                        identity=SimpleNamespace(source_sha256="b" * 64)
                    ),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                ) as load_settings,
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianVisualTextProvider",
                ) as ocr_provider,
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        "a" * 64,
                        "--section-path",
                        "4/4.12/4.12.1",
                        "--disable-ocr",
                    ]
                )

        self.assertEqual(code, 2)
        load_settings.assert_not_called()
        ocr_provider.assert_not_called()
        self.assertFalse(output.exists())

    def test_ocr_scope_and_runtime_limits_are_passed_to_the_pdf_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"scope-bound-pdf")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            visual_provider = SimpleNamespace(
                provider_id="aliyun-bailian-qwen-ocr",
                provider_version="qwen3.5-ocr:test",
            )
            adapter = object()
            preflight = SimpleNamespace(
                identity=SimpleNamespace(source_sha256=source_sha256)
            )
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                    return_value={
                        "api_key": "synthetic-key",
                        "base_url": (
                            "https://dashscope.aliyuncs.com/compatible-mode/v1"
                        ),
                    },
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianVisualTextProvider",
                    return_value=visual_provider,
                ) as provider_factory,
                mock.patch.object(
                    llm_extraction_shadow,
                    "_preflight_source",
                    return_value=preflight,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_validate_ocr_page_range",
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "PdfSourceIRAdapter",
                    return_value=adapter,
                ) as adapter_factory,
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_source_ir",
                    side_effect=RuntimeError("stop_after_adapter"),
                ) as build,
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        source_sha256,
                        "--section-path",
                        "4/4.12/4.12.1",
                        "--ocr-page",
                        "29",
                        "--ocr-page",
                        "30",
                        "--ocr-timeout-seconds",
                        "7.5",
                        "--ocr-max-retries",
                        "1",
                    ]
                )

        self.assertEqual(code, 2)
        provider_factory.assert_called_once_with(
            api_key="synthetic-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.5-ocr",
            timeout_seconds=7.5,
            max_retries=1,
        )
        adapter_factory.assert_called_once_with(
            visual_text_provider=visual_provider,
            visual_text_page_numbers=frozenset({29, 30}),
        )
        build.assert_called_once_with(
            source.resolve(),
            preflight_report=preflight,
            adapters=(adapter,),
        )
        self.assertFalse(output.exists())

    def test_invalid_or_conflicting_ocr_limits_fail_closed(self) -> None:
        parser = llm_extraction_shadow._parser()
        for extra_args, error_code in (
            (("--ocr-page", "0"), "invalid_ocr_page_scope"),
            (
                ("--ocr-page", "1", "--ocr-page", "1"),
                "duplicate_ocr_page",
            ),
            (
                ("--disable-ocr", "--ocr-page", "1"),
                "conflicting_ocr_scope_options",
            ),
            (
                ("--disable-ocr", "--ocr-model", "qwen3.5-ocr"),
                "conflicting_ocr_scope_options",
            ),
            (
                ("--disable-ocr", "--ocr-timeout-seconds", "5"),
                "conflicting_ocr_scope_options",
            ),
            (
                ("--disable-ocr", "--ocr-max-retries", "0"),
                "conflicting_ocr_scope_options",
            ),
        ):
            with self.subTest(extra_args=extra_args):
                args = parser.parse_args(
                    ["--source", "source.pdf", "--out", "report.json", *extra_args]
                )
                with self.assertRaisesRegex(RuntimeError, error_code):
                    llm_extraction_shadow._ocr_page_numbers_from_args(
                        args,
                        scoped=False,
                    )
        for extra_args in (
            ("--ocr-timeout-seconds", "nan"),
            ("--ocr-timeout-seconds", "121"),
            ("--ocr-max-retries", "3"),
        ):
            with self.subTest(extra_args=extra_args):
                args = parser.parse_args(
                    ["--source", "source.pdf", "--out", "report.json", *extra_args]
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "invalid_ocr_runtime_limits",
                ):
                    llm_extraction_shadow._ocr_runtime_from_args(args)
        for extra_args in (
            ("--semantic-timeout-seconds", "nan"),
            ("--semantic-timeout-seconds", "181"),
            ("--semantic-max-retries", "3"),
        ):
            with self.subTest(extra_args=extra_args):
                args = parser.parse_args(
                    ["--source", "source.pdf", "--out", "report.json", *extra_args]
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "invalid_semantic_runtime_limits",
                ):
                    llm_extraction_shadow._semantic_runtime_from_args(args)
        for value in (-1, 2**31):
            with (
                self.subTest(seed=value),
                self.assertRaisesRegex(
                    RuntimeError,
                    "invalid_semantic_seed",
                ),
            ):
                llm_extraction_shadow._semantic_seed_from_args(
                    SimpleNamespace(semantic_seed=value)
                )
        for value in (0, 2, 11, True):
            with (
                self.subTest(runs=value),
                self.assertRaisesRegex(
                    RuntimeError,
                    "repeatability_run_count_invalid",
                ),
            ):
                llm_extraction_shadow._repeatability_runs_from_args(
                    SimpleNamespace(repeatability_runs=value)
                )

    def test_repeatability_gate_requires_an_exact_scope_before_configuration(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"not opened")
            with mock.patch.object(
                llm_extraction_shadow,
                "_load_bailian_settings",
            ) as load_settings:
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--disable-ocr",
                        "--repeatability-runs",
                        "3",
                    ]
                )

        self.assertEqual(code, 2)
        load_settings.assert_not_called()
        self.assertFalse(output.exists())

    def test_ocr_attempt_budget_is_bounded_before_provider_construction(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ocr_attempt_budget_exceeded"):
            llm_extraction_shadow._validate_ocr_attempt_budget(
                frozenset({1, 2, 3}),
                timeout_seconds=60.0,
                max_retries=1,
            )

    @unittest.skipIf(llm_extraction_shadow.pymupdf is None, "PyMuPDF unavailable")
    def test_ocr_page_allowlist_is_checked_against_the_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            document = llm_extraction_shadow.pymupdf.open()
            document.new_page()
            document.save(source)
            document.close()

            with self.assertRaisesRegex(RuntimeError, "ocr_page_out_of_range"):
                llm_extraction_shadow._validate_ocr_page_range(
                    source,
                    frozenset({2}),
                )

    def test_missing_bailian_configuration_fails_before_pdf_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"not opened")
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                    return_value={"api_key": "", "base_url": ""},
                ),
                mock.patch.object(llm_extraction_shadow, "build_source_ir") as build,
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--disable-ocr",
                    ]
                )

        self.assertEqual(code, 2)
        build.assert_not_called()
        self.assertFalse(output.exists())

    def test_semantic_images_are_selected_only_for_visual_and_table_pages(self) -> None:
        source = SimpleNamespace(
            visual_text_snapshots=(SimpleNamespace(page_number=7),),
            nodes=(
                SimpleNamespace(
                    kind=SourceNodeKind.TABLE,
                    native_locator=SimpleNamespace(page_number=3),
                ),
                SimpleNamespace(
                    kind=SourceNodeKind.PARAGRAPH,
                    native_locator=SimpleNamespace(page_number=1),
                ),
                SimpleNamespace(
                    kind=SourceNodeKind.FIGURE,
                    native_locator=SimpleNamespace(page_number=9),
                ),
            ),
        )

        self.assertEqual(
            llm_extraction_shadow._semantic_page_numbers(source),
            (3, 7, 9),
        )

    def test_scoped_semantic_images_ignore_unselected_document_pages(self) -> None:
        selected_table_ref = StructureSourceRef(
            kind=StructureSourceRefKind.SOURCE_NODE,
            source_id="node-" + "1" * 32,
        )
        selected_visual_ref = StructureSourceRef(
            kind=StructureSourceRefKind.VISUAL_TEXT_OBSERVATION,
            source_id="vto-" + "2" * 32,
        )
        source = SimpleNamespace(
            visual_text_snapshots=(
                SimpleNamespace(
                    page_number=7,
                    observations=(
                        SimpleNamespace(
                            observation_id=selected_visual_ref.source_id,
                            page_number=7,
                        ),
                    ),
                ),
                SimpleNamespace(
                    page_number=8,
                    observations=(
                        SimpleNamespace(
                            observation_id="vto-" + "3" * 32,
                            page_number=8,
                        ),
                    ),
                ),
            ),
            nodes=(
                SimpleNamespace(
                    source_node_id=selected_table_ref.source_id,
                    kind=SourceNodeKind.TABLE,
                    native_locator=SimpleNamespace(page_number=3),
                    table_payload=None,
                ),
                SimpleNamespace(
                    source_node_id="node-" + "4" * 32,
                    kind=SourceNodeKind.FIGURE,
                    native_locator=SimpleNamespace(page_number=9),
                    table_payload=None,
                ),
            ),
        )
        tree = SimpleNamespace(
            nodes=(
                SimpleNamespace(
                    structure_node_id="selected",
                    source_refs=(selected_table_ref, selected_visual_ref),
                ),
                SimpleNamespace(
                    structure_node_id="other",
                    source_refs=(),
                ),
            )
        )

        self.assertEqual(
            llm_extraction_shadow._semantic_page_numbers(
                source,
                tree=tree,
                scope_structure_node_ids=("selected",),
            ),
            (3, 7),
        )
        self.assertEqual(
            llm_extraction_shadow._scope_source_page_numbers(
                source,
                tree=tree,
                scope_structure_node_ids=("selected",),
            ),
            (3, 7),
        )

    def test_scoped_visual_gap_stops_before_semantic_provider_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"scope-bound-pdf")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            preflight = SimpleNamespace(
                identity=SimpleNamespace(source_sha256=source_sha256)
            )
            source_ir = SimpleNamespace(diagnostics=())
            tree = SimpleNamespace()
            located = SimpleNamespace(scope_structure_node_ids=("scope-a",))
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_preflight_source",
                    return_value=preflight,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                    return_value={
                        "api_key": "synthetic-key",
                        "base_url": (
                            "https://dashscope.aliyuncs.com/compatible-mode/v1"
                        ),
                    },
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_source_ir",
                    return_value=source_ir,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_structure_tree",
                    return_value=tree,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "locate_llm_extraction_scope",
                    return_value=located,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_scope_source_page_numbers",
                    return_value=(7,),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_raise_for_scoped_visual_incompleteness",
                    side_effect=RuntimeError("scoped_visual_text_incomplete"),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "classify_content",
                ) as baseline_classify,
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianQwenSemanticProvider",
                ) as semantic_provider,
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        source_sha256,
                        "--section-path",
                        "4/4.12/4.12.1",
                        "--disable-ocr",
                    ]
                )

        self.assertEqual(code, 2)
        baseline_classify.assert_not_called()
        semantic_provider.assert_not_called()
        self.assertFalse(output.exists())

    def test_scoped_visual_gap_is_bound_to_selected_source_pages(self) -> None:
        source = SimpleNamespace(
            diagnostics=(
                SimpleNamespace(
                    code="pdf_visual_text_page_out_of_scope",
                    native_locator=SimpleNamespace(page_number=7),
                ),
            )
        )
        llm_extraction_shadow._raise_for_scoped_visual_incompleteness(source, (8,))
        with self.assertRaisesRegex(RuntimeError, "scoped_visual_text_incomplete"):
            llm_extraction_shadow._raise_for_scoped_visual_incompleteness(
                source,
                (7,),
            )

    def test_scope_failure_stops_before_page_render_and_semantic_provider_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"scope-bound-pdf")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            preflight = SimpleNamespace(
                identity=SimpleNamespace(source_sha256=source_sha256)
            )
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_preflight_source",
                    return_value=preflight,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                    return_value={
                        "api_key": "synthetic-key",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    },
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianVisualTextProvider",
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_source_ir",
                    return_value=SimpleNamespace(diagnostics=()),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_structure_tree",
                    return_value=SimpleNamespace(),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "locate_llm_extraction_scope",
                    side_effect=RuntimeError("llm_scope_section_not_found"),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_render_page_images",
                ) as render,
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianQwenSemanticProvider",
                ) as semantic_provider,
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        source_sha256,
                        "--section-path",
                        "4/4.12/4.12.1",
                        "--disable-ocr",
                    ]
                )

        self.assertEqual(code, 2)
        render.assert_not_called()
        semantic_provider.assert_not_called()
        self.assertFalse(output.exists())

    def test_successful_scope_passes_only_resolved_ids_to_llm_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            output = Path(directory) / "report.json"
            source.write_bytes(b"scope-bound-pdf")
            source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            preflight = SimpleNamespace(
                identity=SimpleNamespace(source_sha256=source_sha256)
            )
            source_ir = SimpleNamespace(diagnostics=())
            tree = SimpleNamespace()
            located = SimpleNamespace(scope_structure_node_ids=("scope-a", "scope-b"))
            semantics = SimpleNamespace(
                annotations=(),
                scope_structure_node_ids=("scope-a", "scope-b"),
            )
            result = SimpleNamespace(
                semantics=semantics,
                assembly=SimpleNamespace(items=()),
                requirement_view=SimpleNamespace(nodes=()),
                provider_runs=(
                    SimpleNamespace(status=SemanticProviderRunStatus.SUCCESS),
                ),
            )
            provider = SimpleNamespace(model_id="qwen3.7-plus-2026-05-26")
            with (
                mock.patch.object(
                    llm_extraction_shadow,
                    "_preflight_source",
                    return_value=preflight,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_load_bailian_settings",
                    return_value={
                        "api_key": "synthetic-key",
                        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    },
                ),
                mock.patch.object(llm_extraction_shadow, "BailianVisualTextProvider"),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_source_ir",
                    return_value=source_ir,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "build_structure_tree",
                    return_value=tree,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "locate_llm_extraction_scope",
                    return_value=located,
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "classify_content",
                    return_value=SimpleNamespace(),
                ) as baseline_classify,
                mock.patch.object(
                    llm_extraction_shadow,
                    "project_requirements",
                    return_value=SimpleNamespace(),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_semantic_page_numbers",
                    return_value=(),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "_render_page_images",
                    return_value=(),
                ),
                mock.patch.object(
                    llm_extraction_shadow,
                    "BailianQwenSemanticProvider",
                    return_value=provider,
                ) as semantic_provider_factory,
                mock.patch.object(
                    llm_extraction_shadow,
                    "extract_requirements_with_provider",
                    return_value=result,
                ) as extract,
                mock.patch.object(
                    llm_extraction_shadow,
                    "_safe_report",
                    return_value={},
                ),
                mock.patch.object(llm_extraction_shadow, "_write_report_atomic"),
            ):
                code = llm_extraction_shadow.main(
                    [
                        "--source",
                        str(source),
                        "--out",
                        str(output),
                        "--scope-source-sha256",
                        source_sha256,
                        "--section-path",
                        "4/4.12/4.12.1",
                        "--evaluation-model",
                        "--disable-ocr",
                    ]
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            extract.call_args.kwargs["scope_structure_node_ids"],
            ("scope-a", "scope-b"),
        )
        baseline_classify.assert_called_once_with(
            source_ir,
            tree,
            scope_structure_node_ids=("scope-a", "scope-b"),
        )
        semantic_provider_factory.assert_called_once_with(
            api_key="synthetic-key",
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            model="qwen3.7-plus-2026-05-26",
            seed=1234,
            timeout_seconds=90.0,
            max_retries=1,
        )

    @unittest.skipIf(llm_extraction_shadow.pymupdf is None, "PyMuPDF unavailable")
    def test_page_render_is_in_memory_png_with_verified_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            document = llm_extraction_shadow.pymupdf.open()
            page = document.new_page()
            page.insert_text((72, 72), "synthetic PDF")
            document.save(source)
            document.close()

            images = llm_extraction_shadow._render_page_images(source, (1,))

        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].image_bytes.startswith(b"\x89PNG"))
        self.assertGreater(images[0].width_px, 0)
        self.assertGreater(images[0].height_px, 0)
        self.assertEqual(
            images[0].image_sha256,
            hashlib.sha256(images[0].image_bytes).hexdigest(),
        )

    def test_visual_provider_failures_are_not_hidden_as_model_results(self) -> None:
        source = SimpleNamespace(
            diagnostics=(SimpleNamespace(code="pdf_visual_text_provider_failed"),)
        )
        with self.assertRaisesRegex(RuntimeError, "runtime_failure"):
            llm_extraction_shadow._raise_for_visual_provider_runtime_failure(source)

    def test_unknown_exception_messages_are_not_exposed_as_reason_codes(self) -> None:
        self.assertEqual(
            llm_extraction_shadow._safe_exception_code(
                RuntimeError("customer_secret_sentinel")
            ),
            "RuntimeError",
        )
        self.assertEqual(
            llm_extraction_shadow._safe_exception_code(
                RuntimeError("ocr_page_out_of_range")
            ),
            "ocr_page_out_of_range",
        )
        self.assertEqual(
            llm_extraction_shadow._safe_exception_code(
                ValueError(
                    "one semantic provision exceeds the page-image request budget"
                )
            ),
            "semantic_provision_image_budget_exceeded",
        )


if __name__ == "__main__":
    unittest.main()
