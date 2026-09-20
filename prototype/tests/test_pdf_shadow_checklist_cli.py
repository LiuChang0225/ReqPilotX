from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import unittest
import tempfile
import json
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.adapters.documents.azure_document_intelligence_visual_text_provider import (
    AzureDocumentIntelligenceVisualTextProvider,
)
from app.adapters.documents.bailian_visual_text_provider import (
    BailianVisualTextProvider,
)
from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
)
from app.domain.extraction.requirement_view import RequirementProjectionPolicy
from scripts import pdf_shadow_checklist


class PdfShadowChecklistCliTests(unittest.TestCase):
    def test_cli_summary_separates_direct_failures_from_blocked_checks(self) -> None:
        payload = pdf_shadow_checklist._report_summary_payload(
            report=SimpleNamespace(
                status=pdf_shadow_checklist.PdfShadowSilverCheckStatus.FAIL,
                passed_check_count=90,
                failed_check_count=4,
                blocked_check_count=17,
            ),
            visual_text_provider=SimpleNamespace(provider_id="aliyun-bailian-qwen-ocr"),
            visual_text_page_count=4,
            artifact_scope="FULL_DOCUMENT",
            destination=Path("/tmp/ncs-report.json"),
        )

        self.assertEqual(payload["status"], "FAIL")
        self.assertEqual(payload["passed_check_count"], 90)
        self.assertEqual(payload["failed_check_count"], 4)
        self.assertEqual(payload["blocked_check_count"], 17)

    def test_parser_defaults_to_bailian_remote_provider(self) -> None:
        args = pdf_shadow_checklist._parser().parse_args(
            [
                "--source",
                "source.pdf",
                "--document-id",
                "DOC-1",
                "--out",
                "report.json",
            ]
        )

        self.assertEqual(
            args.ocr_provider,
            pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
        )
        self.assertIsNone(args.page)
        self.assertIsNone(args.page_layout_page)

    def test_parser_accepts_explicit_layout_page_subset(self) -> None:
        args = pdf_shadow_checklist._parser().parse_args(
            [
                "--source",
                "source.pdf",
                "--document-id",
                "DOC-1",
                "--out",
                "report.json",
                "--ocr-provider",
                pdf_shadow_checklist.OCR_PROVIDER_BAILIAN_LAYOUT,
                "--page",
                "18",
                "--page",
                "19",
            ]
        )

        self.assertEqual(
            args.ocr_provider,
            pdf_shadow_checklist.OCR_PROVIDER_BAILIAN_LAYOUT,
        )
        self.assertEqual(args.page, [18, 19])

    def test_page_layout_overrides_are_explicit_full_scope_routes(self) -> None:
        authorized = frozenset({17, 18, 19})
        self.assertEqual(
            pdf_shadow_checklist._page_layout_provider_overrides_from_args(
                [18],
                authorized_page_numbers=authorized,
                selected_page_numbers=None,
                default_provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
            ),
            {18: pdf_shadow_checklist.OCR_PROVIDER_BAILIAN_LAYOUT},
        )
        for selected_pages, default_provider, reason_code in (
            (
                frozenset({18}),
                pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                "page_layout_override_requires_full_scope",
            ),
            (
                None,
                pdf_shadow_checklist.OCR_PROVIDER_AZURE,
                "page_layout_override_requires_bailian_default",
            ),
        ):
            with self.subTest(reason_code=reason_code):
                with self.assertRaisesRegex(
                    pdf_shadow_checklist.PdfShadowChecklistRejected,
                    f"^{reason_code}$",
                ):
                    (
                        pdf_shadow_checklist
                        ._page_layout_provider_overrides_from_args(
                            [18],
                            authorized_page_numbers=authorized,
                            selected_page_numbers=selected_pages,
                            default_provider_name=default_provider,
                        )
                    )

    def test_page_subset_is_nonempty_unique_positive_and_authorized(self) -> None:
        authorized = frozenset({18, 19})
        self.assertIsNone(
            pdf_shadow_checklist._selected_page_numbers_from_args(
                None,
                authorized_page_numbers=authorized,
            )
        )
        self.assertEqual(
            pdf_shadow_checklist._selected_page_numbers_from_args(
                [18],
                authorized_page_numbers=authorized,
            ),
            frozenset({18}),
        )
        invalid_cases = (
            ([], "subset_page_scope_empty"),
            ([0], "subset_page_scope_invalid"),
            ([18, 18], "subset_page_scope_duplicate"),
            ([20], "subset_page_not_authorized"),
        )
        for pages, reason_code in invalid_cases:
            with self.subTest(pages=pages):
                with self.assertRaisesRegex(
                    pdf_shadow_checklist.PdfShadowChecklistRejected,
                    f"^{reason_code}$",
                ):
                    pdf_shadow_checklist._selected_page_numbers_from_args(
                        pages,
                        authorized_page_numbers=authorized,
                    )

    def test_parser_rejects_blank_and_invalid_page_values(self) -> None:
        base = [
            "--source",
            "source.pdf",
            "--document-id",
            "DOC-1",
            "--out",
            "report.json",
            "--page",
        ]
        for value in ("", "0", "-1", "1.5", "01"):
            with self.subTest(value=value):
                with (
                    redirect_stderr(StringIO()),
                    self.assertRaises(SystemExit),
                ):
                    pdf_shadow_checklist._parser().parse_args([*base, value])

    def test_unauthorized_subset_stops_before_runtime_or_credentials(self) -> None:
        source_sha256 = "a" * 64
        silver_document = SimpleNamespace(
            source_sha256=source_sha256,
            projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            page_assets=(SimpleNamespace(page_number=18),),
        )
        registry = SimpleNamespace(documents={"DOC-1": silver_document})
        with (
            mock.patch.object(
                pdf_shadow_checklist,
                "load_pdf_shadow_silver",
                return_value=registry,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_source_sha256",
                return_value=source_sha256,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_trusted_projection_policy",
                return_value=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "build_pdf_visual_text_runtime_from_silver",
            ) as runtime_builder,
            redirect_stderr(StringIO()),
        ):
            exit_code = pdf_shadow_checklist.main(
                [
                    "--source",
                    "source.pdf",
                    "--document-id",
                    "DOC-1",
                    "--out",
                    "report.json",
                    "--page",
                    "19",
                ]
            )

        self.assertEqual(exit_code, 2)
        runtime_builder.assert_not_called()

    def test_subset_main_selects_only_p18_layout_runtime(self) -> None:
        source_sha256 = "a" * 64
        silver_document = SimpleNamespace(
            source_sha256=source_sha256,
            projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            page_assets=(
                SimpleNamespace(page_number=18),
                SimpleNamespace(page_number=19),
            ),
        )
        registry = SimpleNamespace(documents={"DOC-1": silver_document})
        provider = SimpleNamespace(
            provider_id="aliyun-bailian-qwen-ocr",
            provider_version="layout-provider-v1",
        )
        runtime = SimpleNamespace(
            pdf_adapter=object(),
            visual_text_provider=provider,
            visual_text_page_numbers=frozenset({18}),
        )
        source_ir = object()
        report = SimpleNamespace(
            status=pdf_shadow_checklist.PdfShadowSilverCheckStatus.PASS,
            passed_check_count=7,
            failed_check_count=0,
            model_dump=mock.Mock(return_value={"schema_version": "subset/v1"}),
        )
        with (
            mock.patch.object(
                pdf_shadow_checklist,
                "load_pdf_shadow_silver",
                return_value=registry,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_source_sha256",
                return_value=source_sha256,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_trusted_projection_policy",
                return_value=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "build_pdf_visual_text_runtime_from_silver",
                return_value=runtime,
            ) as runtime_builder,
            mock.patch.object(
                pdf_shadow_checklist,
                "build_source_ir",
                return_value=source_ir,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_raise_for_visual_provider_runtime_failure",
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "validate_pdf_visual_text_capture",
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "evaluate_pdf_shadow_silver_subset",
                return_value=report,
            ) as subset_evaluator,
            mock.patch.object(
                pdf_shadow_checklist,
                "evaluate_pdf_shadow_silver",
            ) as full_evaluator,
            mock.patch.object(
                pdf_shadow_checklist,
                "_write_report_atomic",
            ),
        ):
            with redirect_stdout(StringIO()):
                exit_code = pdf_shadow_checklist.main(
                    [
                        "--source",
                        "source.pdf",
                        "--document-id",
                        "DOC-1",
                        "--out",
                        "report.json",
                        "--ocr-provider",
                        pdf_shadow_checklist.OCR_PROVIDER_BAILIAN_LAYOUT,
                        "--page",
                        "18",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            runtime_builder.call_args.kwargs["visual_text_page_numbers"],
            frozenset({18}),
        )
        self.assertEqual(
            runtime_builder.call_args.kwargs["provider_name"],
            pdf_shadow_checklist.OCR_PROVIDER_BAILIAN_LAYOUT,
        )
        self.assertEqual(
            subset_evaluator.call_args.kwargs["selected_page_numbers"],
            (18,),
        )
        self.assertEqual(
            subset_evaluator.call_args.kwargs["expected_evaluated_content_types"],
            pdf_shadow_checklist.VISUAL_CONTENT_TYPES,
        )
        full_evaluator.assert_not_called()

    def test_full_scope_main_remains_the_default_compatibility_path(self) -> None:
        source_sha256 = "a" * 64
        silver_document = SimpleNamespace(
            source_sha256=source_sha256,
            projection_policy=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            page_assets=(SimpleNamespace(page_number=18),),
        )
        registry = SimpleNamespace(documents={"DOC-1": silver_document})
        provider = SimpleNamespace(
            provider_id="aliyun-bailian-qwen-ocr",
            provider_version="ocr-provider-v1",
        )
        runtime = SimpleNamespace(
            pdf_adapter=object(),
            visual_text_provider=provider,
            visual_text_page_numbers=frozenset({18}),
        )
        source_ir = object()
        structure_tree = object()
        semantics = object()
        requirement_view = object()
        report = SimpleNamespace(
            status=pdf_shadow_checklist.PdfShadowSilverCheckStatus.PASS,
            passed_check_count=3,
            failed_check_count=0,
            model_dump=mock.Mock(return_value={"schema_version": "full/v1"}),
        )
        with (
            mock.patch.object(
                pdf_shadow_checklist,
                "load_pdf_shadow_silver",
                return_value=registry,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_source_sha256",
                return_value=source_sha256,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_trusted_projection_policy",
                return_value=RequirementProjectionPolicy.SHADOW_CANDIDATES,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "build_pdf_visual_text_runtime_from_silver",
                return_value=runtime,
            ) as runtime_builder,
            mock.patch.object(
                pdf_shadow_checklist,
                "build_source_ir",
                return_value=source_ir,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_raise_for_visual_provider_runtime_failure",
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "validate_pdf_visual_text_capture",
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "build_structure_tree",
                return_value=structure_tree,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "classify_content",
                return_value=semantics,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "project_requirements",
                return_value=requirement_view,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "evaluate_pdf_shadow_silver",
                return_value=report,
            ) as full_evaluator,
            mock.patch.object(
                pdf_shadow_checklist,
                "evaluate_pdf_shadow_silver_subset",
            ) as subset_evaluator,
            mock.patch.object(
                pdf_shadow_checklist,
                "_write_report_atomic",
            ),
        ):
            with redirect_stdout(StringIO()):
                exit_code = pdf_shadow_checklist.main(
                    [
                        "--source",
                        "source.pdf",
                        "--document-id",
                        "DOC-1",
                        "--out",
                        "report.json",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertIsNone(runtime_builder.call_args.kwargs["visual_text_page_numbers"])
        self.assertEqual(
            runtime_builder.call_args.kwargs["page_provider_names_by_page"],
            {},
        )
        full_evaluator.assert_called_once()
        subset_evaluator.assert_not_called()

    def test_bailian_factory_reuses_configured_remote_credentials(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url=("https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        with (
            mock.patch.object(
                pdf_shadow_checklist,
                "load_ai_copilot_config",
                return_value=config,
            ),
            mock.patch.object(
                pdf_shadow_checklist,
                "_runtime_setting",
                return_value="qwen3.5-ocr",
            ),
        ):
            provider = pdf_shadow_checklist._build_visual_text_provider(
                provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                bailian_model=None,
            )

        self.assertIsInstance(provider, BailianVisualTextProvider)
        self.assertEqual(provider.provider_id, "aliyun-bailian-qwen-ocr")

    def test_azure_factory_uses_only_document_intelligence_configuration(self) -> None:
        def setting(*names: str) -> str:
            return (
                "https://example.cognitiveservices.azure.com"
                if "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT" in names
                else "test-key"
            )

        with mock.patch.object(
            pdf_shadow_checklist,
            "_runtime_setting",
            side_effect=setting,
        ):
            provider = pdf_shadow_checklist._build_visual_text_provider(
                provider_name=pdf_shadow_checklist.OCR_PROVIDER_AZURE,
                bailian_model=None,
            )

        self.assertIsInstance(
            provider,
            AzureDocumentIntelligenceVisualTextProvider,
        )
        self.assertEqual(provider.provider_id, "azure-document-intelligence")

    def test_missing_remote_configuration_fails_before_document_processing(
        self,
    ) -> None:
        config = SimpleNamespace(dashscope_api_key="", dashscope_base_url="")
        with mock.patch.object(
            pdf_shadow_checklist,
            "load_ai_copilot_config",
            return_value=config,
        ):
            with self.assertRaisesRegex(RuntimeError, "not_configured"):
                pdf_shadow_checklist._build_visual_text_provider(
                    provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                    bailian_model=None,
                )

    def test_empty_registered_scope_skips_credentials_and_provider_construction(
        self,
    ) -> None:
        with mock.patch.object(
            pdf_shadow_checklist,
            "_build_visual_text_provider",
        ) as provider_factory:
            adapter, provider = pdf_shadow_checklist._build_scoped_pdf_adapter(
                visual_text_page_numbers=frozenset(),
                allowed_visual_provider_ids=frozenset(
                    {
                        "aliyun-bailian-qwen-ocr",
                        "azure-document-intelligence",
                    }
                ),
                provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                bailian_model=None,
            )

        provider_factory.assert_not_called()
        self.assertIsNone(provider)
        self.assertEqual(adapter.adapter_version, PDF_SOURCE_IR_ADAPTER_VERSION)

    def test_disallowed_remote_provider_is_rejected_before_credentials(self) -> None:
        with mock.patch.object(
            pdf_shadow_checklist,
            "_build_visual_text_provider",
        ) as provider_factory:
            with self.assertRaisesRegex(RuntimeError, "not_allowed"):
                pdf_shadow_checklist._build_scoped_pdf_adapter(
                    visual_text_page_numbers=frozenset({11}),
                    allowed_visual_provider_ids=frozenset(
                        {"azure-document-intelligence"}
                    ),
                    provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                    bailian_model=None,
                )

        provider_factory.assert_not_called()

    def test_ncs_four_page_rotation_scope_is_passed_to_bailian_factory(self) -> None:
        rotations = {1: 90, 2: 90, 3: 90, 4: 90}
        fake_provider = SimpleNamespace(
            provider_id="aliyun-bailian-qwen-ocr",
            provider_version="test-provider-version",
        )
        with mock.patch.object(
            pdf_shadow_checklist,
            "_build_visual_text_provider",
            return_value=fake_provider,
        ) as provider_factory:
            _, provider = pdf_shadow_checklist._build_scoped_pdf_adapter(
                visual_text_page_numbers=frozenset(rotations),
                input_rotations_by_page=rotations,
                allowed_visual_provider_ids=frozenset({"aliyun-bailian-qwen-ocr"}),
                provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                bailian_model=None,
            )

        self.assertIs(provider, fake_provider)
        provider_factory.assert_called_once_with(
            provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
            bailian_model=None,
            input_rotations_by_page=rotations,
        )

    def test_explicit_rotation_disables_bailian_auto_rotation(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url=("https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        cases = (
            ({1: 0, 2: 0}, True),
            ({1: 90, 2: 90}, False),
        )
        for rotations, expected_enable_rotate in cases:
            with (
                self.subTest(rotations=rotations),
                mock.patch.object(
                    pdf_shadow_checklist,
                    "load_ai_copilot_config",
                    return_value=config,
                ),
                mock.patch.object(
                    pdf_shadow_checklist,
                    "_runtime_setting",
                    return_value="qwen3.5-ocr",
                ),
                mock.patch.object(
                    pdf_shadow_checklist,
                    "BailianVisualTextProvider",
                ) as provider_type,
            ):
                pdf_shadow_checklist._build_visual_text_provider(
                    provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                    bailian_model=None,
                    input_rotations_by_page=rotations,
                )

            provider_type.assert_called_once_with(
                api_key="test-key",
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
                model="qwen3.5-ocr",
                enable_rotate=expected_enable_rotate,
                input_rotations_by_page=rotations,
            )

    def test_azure_rejects_explicit_rotation_before_configuration_lookup(self) -> None:
        with (
            mock.patch.object(
                pdf_shadow_checklist,
                "_runtime_setting",
            ) as runtime_setting,
            mock.patch.object(
                pdf_shadow_checklist,
                "AzureDocumentIntelligenceVisualTextProvider",
            ) as provider_type,
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "azure_input_rotation_not_supported",
            ):
                pdf_shadow_checklist._build_visual_text_provider(
                    provider_name=pdf_shadow_checklist.OCR_PROVIDER_AZURE,
                    bailian_model=None,
                    input_rotations_by_page={1: 90},
                )

        runtime_setting.assert_not_called()
        provider_type.assert_not_called()

    def test_rotation_map_must_match_visual_page_scope_exactly(self) -> None:
        invalid_maps = (
            {1: 90},
            {1: 90, 2: 90, 3: 90},
        )
        for rotations in invalid_maps:
            with self.subTest(rotations=rotations):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "visual_text_rotation_scope_mismatch",
                ):
                    pdf_shadow_checklist._build_scoped_pdf_adapter(
                        visual_text_page_numbers=frozenset({1, 2}),
                        input_rotations_by_page=rotations,
                        allowed_visual_provider_ids=frozenset(
                            {"aliyun-bailian-qwen-ocr"}
                        ),
                        provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                        bailian_model=None,
                    )

    def test_rotation_map_rejects_non_quarter_turn_and_boolean_values(self) -> None:
        for rotation in (45, True):
            with self.subTest(rotation=rotation):
                with self.assertRaisesRegex(ValueError, "input rotations"):
                    pdf_shadow_checklist._build_scoped_pdf_adapter(
                        visual_text_page_numbers=frozenset({1}),
                        input_rotations_by_page={1: rotation},
                        allowed_visual_provider_ids=frozenset(
                            {"aliyun-bailian-qwen-ocr"}
                        ),
                        provider_name=pdf_shadow_checklist.OCR_PROVIDER_BAILIAN,
                        bailian_model=None,
                    )

    def test_provider_diagnostic_is_a_runtime_error_not_a_silver_mismatch(self) -> None:
        failed_source_ir = SimpleNamespace(
            diagnostics=(
                SimpleNamespace(
                    code="pdf_visual_text_provider_failed",
                    native_locator=SimpleNamespace(page_number=17),
                ),
                SimpleNamespace(
                    code="pdf_visual_text_provider_output_invalid",
                    native_locator=SimpleNamespace(page_number=19),
                ),
            )
        )
        with self.assertRaisesRegex(
            pdf_shadow_checklist.PdfShadowChecklistRejected,
            "runtime_failure",
        ) as raised:
            pdf_shadow_checklist._raise_for_visual_provider_runtime_failure(
                failed_source_ir
            )
        self.assertEqual(
            raised.exception.failure_codes,
            (
                "pdf_visual_text_provider_failed",
                "pdf_visual_text_provider_output_invalid",
            ),
        )
        self.assertEqual(raised.exception.failure_pages, (17, 19))

    def test_source_hash_is_checked_before_remote_document_processing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"approved source bytes")

            self.assertEqual(
                pdf_shadow_checklist._source_sha256(source),
                "b01e8e183a4d2b4c54506948009df2a795353583a0262f55b6454848237785c6",
            )

    def test_trusted_ingress_route_selects_projection_policy(self) -> None:
        source_sha256 = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            for route, expected in (
                (
                    "requirement_source",
                    RequirementProjectionPolicy.SHADOW_CANDIDATES,
                ),
                (
                    "downstream_artifact",
                    RequirementProjectionPolicy.NON_REQUIREMENT_ARTIFACT,
                ),
            ):
                with self.subTest(route=route):
                    manifest.write_text(
                        json.dumps(
                            {
                                "schema_version": "1.2.0",
                                "documents": {
                                    "DOC-1": {
                                        "source_sha256": source_sha256,
                                        "ingress_route": route,
                                    }
                                },
                            }
                        ),
                        encoding="utf-8",
                    )
                    self.assertIs(
                        pdf_shadow_checklist._trusted_projection_policy(
                            manifest_path=manifest,
                            document_id="DOC-1",
                            source_sha256=source_sha256,
                        ),
                        expected,
                    )

    def test_trusted_projection_policy_rejects_source_or_route_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "schema_version": "1.2.0",
                        "documents": {
                            "DOC-1": {
                                "source_sha256": "a" * 64,
                                "ingress_route": "attachment",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "source_mismatch"):
                pdf_shadow_checklist._trusted_projection_policy(
                    manifest_path=manifest,
                    document_id="DOC-1",
                    source_sha256="b" * 64,
                )
            with self.assertRaisesRegex(RuntimeError, "route_unsupported"):
                pdf_shadow_checklist._trusted_projection_policy(
                    manifest_path=manifest,
                    document_id="DOC-1",
                    source_sha256="a" * 64,
                )


if __name__ == "__main__":
    unittest.main()
