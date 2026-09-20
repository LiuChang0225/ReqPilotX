from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

if __package__:
    from .private_eval import require_private_eval
else:
    from private_eval import require_private_eval

import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from app.adapters.documents.bailian_visual_text_provider import (
    BAILIAN_VISUAL_TEXT_PROVIDER_ID,
)
from app.adapters.documents.pdf_source_ir_adapter import (
    PDF_SOURCE_IR_ADAPTER_VERSION,
)
from app.adapters.documents import pdf_visual_text_runtime
from app.application.extraction.evaluate_pdf_shadow_silver import (
    load_pdf_shadow_silver,
)


class PdfVisualTextRuntimeTests(unittest.TestCase):
    def test_real_pef_silver_entry_yields_only_reviewed_visual_route(self) -> None:
        silver_document = load_pdf_shadow_silver(
            require_private_eval("extraction_cases/silver.json")
        ).documents["DOC-PEF-SOR"]

        route = pdf_visual_text_runtime.pdf_visual_text_route_from_silver(
            silver_document
        )

        self.assertEqual(route.source_sha256, silver_document.source_sha256)
        expected_pages = frozenset({1, 2, *range(6, 31)})
        self.assertEqual(route.page_numbers, expected_pages)
        self.assertEqual(
            dict(route.input_rotations_by_page),
            {page_number: 0 for page_number in sorted(expected_pages)},
        )

    def test_source_hash_mismatch_fails_before_provider_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"actual source")
            silver_document = _silver_route(
                source_sha256="0" * 64,
                pages=((3, 0),),
            )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
            ) as provider_factory:
                with self.assertRaisesRegex(RuntimeError, "source_hash_mismatch"):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                    )

        provider_factory.assert_not_called()

    def test_exact_page_scope_and_rotation_are_derived_from_silver_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((4, 90), (1, 0)),
            )
            provider = SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                provider_version="test-provider-version",
            )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
                return_value=provider,
            ) as provider_factory:
                runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN,
                    )
                )

        self.assertIs(runtime.visual_text_provider, provider)
        self.assertIsNone(runtime.visual_table_provider)
        self.assertEqual(runtime.visual_table_page_numbers, frozenset())
        self.assertEqual(runtime.route.page_numbers, frozenset({1, 4}))
        self.assertEqual(dict(runtime.route.input_rotations_by_page), {1: 0, 4: 90})
        self.assertEqual(
            runtime.pdf_adapter._visual_text_page_numbers,
            frozenset({1, 4}),
        )
        provider_factory.assert_called_once()
        self.assertEqual(
            dict(provider_factory.call_args.kwargs["input_rotations_by_page"]),
            {1: 0, 4: 90},
        )

    def test_explicit_page_provider_route_is_bound_and_has_no_fallback(self) -> None:
        default_provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="ocr-v1",
            recognize_page=mock.Mock(return_value="default-result"),
        )
        layout_provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="layout-v1",
            recognize_page=mock.Mock(return_value="layout-result"),
        )
        router = pdf_visual_text_runtime.ExplicitPageVisualTextProviderRouter(
            default_provider=default_provider,
            providers_by_page={18: layout_provider},
        )

        self.assertEqual(
            router.recognize_page(SimpleNamespace(page_number=17)),
            "default-result",
        )
        self.assertEqual(
            router.recognize_page(SimpleNamespace(page_number=18)),
            "layout-result",
        )
        self.assertEqual(router.provider_version_for_page(17), "ocr-v1")
        self.assertEqual(router.provider_version_for_page(18), "layout-v1")
        self.assertTrue(router.provider_version.startswith("explicit-page-router/v1:"))

        layout_provider.recognize_page.side_effect = RuntimeError("layout failed")
        default_provider.recognize_page.reset_mock()
        with self.assertRaisesRegex(RuntimeError, "layout failed"):
            router.recognize_page(SimpleNamespace(page_number=18))
        default_provider.recognize_page.assert_not_called()

    def test_runtime_builds_only_declared_same_id_page_provider_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((17, 0), (18, 0)),
            )
            providers = {
                pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN: SimpleNamespace(
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                    provider_version="ocr-v1",
                ),
                pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT: SimpleNamespace(
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                    provider_version="layout-v1",
                ),
            }

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
                side_effect=lambda **kwargs: providers[kwargs["provider_name"]],
            ) as provider_factory:
                runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN,
                        page_provider_names_by_page={
                            18: (
                                pdf_visual_text_runtime
                                .OCR_PROVIDER_BAILIAN_LAYOUT
                            )
                        },
                    )
                )

        self.assertIsInstance(
            runtime.visual_text_provider,
            pdf_visual_text_runtime.ExplicitPageVisualTextProviderRouter,
        )
        assert runtime.visual_text_provider is not None
        self.assertEqual(
            runtime.visual_text_provider.provider_version_for_page(17),
            "ocr-v1",
        )
        self.assertEqual(
            runtime.visual_text_provider.provider_version_for_page(18),
            "layout-v1",
        )
        self.assertEqual(provider_factory.call_count, 2)

    def test_page_provider_override_outside_selected_scope_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((17, 0), (18, 0)),
            )
            with self.assertRaisesRegex(ValueError, "outside the selected"):
                pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                    source=source,
                    silver_document=silver_document,
                    visual_text_page_numbers=frozenset({17}),
                    page_provider_names_by_page={
                        18: pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT
                    },
                )

    def test_visual_table_scope_is_explicit_subset_and_builds_only_when_selected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((1, 0), (4, 0)),
            )
            page_provider = SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                provider_version="page-v1",
            )
            table_provider = SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                provider_version="table-v1",
            )
            with (
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "build_remote_visual_text_provider",
                    return_value=page_provider,
                ),
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "build_remote_visual_table_provider",
                    return_value=table_provider,
                ) as table_factory,
            ):
                runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        visual_table_page_numbers=frozenset({4}),
                    )
                )

        self.assertIs(runtime.visual_table_provider, table_provider)
        self.assertEqual(runtime.visual_table_page_numbers, frozenset({4}))
        self.assertEqual(
            runtime.pdf_adapter._visual_table_page_numbers,
            frozenset({4}),
        )
        table_factory.assert_called_once()

    def test_visual_text_capture_scope_is_a_subset_of_the_authorized_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((1, 0), (4, 90)),
            )
            provider = SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                provider_version="test-provider-version",
            )
            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
                return_value=provider,
            ) as provider_factory:
                runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        visual_text_page_numbers=frozenset({4}),
                    )
                )

        self.assertEqual(runtime.route.page_numbers, frozenset({1, 4}))
        self.assertEqual(runtime.visual_text_page_numbers, frozenset({4}))
        self.assertEqual(
            runtime.pdf_adapter._visual_text_page_numbers,
            frozenset({4}),
        )
        self.assertEqual(
            provider_factory.call_args.kwargs["input_rotations_by_page"],
            {1: 0, 4: 90},
        )

    def test_p18_layout_subset_keeps_full_route_identity_but_invokes_only_p18(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((18, 0), (19, 90)),
            )

            def provider_for_route(**kwargs: object) -> object:
                rotations = dict(kwargs["input_rotations_by_page"])
                return SimpleNamespace(
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                    provider_version=f"layout-provider-v1;rot={sorted(rotations.items())}",
                )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
                side_effect=provider_for_route,
            ) as provider_factory:
                subset_runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=(
                            pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT
                        ),
                        visual_text_page_numbers=frozenset({18}),
                    )
                )
                full_runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=(
                            pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT
                        ),
                    )
                )

        self.assertEqual(subset_runtime.route.page_numbers, frozenset({18, 19}))
        self.assertEqual(subset_runtime.visual_text_page_numbers, frozenset({18}))
        self.assertEqual(
            subset_runtime.pdf_adapter._visual_text_page_numbers,
            frozenset({18}),
        )
        self.assertEqual(
            subset_runtime.visual_text_provider.provider_version,
            full_runtime.visual_text_provider.provider_version,
        )
        self.assertEqual(provider_factory.call_count, 2)
        self.assertTrue(
            all(
                dict(call.kwargs["input_rotations_by_page"]) == {18: 0, 19: 90}
                for call in provider_factory.call_args_list
            )
        )

    def test_ncs_rotation_profile_is_identical_for_full_and_subset_layout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed ncs source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed ncs source"),
                pages=((1, 90), (2, 90), (3, 90), (4, 90)),
            )

            def actual_layout_provider(**kwargs: object) -> object:
                return pdf_visual_text_runtime.BailianPageLayoutProvider(
                    api_key="test-key",
                    base_url="https://dashscope.example/compatible-mode/v1",
                    model="qwen3.7-plus",
                    input_rotations_by_page=dict(kwargs["input_rotations_by_page"]),
                )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
                side_effect=actual_layout_provider,
            ) as provider_factory:
                subset_runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=(
                            pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT
                        ),
                        visual_text_page_numbers=frozenset({2}),
                    )
                )
                full_runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=(
                            pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT
                        ),
                    )
                )

        self.assertEqual(
            subset_runtime.visual_text_provider.provider_version,
            full_runtime.visual_text_provider.provider_version,
        )
        self.assertEqual(
            subset_runtime.pdf_adapter._visual_text_page_numbers,
            frozenset({2}),
        )
        self.assertTrue(
            all(
                dict(call.kwargs["input_rotations_by_page"])
                == {1: 90, 2: 90, 3: 90, 4: 90}
                for call in provider_factory.call_args_list
            )
        )

    def test_azure_subset_cannot_ignore_rotated_pages_in_full_identity_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((1, 90), (2, 0)),
                allowed_provider_ids=("azure-document-intelligence",),
            )
            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
            ) as provider_factory:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "azure_input_rotation_not_supported",
                ):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=pdf_visual_text_runtime.OCR_PROVIDER_AZURE,
                        visual_text_page_numbers=frozenset({2}),
                    )

        provider_factory.assert_not_called()

    def test_visual_table_scope_outside_visual_route_fails_before_providers(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((1, 0),),
            )
            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
            ) as provider_factory:
                with self.assertRaisesRegex(RuntimeError, "scope_mismatch"):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        visual_table_page_numbers=frozenset({2}),
                    )

        provider_factory.assert_not_called()

    def test_disallowed_remote_provider_fails_before_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"reviewed source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"reviewed source"),
                pages=((1, 0),),
                allowed_provider_ids=("azure-document-intelligence",),
            )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
            ) as provider_factory:
                with self.assertRaisesRegex(RuntimeError, "not_allowed"):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN,
                    )

        provider_factory.assert_not_called()

    def test_local_provider_name_is_rejected_even_for_empty_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"native source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"native source"),
                pages=(),
            )

            with mock.patch.object(
                pdf_visual_text_runtime,
                "build_remote_visual_text_provider",
            ) as provider_factory:
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name="local-ocr",
                    )

        provider_factory.assert_not_called()

    def test_empty_scope_still_enforces_provider_whitelist_without_credentials(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"native source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"native source"),
                pages=(),
                allowed_provider_ids=("azure-document-intelligence",),
            )

            with (
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "build_remote_visual_text_provider",
                ) as provider_factory,
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "_load_ai_copilot_config",
                ) as credential_loader,
            ):
                with self.assertRaisesRegex(RuntimeError, "not_allowed"):
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                        provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN,
                    )

        provider_factory.assert_not_called()
        credential_loader.assert_not_called()

    def test_empty_scope_does_not_resolve_credentials_or_build_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(b"native source")
            silver_document = _silver_route(
                source_sha256=_sha256(b"native source"),
                pages=(),
            )

            with (
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "build_remote_visual_text_provider",
                ) as provider_factory,
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "_load_ai_copilot_config",
                ) as credential_loader,
                mock.patch.object(
                    pdf_visual_text_runtime,
                    "runtime_setting",
                ) as runtime_setting_loader,
            ):
                runtime = (
                    pdf_visual_text_runtime.build_pdf_visual_text_runtime_from_silver(
                        source=source,
                        silver_document=silver_document,
                    )
                )

        provider_factory.assert_not_called()
        credential_loader.assert_not_called()
        runtime_setting_loader.assert_not_called()
        self.assertIsNone(runtime.visual_text_provider)
        self.assertEqual(
            runtime.pdf_adapter.adapter_version,
            PDF_SOURCE_IR_ADAPTER_VERSION,
        )

    def test_invalid_silver_provider_whitelist_rejects_local_provider_id(self) -> None:
        silver_document = _silver_route(
            source_sha256="a" * 64,
            pages=(),
            allowed_provider_ids=("local-ocr",),
        )

        with self.assertRaisesRegex(ValueError, "provider whitelist"):
            pdf_visual_text_runtime.pdf_visual_text_route_from_silver(silver_document)

    def test_shared_bailian_factory_uses_injected_credentials(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
        )
        provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="test-provider-version",
        )
        provider_type = mock.Mock(return_value=provider)
        runtime_setting_loader = mock.Mock(return_value="test-ocr-model")

        actual = pdf_visual_text_runtime.build_remote_visual_text_provider(
            provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN,
            bailian_model=None,
            input_rotations_by_page={1: 90},
            bailian_config_loader=mock.Mock(return_value=config),
            runtime_setting_loader=runtime_setting_loader,
            bailian_provider_type=provider_type,
        )

        self.assertIs(actual, provider)
        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/v1",
            model="test-ocr-model",
            enable_rotate=False,
            input_rotations_by_page={1: 90},
        )

    def test_qwen37_page_layout_factory_uses_separate_model_setting(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/compatible-mode/v1",
        )
        provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="qwen37-layout-v1",
        )
        provider_type = mock.Mock(return_value=provider)
        runtime_setting_loader = mock.Mock(return_value="qwen3.7-plus")

        actual = pdf_visual_text_runtime.build_remote_visual_text_provider(
            provider_name=pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_LAYOUT,
            bailian_model=None,
            input_rotations_by_page={1: 0, 2: 90},
            bailian_config_loader=mock.Mock(return_value=config),
            runtime_setting_loader=runtime_setting_loader,
            bailian_layout_provider_type=provider_type,
        )

        self.assertIs(actual, provider)
        runtime_setting_loader.assert_called_once_with(
            "REQPILOT_ALIYUN_PAGE_LAYOUT_MODEL"
        )
        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            model="qwen3.7-plus",
            input_rotations_by_page={1: 0, 2: 90},
        )

    def test_qwen36_flash_page_layout_factory_uses_its_own_model_setting(
        self,
    ) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/compatible-mode/v1",
        )
        provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="qwen36-flash-layout-v1",
        )
        provider_type = mock.Mock(return_value=provider)
        runtime_setting_loader = mock.Mock(return_value="")

        actual = pdf_visual_text_runtime.build_remote_visual_text_provider(
            provider_name=(pdf_visual_text_runtime.OCR_PROVIDER_BAILIAN_FLASH_LAYOUT),
            bailian_model=None,
            input_rotations_by_page={1: 0, 2: 90},
            bailian_config_loader=mock.Mock(return_value=config),
            runtime_setting_loader=runtime_setting_loader,
            bailian_layout_provider_type=provider_type,
        )

        self.assertIs(actual, provider)
        runtime_setting_loader.assert_called_once_with(
            "REQPILOT_ALIYUN_FLASH_PAGE_LAYOUT_MODEL"
        )
        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/compatible-mode/v1",
            model="qwen3.6-flash",
            input_rotations_by_page={1: 0, 2: 90},
        )

    def test_visual_table_factory_is_bailian_only_and_exposes_version(self) -> None:
        config = SimpleNamespace(
            dashscope_api_key="test-key",
            dashscope_base_url="https://dashscope.example/v1",
        )
        provider = SimpleNamespace(
            provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
            provider_version="table-parser-v1",
        )
        provider_type = mock.Mock(return_value=provider)

        actual = pdf_visual_text_runtime.build_remote_visual_table_provider(
            bailian_model="qwen3.5-ocr",
            bailian_config_loader=mock.Mock(return_value=config),
            runtime_setting_loader=mock.Mock(return_value=""),
            bailian_provider_type=provider_type,
        )

        self.assertIs(actual, provider)
        self.assertEqual(actual.provider_version, "table-parser-v1")
        provider_type.assert_called_once_with(
            api_key="test-key",
            base_url="https://dashscope.example/v1",
            model="qwen3.5-ocr",
        )

    def test_capture_validation_binds_exact_page_hash_and_provider(self) -> None:
        digest = _sha256(b"reviewed source")
        route = pdf_visual_text_runtime.pdf_visual_text_route_from_silver(
            _silver_route(source_sha256=digest, pages=((4, 0),))
        )
        expected_page_hash = route.page_bindings[4].page_image_sha256
        runtime = SimpleNamespace(
            route=route,
            visual_text_provider=SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID
            ),
        )
        source_ir = SimpleNamespace(
            source_sha256=digest,
            visual_text_snapshots=(
                SimpleNamespace(
                    source_sha256=digest,
                    page_number=4,
                    page_image_sha256=expected_page_hash,
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                ),
            ),
            diagnostics=(
                SimpleNamespace(
                    code="pdf_full_page_image_without_visible_text",
                    native_locator=SimpleNamespace(page_number=4),
                ),
            ),
            nodes=(),
            assets=(),
            visual_table_page_numbers=(),
            visual_table_snapshots=(),
        )

        pdf_visual_text_runtime.validate_pdf_visual_text_capture(source_ir, runtime)

        source_ir.visual_text_snapshots[0].page_image_sha256 = "f" * 64
        with self.assertRaisesRegex(RuntimeError, "page_hash_mismatch"):
            pdf_visual_text_runtime.validate_pdf_visual_text_capture(
                source_ir,
                runtime,
            )

    def test_simple_router_skips_authorized_native_sufficient_pages(self) -> None:
        digest = _sha256(b"reviewed source")
        route = pdf_visual_text_runtime.pdf_visual_text_route_from_silver(
            _silver_route(source_sha256=digest, pages=((1, 0), (2, 0)))
        )
        runtime = SimpleNamespace(
            route=route,
            visual_text_provider=SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID
            ),
            visual_table_provider=None,
            visual_table_page_numbers=frozenset(),
        )
        source_ir = SimpleNamespace(
            source_sha256=digest,
            visual_text_snapshots=(
                SimpleNamespace(
                    source_sha256=digest,
                    page_number=2,
                    page_image_sha256=route.page_bindings[2].page_image_sha256,
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                ),
            ),
            diagnostics=(
                SimpleNamespace(
                    code="pdf_full_page_image_with_invisible_text_layer",
                    native_locator=SimpleNamespace(page_number=2),
                ),
            ),
            nodes=(),
            assets=(),
            visual_table_page_numbers=(),
            visual_table_snapshots=(),
        )

        pdf_visual_text_runtime.validate_pdf_visual_text_capture(source_ir, runtime)

        self.assertEqual(
            pdf_visual_text_runtime.pdf_visual_text_routing_reasons(source_ir),
            {2: ("FULL_PAGE_IMAGE_UNTRUSTED_TEXT",)},
        )

    def test_capture_validation_accepts_proven_out_of_scope_risk_pages(self) -> None:
        digest = _sha256(b"reviewed source")
        route = pdf_visual_text_runtime.pdf_visual_text_route_from_silver(
            _silver_route(source_sha256=digest, pages=((2, 0),))
        )
        runtime = SimpleNamespace(
            route=route,
            visual_text_page_numbers=frozenset({2}),
            visual_text_provider=SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID
            ),
            visual_table_provider=None,
            visual_table_page_numbers=frozenset(),
        )
        source_ir = SimpleNamespace(
            source_sha256=digest,
            visual_text_snapshots=(
                SimpleNamespace(
                    source_sha256=digest,
                    page_number=2,
                    page_image_sha256=route.page_bindings[2].page_image_sha256,
                    provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID,
                ),
            ),
            diagnostics=(
                SimpleNamespace(
                    code="pdf_vector_text_with_low_native_unicode_coverage",
                    native_locator=SimpleNamespace(page_number=1),
                ),
                SimpleNamespace(
                    code="pdf_visual_text_page_out_of_scope",
                    native_locator=SimpleNamespace(page_number=1),
                ),
                SimpleNamespace(
                    code="pdf_vector_text_with_low_native_unicode_coverage",
                    native_locator=SimpleNamespace(page_number=2),
                ),
            ),
            nodes=(),
            assets=(),
            visual_table_page_numbers=(),
            visual_table_snapshots=(),
        )

        pdf_visual_text_runtime.validate_pdf_visual_text_capture(source_ir, runtime)

        source_ir.diagnostics = tuple(
            diagnostic
            for diagnostic in source_ir.diagnostics
            if diagnostic.code != "pdf_visual_text_page_out_of_scope"
        )
        with self.assertRaisesRegex(RuntimeError, "required_page_not_authorized"):
            pdf_visual_text_runtime.validate_pdf_visual_text_capture(
                source_ir,
                runtime,
            )

    def test_simple_router_rejects_required_page_outside_authorization(self) -> None:
        digest = _sha256(b"reviewed source")
        route = pdf_visual_text_runtime.pdf_visual_text_route_from_silver(
            _silver_route(source_sha256=digest, pages=((1, 0),))
        )
        runtime = SimpleNamespace(
            route=route,
            visual_text_provider=SimpleNamespace(
                provider_id=BAILIAN_VISUAL_TEXT_PROVIDER_ID
            ),
            visual_table_provider=None,
            visual_table_page_numbers=frozenset(),
        )
        source_ir = SimpleNamespace(
            source_sha256=digest,
            visual_text_snapshots=(),
            diagnostics=(
                SimpleNamespace(
                    code="pdf_vector_text_with_low_native_unicode_coverage",
                    native_locator=SimpleNamespace(page_number=2),
                ),
            ),
            nodes=(),
            assets=(),
            visual_table_page_numbers=(),
            visual_table_snapshots=(),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "required_page_not_authorized",
        ):
            pdf_visual_text_runtime.validate_pdf_visual_text_capture(
                source_ir,
                runtime,
            )

    def test_route_rejects_page_without_exact_hash_binding(self) -> None:
        silver_document = _silver_route(
            source_sha256="a" * 64,
            pages=((1, 0),),
        )
        silver_document.page_assets[0].page_image_sha256 = None

        with self.assertRaisesRegex(ValueError, "exactly one page hash"):
            pdf_visual_text_runtime.pdf_visual_text_route_from_silver(silver_document)


def _silver_route(
    *,
    source_sha256: str,
    pages: tuple[tuple[int, int], ...],
    allowed_provider_ids: tuple[str, ...] = (
        "aliyun-bailian-qwen-ocr",
        "azure-document-intelligence",
    ),
) -> SimpleNamespace:
    # This intentionally exposes no semantic/projection expectation fields.
    return SimpleNamespace(
        source_sha256=source_sha256,
        allowed_visual_provider_ids=allowed_provider_ids,
        page_assets=tuple(
            SimpleNamespace(
                page_number=page_number,
                input_rotation_cw_degrees=rotation,
                source_asset_sha256=None,
                page_image_sha256=_sha256(f"page-{page_number}".encode("utf-8")),
            )
            for page_number, rotation in pages
        ),
    )


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


if __name__ == "__main__":
    unittest.main()
