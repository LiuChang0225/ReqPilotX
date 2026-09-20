from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import copy
import hashlib
import json
import os
import re
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from unittest import mock

import pymupdf
from PIL import Image

from app.application.extraction.build_shadow_preview import (
    PAGE_COMPARISON_SCHEMA_VERSION,
    SHADOW_PREVIEW_SCHEMA_VERSION,
)
from scripts.extraction_case_runner import CorpusPathError
from scripts.pdf_visual_review_bundle import (
    PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION,
    STRUCTURE_ONLY_INSPECTION_STATUS,
    PdfVisualReviewBundleRejected,
    _annotate_page_png,
    canonical_bundle_sha256,
    export_pdf_visual_review_bundle,
    inspect_pdf_visual_review_bundle,
    inspect_pdf_visual_review_bundle_structure_only,
    verify_pdf_visual_review_bundle,
)
import scripts.pdf_visual_review_bundle as bundle_module
import scripts.pdf_visual_ai_pre_review as ai_pre_review_module


class PdfVisualReviewBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.corpus_root = self.root / "corpus"
        self.corpus_root.mkdir()
        self.source = self.corpus_root / "source.pdf"
        image_buffer = BytesIO()
        Image.new("RGB", (48, 48), "#2563eb").save(
            image_buffer,
            format="PNG",
        )
        embedded_image = image_buffer.getvalue()
        document = pymupdf.open()
        for page_number in range(1, 4):
            page = document.new_page(width=420, height=595)
            page.insert_text(
                (48, 72),
                f"{page_number}. CUSTOMER SECRET PAGE {page_number}",
                fontsize=14,
            )
            page.draw_rect(
                pymupdf.Rect(45, 100, 375, 180),
                color=(0.2, 0.2, 0.2),
                width=1,
            )
            page.insert_text(
                (60, 135),
                f"Requirement content for synthetic page {page_number}",
                fontsize=11,
            )
            if page_number == 2:
                page.insert_image(
                    pymupdf.Rect(240, 230, 320, 310),
                    stream=embedded_image,
                )
        document.save(self.source)
        document.close()
        self.source_sha256 = _sha256(self.source)
        self.output_root = self.root / "reports"

    def _export_remote_fixture(
        self,
        destination: Path,
        *,
        captured: bool,
    ) -> dict[str, object]:
        real_preview = bundle_module.build_extraction_shadow_preview
        authorization = _remote_authorization(self.source_sha256)
        authorized_runtime = mock.Mock()
        authorized_runtime.runtime = object()

        def remote_preview(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            native_kwargs = dict(kwargs)
            native_kwargs["visual_provider"] = "off"
            native_kwargs["visual_runtime"] = None
            native_kwargs["visual_authorization"] = None
            result = copy.deepcopy(real_preview(*args, **native_kwargs))
            result["visual_provider"] = "bailian"
            result["ocr"] = (
                _remote_capture(authorization)
                if captured
                else _empty_remote_capture(authorization)
            )
            if captured:
                _attach_remote_page(result, authorization)
            return result

        with (
            mock.patch.object(
                bundle_module,
                "build_authorized_pdf_visual_runtime",
                return_value=authorized_runtime,
            ),
            mock.patch.object(
                bundle_module,
                "visual_runtime_audit",
                return_value=authorization,
            ),
            mock.patch.object(
                bundle_module,
                "build_extraction_shadow_preview",
                side_effect=remote_preview,
            ),
        ):
            return export_pdf_visual_review_bundle(
                self.source,
                document_id="DOC-SYNTHETIC-3P",
                expected_source_sha256=self.source_sha256,
                page_start=1,
                page_end=3,
                output_dir=destination,
                corpus_root=self.corpus_root,
                visual_provider="bailian",
            )

    def _write_rehashed_manifest(
        self,
        destination: Path,
        manifest: dict[str, Any],
    ) -> None:
        manifest["bundle_sha256"] = canonical_bundle_sha256(manifest)
        (destination / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True),
            encoding="utf-8",
        )

    def _rewrite_page_artifact(
        self,
        destination: Path,
        manifest: dict[str, Any],
        *,
        page_index: int,
        mutate: Callable[[dict[str, Any], dict[str, Any]], None],
        synchronize_manifest_coverage: bool = False,
    ) -> dict[str, Any]:
        tampered = copy.deepcopy(manifest)
        page_record = tampered["pages"][page_index]["artifact"]
        page_path = destination / page_record["path"]
        page_dto = json.loads(page_path.read_text(encoding="utf-8"))
        mutate(page_dto, tampered)
        page_bytes = (
            json.dumps(
                page_dto,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        page_path.write_bytes(page_bytes)
        page_record["sha256"] = hashlib.sha256(page_bytes).hexdigest()
        if synchronize_manifest_coverage:
            tampered["pages"][page_index]["coverage"] = copy.deepcopy(
                page_dto["coverage"]
            )
        self._write_rehashed_manifest(destination, tampered)
        return tampered

    def _assert_bundle_rejected(
        self,
        destination: Path,
        *,
        expected_code: str,
    ) -> None:
        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            inspect_pdf_visual_review_bundle(destination)
        self.assertEqual(context.exception.code, expected_code)

    def test_exports_exact_pages_hashes_and_offline_customer_content_bundle(
        self,
    ) -> None:
        destination = self.output_root / "full-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )

        self.assertEqual(
            manifest["schema_version"],
            PDF_VISUAL_REVIEW_BUNDLE_SCHEMA_VERSION,
        )
        self.assertEqual(manifest["scope"]["page_numbers"], [1, 2, 3])
        self.assertTrue(manifest["scope"]["full_document"])
        self.assertEqual(
            manifest["coverage"]["page_set"]["preview_document_pages"],
            [1, 2, 3],
        )
        self.assertTrue(manifest["coverage"]["page_set"]["preview_complete"])
        self.assertTrue(manifest["coverage"]["page_set"]["preview_ordered"])
        self.assertEqual(manifest["pipeline"]["visual_provider"], "off")
        self.assertEqual(manifest["pipeline"]["collection"], "document_outline")
        self.assertEqual(
            manifest["pipeline"]["preview"]["schema_version"],
            SHADOW_PREVIEW_SCHEMA_VERSION,
        )
        self.assertEqual(
            manifest["pipeline"]["page_comparison"]["schema_version"],
            PAGE_COMPARISON_SCHEMA_VERSION,
        )
        self.assertEqual(
            manifest["bundle_sha256"],
            canonical_bundle_sha256(manifest),
        )
        self.assertTrue(manifest["privacy"]["contains_customer_content"])
        self.assertTrue(manifest["privacy"]["contains_customer_text"])
        self.assertTrue(manifest["privacy"]["contains_source_page_images"])
        self.assertFalse(manifest["privacy"]["contains_source_path"])
        self.assertFalse(manifest["privacy"]["is_anonymized"])
        self.assertEqual(
            manifest["review_provenance"]["approval_status"],
            "NOT_APPROVED",
        )
        self.assertFalse(manifest["review_provenance"]["is_human_gold"])
        self.assertFalse(manifest["review_provenance"]["is_gold"])
        self.assertFalse(manifest["review_provenance"]["is_formal_approval"])
        self.assertFalse(manifest["review_provenance"]["is_serving_activation_gate"])
        self.assertEqual(
            manifest["coverage"]["document"]["text_authority"],
            {
                "visual_page_count": 0,
                "ready_page_count": 0,
                "degraded_page_numbers": [],
            },
        )

        stored_manifest = json.loads(
            (destination / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(stored_manifest, manifest)
        for expected_page, page_entry in enumerate(manifest["pages"], start=1):
            self.assertEqual(page_entry["page_number"], expected_page)
            for artifact_key in ("artifact", "render", "annotated"):
                artifact = page_entry[artifact_key]
                artifact_path = destination / artifact["path"]
                self.assertTrue(artifact_path.is_file())
                self.assertEqual(_sha256(artifact_path), artifact["sha256"])
            page_dto = json.loads(
                (destination / page_entry["artifact"]["path"]).read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(page_dto["page_number"], expected_page)
            self.assertEqual(
                page_dto["coverage"]["text_authority"],
                {"channel": "NATIVE", "status": "READY", "reason_codes": []},
            )
            self.assertGreater(page_entry["annotated"]["bbox_type_label_count"], 0)
            self.assertNotEqual(
                page_entry["render"]["sha256"],
                page_entry["annotated"]["sha256"],
            )
            with Image.open(destination / page_entry["render"]["path"]) as image:
                self.assertEqual(
                    image.size,
                    (
                        page_entry["render"]["width_px"],
                        page_entry["render"]["height_px"],
                    ),
                )

        index_text = (destination / "index.html").read_text(encoding="utf-8")
        self.assertNotRegex(
            index_text,
            re.compile(r"\b(?:https?|ftp|file|data):", re.IGNORECASE),
        )
        self.assertNotRegex(
            index_text,
            re.compile(r"""(?:src|href)\s*=\s*["']\s*//""", re.IGNORECASE),
        )
        self.assertIn("CUSTOMER CONTENT", index_text)
        self.assertIn("NOT Human Gold", index_text)
        self.assertIn("NOT a formal approval", index_text)
        self.assertIn("renders/page-0001.png", index_text)
        self.assertIn("annotated/page-0003.png", index_text)
        self.assertNotIn(str(self.source), index_text)
        self.assertNotIn(self.source.name, index_text)
        self.assertEqual(
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
                require_full_document=True,
            ),
            manifest,
        )
        self.assertEqual(
            {path.name for path in self.output_root.iterdir()},
            {"full-review"},
        )

    def test_exports_an_exact_contiguous_subrange(self) -> None:
        destination = self.output_root / "partial-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=2,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )

        self.assertEqual(manifest["scope"]["page_numbers"], [2, 3])
        self.assertFalse(manifest["scope"]["full_document"])
        self.assertIn("PARTIAL_DOCUMENT_PAGE_RANGE", manifest["risk_flags"])
        self.assertEqual(
            [entry["page_number"] for entry in manifest["pages"]],
            [2, 3],
        )
        self.assertFalse((destination / "pages/page-0001.json").exists())
        index_text = (destination / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("renders/page-0001.png", index_text)
        self.assertIn("renders/page-0002.png", index_text)
        self.assertEqual(
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
            ),
            manifest,
        )
        with self.assertRaisesRegex(
            PdfVisualReviewBundleRejected,
            "bundle_full_document_required",
        ):
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
                require_full_document=True,
            )

    def test_rejects_hash_drift_after_preview_without_publishing(self) -> None:
        destination = self.output_root / "drifted-review"
        real_preview = bundle_module.build_extraction_shadow_preview

        def build_then_drift(*args: object, **kwargs: object) -> dict[str, object]:
            result = real_preview(*args, **kwargs)
            self.source.write_bytes(self.source.read_bytes() + b"\n")
            return result

        with mock.patch.object(
            bundle_module,
            "build_extraction_shadow_preview",
            side_effect=build_then_drift,
        ):
            with self.assertRaisesRegex(
                PdfVisualReviewBundleRejected,
                "bundle_source_hash_drift",
            ):
                export_pdf_visual_review_bundle(
                    self.source,
                    document_id="DOC-SYNTHETIC-3P",
                    expected_source_sha256=self.source_sha256,
                    page_start=1,
                    page_end=3,
                    output_dir=destination,
                    corpus_root=self.corpus_root,
                )

        self.assertFalse(destination.exists())
        self.assertFalse(self.output_root.exists())

    def test_rejects_incomplete_or_out_of_order_preview_page_set(self) -> None:
        destination = self.output_root / "unordered-review"
        real_preview = bundle_module.build_extraction_shadow_preview

        def unordered_preview(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            result = copy.deepcopy(real_preview(*args, **kwargs))
            pages = result["page_comparison"]["pages"]
            pages[0], pages[1] = pages[1], pages[0]
            return result

        with mock.patch.object(
            bundle_module,
            "build_extraction_shadow_preview",
            side_effect=unordered_preview,
        ):
            with self.assertRaisesRegex(
                PdfVisualReviewBundleRejected,
                "bundle_preview_page_set_invalid",
            ):
                export_pdf_visual_review_bundle(
                    self.source,
                    document_id="DOC-SYNTHETIC-3P",
                    expected_source_sha256=self.source_sha256,
                    page_start=1,
                    page_end=3,
                    output_dir=destination,
                    corpus_root=self.corpus_root,
                )
        self.assertFalse(destination.exists())

    def test_remote_export_binds_exact_route_provider_and_capture(self) -> None:
        destination = self.output_root / "remote-review"
        real_preview = bundle_module.build_extraction_shadow_preview
        authorization = _remote_authorization(self.source_sha256)
        authorized_runtime = mock.Mock()
        authorized_runtime.runtime = object()

        def remote_preview(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            self.assertEqual(kwargs["visual_provider"], "bailian")
            self.assertIs(kwargs["visual_runtime"], authorized_runtime.runtime)
            self.assertEqual(kwargs["visual_authorization"], authorization)
            native_kwargs = dict(kwargs)
            native_kwargs["visual_provider"] = "off"
            native_kwargs["visual_runtime"] = None
            native_kwargs["visual_authorization"] = None
            result = copy.deepcopy(real_preview(*args, **native_kwargs))
            result["visual_provider"] = "bailian"
            result["ocr"] = _remote_capture(authorization)
            _attach_remote_page(result, authorization)
            return result

        registry_path = self.root / "controlled-silver.json"
        with (
            mock.patch.object(
                bundle_module,
                "build_authorized_pdf_visual_runtime",
                return_value=authorized_runtime,
            ) as runtime_builder,
            mock.patch.object(
                bundle_module,
                "visual_runtime_audit",
                return_value=authorization,
            ),
            mock.patch.object(
                bundle_module,
                "build_extraction_shadow_preview",
                side_effect=remote_preview,
            ),
        ):
            manifest = export_pdf_visual_review_bundle(
                self.source,
                document_id="DOC-SYNTHETIC-3P",
                expected_source_sha256=self.source_sha256,
                page_start=1,
                page_end=3,
                output_dir=destination,
                corpus_root=self.corpus_root,
                visual_provider="bailian",
                visual_registry_path=registry_path,
            )

        runtime_builder.assert_called_once_with(
            source=self.source.resolve(),
            source_sha256=self.source_sha256,
            provider_name="bailian",
            registry_path=registry_path,
        )
        self.assertEqual(manifest["pipeline"]["visual_provider"], "bailian")
        self.assertEqual(
            manifest["pipeline"]["visual_authorization"],
            authorization,
        )
        self.assertEqual(
            manifest["pipeline"]["visual_capture"]["captured_pages"],
            [2],
        )
        self.assertEqual(
            manifest["coverage"]["document"]["text_authority"],
            {
                "visual_page_count": 1,
                "ready_page_count": 1,
                "degraded_page_numbers": [],
            },
        )
        self.assertEqual(
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
                require_full_document=True,
            ),
            manifest,
        )
        page_record = manifest["pages"][1]["artifact"]
        page_path = destination / page_record["path"]
        page_dto = json.loads(page_path.read_text(encoding="utf-8"))
        remote_block = next(
            block
            for block in page_dto["blocks"]
            if block["provenance"].get("channel") == "REMOTE_VISUAL"
        )
        self.assertEqual(
            page_dto["coverage"]["text_authority"],
            {
                "channel": "REMOTE_VISUAL",
                "status": "READY",
                "reason_codes": [],
            },
        )
        remote_block["provenance"]["provider"]["version"] = "forged-version"
        page_bytes = (
            json.dumps(
                page_dto,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        page_path.write_bytes(page_bytes)
        tampered = copy.deepcopy(manifest)
        tampered["pages"][1]["artifact"]["sha256"] = hashlib.sha256(
            page_bytes
        ).hexdigest()
        tampered["bundle_sha256"] = canonical_bundle_sha256(tampered)
        (destination / "manifest.json").write_text(
            json.dumps(tampered, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            inspect_pdf_visual_review_bundle(destination)
        self.assertEqual(
            context.exception.code,
            "bundle_page_visual_provenance_invalid",
        )

    def test_remote_verifier_rejects_rehashed_capture_route_tampering(self) -> None:
        destination = self.output_root / "remote-tamper-review"
        real_preview = bundle_module.build_extraction_shadow_preview
        authorization = _remote_authorization(self.source_sha256)
        authorized_runtime = mock.Mock()
        authorized_runtime.runtime = object()

        def remote_preview(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            native_kwargs = dict(kwargs)
            native_kwargs["visual_provider"] = "off"
            native_kwargs["visual_runtime"] = None
            native_kwargs["visual_authorization"] = None
            result = copy.deepcopy(real_preview(*args, **native_kwargs))
            result["visual_provider"] = "bailian"
            result["ocr"] = _remote_capture(authorization)
            _attach_remote_page(result, authorization)
            return result

        with (
            mock.patch.object(
                bundle_module,
                "build_authorized_pdf_visual_runtime",
                return_value=authorized_runtime,
            ),
            mock.patch.object(
                bundle_module,
                "visual_runtime_audit",
                return_value=authorization,
            ),
            mock.patch.object(
                bundle_module,
                "build_extraction_shadow_preview",
                side_effect=remote_preview,
            ),
        ):
            manifest = export_pdf_visual_review_bundle(
                self.source,
                document_id="DOC-SYNTHETIC-3P",
                expected_source_sha256=self.source_sha256,
                page_start=1,
                page_end=3,
                output_dir=destination,
                corpus_root=self.corpus_root,
                visual_provider="bailian",
            )

        tampered = copy.deepcopy(manifest)
        tampered["pipeline"]["visual_capture"]["captured_pages"] = [1]
        tampered["bundle_sha256"] = canonical_bundle_sha256(tampered)
        (destination / "manifest.json").write_text(
            json.dumps(tampered, sort_keys=True),
            encoding="utf-8",
        )
        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            inspect_pdf_visual_review_bundle(destination)
        self.assertEqual(context.exception.code, "bundle_visual_capture_invalid")

    def test_remote_export_allows_authorized_pages_with_empty_capture_subset(
        self,
    ) -> None:
        destination = self.output_root / "remote-empty-capture-review"

        manifest = self._export_remote_fixture(destination, captured=False)

        capture = manifest["pipeline"]["visual_capture"]
        self.assertEqual(capture["authorized_page_count"], 2)
        self.assertEqual(capture["captured_pages"], [])
        self.assertEqual(capture["snapshot_count"], 0)
        self.assertEqual(capture["evaluated_content_types"], [])
        self.assertEqual(
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
                require_full_document=True,
            ),
            manifest,
        )

    def test_remote_verifier_rejects_rehashed_page_coverage_semantic_tampering(
        self,
    ) -> None:
        destination = self.output_root / "remote-coverage-tamper-review"
        manifest = self._export_remote_fixture(destination, captured=True)
        page_record = manifest["pages"][1]["artifact"]
        page_path = destination / page_record["path"]
        page_dto = json.loads(page_path.read_text(encoding="utf-8"))
        page_dto["coverage"]["remote_text"]["status"] = "NOT_EVALUATED"
        page_dto["coverage"]["remote_visual"]["full_layout_evaluated"] = True
        page_bytes = (
            json.dumps(
                page_dto,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        page_path.write_bytes(page_bytes)
        tampered = copy.deepcopy(manifest)
        tampered["pages"][1]["artifact"]["sha256"] = hashlib.sha256(
            page_bytes
        ).hexdigest()
        tampered["bundle_sha256"] = canonical_bundle_sha256(tampered)
        (destination / "manifest.json").write_text(
            json.dumps(tampered, sort_keys=True),
            encoding="utf-8",
        )

        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            inspect_pdf_visual_review_bundle(destination)

        self.assertEqual(
            context.exception.code,
            "bundle_page_visual_provenance_invalid",
        )

    def test_remote_verifier_recomputes_page_text_authority(self) -> None:
        destination = self.output_root / "remote-authority-tamper-review"
        manifest = self._export_remote_fixture(destination, captured=True)

        def forge_authority(
            page_dto: dict[str, Any],
            tampered: dict[str, Any],
        ) -> None:
            page_dto["coverage"]["text_authority"]["status"] = "PENDING"
            tampered["coverage"]["document"]["text_authority"] = {
                "visual_page_count": 1,
                "ready_page_count": 0,
                "degraded_page_numbers": [2],
            }

        self._rewrite_page_artifact(
            destination,
            manifest,
            page_index=1,
            mutate=forge_authority,
            synchronize_manifest_coverage=True,
        )
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_remote_verifier_recomputes_document_text_authority(self) -> None:
        destination = self.output_root / "remote-document-authority-tamper-review"
        manifest = self._export_remote_fixture(destination, captured=True)
        tampered = copy.deepcopy(manifest)
        tampered["coverage"]["document"]["text_authority"] = {
            "visual_page_count": 1,
            "ready_page_count": 0,
            "degraded_page_numbers": [2],
        }
        self._write_rehashed_manifest(destination, tampered)
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_remote_verifier_rejects_rehashed_capture_contract_tampering(
        self,
    ) -> None:
        mutations = (
            (
                "capability",
                "bundle_visual_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"].__setitem__(
                    "capability",
                    "PAGE_LAYOUT",
                ),
            ),
            (
                "coordinate-space",
                "bundle_visual_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"][
                    "coordinate_mapping"
                ].__setitem__("target_space", "FORGED_SPACE"),
            ),
            (
                "coherent-confidence",
                "bundle_page_visual_provenance_invalid",
                lambda payload: payload["pipeline"]["visual_capture"].__setitem__(
                    "confidence",
                    {
                        "available": 1,
                        "unavailable": 0,
                        "minimum": 0.8,
                        "maximum": 0.8,
                        "mean": 0.8,
                    },
                ),
            ),
            (
                "boolean-snapshot-count",
                "bundle_visual_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"].__setitem__(
                    "snapshot_count",
                    True,
                ),
            ),
            (
                "boolean-observation-count",
                "bundle_visual_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"].__setitem__(
                    "observation_count",
                    True,
                ),
            ),
            (
                "unauthorized-route-provider",
                "bundle_visual_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"]["route"][
                    "allowed_provider_ids"
                ].append("forged-provider"),
            ),
            (
                "forged-visual-table-provider",
                "bundle_visual_table_capture_invalid",
                lambda payload: payload["pipeline"]["visual_capture"].__setitem__(
                    "visual_table",
                    {
                        "mode": "REMOTE_VISUAL",
                        "provider": {
                            "id": "forged-provider",
                            "version": "forged-version",
                        },
                        "authorized_pages": [2],
                        "snapshot_count": 1,
                    },
                ),
            ),
        )
        for name, expected_code, mutate in mutations:
            with self.subTest(name=name):
                destination = self.output_root / f"capture-tamper-{name}"
                manifest = self._export_remote_fixture(destination, captured=True)
                tampered = copy.deepcopy(manifest)
                mutate(tampered)
                self._write_rehashed_manifest(destination, tampered)
                self._assert_bundle_rejected(
                    destination,
                    expected_code=expected_code,
                )

    def test_remote_verifier_rejects_rehashed_block_provenance_tampering(
        self,
    ) -> None:
        mutations = (
            (
                "fact-kind",
                lambda block: block["provenance"].__setitem__(
                    "fact_kind",
                    "SOURCE_NODE",
                ),
            ),
            (
                "adapter",
                lambda block: block["provenance"].__setitem__(
                    "adapter",
                    {"id": "forged-adapter", "version": "forged-version"},
                ),
            ),
            (
                "confidence",
                lambda block: block["provenance"].__setitem__("confidence", 0.8),
            ),
            (
                "coordinate-mapping",
                lambda block: block["provenance"]["coordinate_mapping"].__setitem__(
                    "status",
                    "FORGED",
                ),
            ),
            (
                "page-image-binding",
                lambda block: block["provenance"].__setitem__(
                    "page_image_sha256",
                    "3" * 64,
                ),
            ),
            (
                "provider",
                lambda block: block["provenance"].__setitem__(
                    "provider",
                    {"id": "forged-provider", "version": "fixture-v1"},
                ),
            ),
            (
                "source-ref",
                lambda block: block["source_ref"].__setitem__(
                    "kind",
                    "source_node",
                ),
            ),
            (
                "source-kind",
                lambda block: block.__setitem__("source_kind", "paragraph"),
            ),
            (
                "locatable",
                lambda block: (
                    block.__setitem__("locatable", False),
                    block.__setitem__("bbox", None),
                ),
            ),
            (
                "location-status",
                lambda block: block.__setitem__(
                    "location_status",
                    "BBOX_INVALID",
                ),
            ),
            (
                "bbox-source-drift",
                lambda block: block.__setitem__(
                    "bbox",
                    [
                        block["bbox"][0] + 1,
                        block["bbox"][1],
                        block["bbox"][2],
                        block["bbox"][3],
                    ],
                ),
            ),
            (
                "source-location-bbox-drift",
                lambda block: block["source_location"].__setitem__(
                    "bbox",
                    [
                        block["source_location"]["bbox"][0] + 1,
                        block["source_location"]["bbox"][1],
                        block["source_location"]["bbox"][2],
                        block["source_location"]["bbox"][3],
                    ],
                ),
            ),
            (
                "type-not-evaluated",
                lambda block: block.__setitem__("type", "TABLE"),
            ),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                destination = self.output_root / f"block-tamper-{name}"
                manifest = self._export_remote_fixture(destination, captured=True)

                def mutate_page(
                    page_dto: dict[str, Any],
                    _manifest: dict[str, Any],
                ) -> None:
                    mutate(_remote_visual_block(page_dto))

                self._rewrite_page_artifact(
                    destination,
                    manifest,
                    page_index=1,
                    mutate=mutate_page,
                )
                self._assert_bundle_rejected(
                    destination,
                    expected_code="bundle_page_visual_provenance_invalid",
                )

    def test_remote_verifier_rejects_channel_relabeling_with_remote_identity(
        self,
    ) -> None:
        destination = self.output_root / "remote-channel-laundering"
        manifest = self._export_remote_fixture(destination, captured=True)

        def launder(
            page_dto: dict[str, Any],
            tampered: dict[str, Any],
        ) -> None:
            _launder_remote_block_as_native(
                page_dto,
                tampered,
                source_sha256=self.source_sha256,
                preserve_remote_identity=True,
            )

        self._rewrite_page_artifact(
            destination,
            manifest,
            page_index=1,
            mutate=launder,
            synchronize_manifest_coverage=True,
        )
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_verifier_rejects_rehashed_page_counts_tampering(self) -> None:
        destination = self.output_root / "page-counts-tamper"
        manifest = self._export_remote_fixture(destination, captured=True)

        def mutate_counts(
            page_dto: dict[str, Any],
            _manifest: dict[str, Any],
        ) -> None:
            page_dto["counts"]["by_channel"]["REMOTE_VISUAL"] = True

        self._rewrite_page_artifact(
            destination,
            manifest,
            page_index=1,
            mutate=mutate_counts,
        )
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_off_verifier_rejects_rehashed_remote_coverage_forgery(self) -> None:
        destination = self.output_root / "off-remote-coverage-forgery"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        tampered = copy.deepcopy(manifest)
        tampered["coverage"]["document"]["remote_text"] = {
            "pages_evaluated": 1,
            "pages_with_observations": 1,
            "observations": 1,
            "total_pages": 3,
        }
        tampered["coverage"]["document"]["remote_visual"] = {
            "pages_evaluated": 1,
            "full_layout_pages": 0,
            "evaluated_types": ["TEXT"],
        }
        self._write_rehashed_manifest(destination, tampered)
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_verifier_rejects_extra_and_non_regular_inventory_nodes(self) -> None:
        def add_extra_file(destination: Path, _manifest: dict[str, Any]) -> None:
            (destination / "rogue.txt").write_text("extra", encoding="utf-8")

        def add_extra_directory(
            destination: Path,
            _manifest: dict[str, Any],
        ) -> None:
            (destination / "rogue-directory").mkdir()

        def replace_artifact_with_symlink(
            destination: Path,
            manifest: dict[str, Any],
        ) -> None:
            render_path = destination / manifest["pages"][0]["render"]["path"]
            render_path.unlink()
            render_path.symlink_to(
                destination / manifest["pages"][0]["annotated"]["path"]
            )

        def replace_component_with_symlink(
            destination: Path,
            _manifest: dict[str, Any],
        ) -> None:
            pages = destination / "pages"
            external_pages = destination.parent / f"{destination.name}-external-pages"
            pages.rename(external_pages)
            pages.symlink_to(external_pages, target_is_directory=True)

        def add_fifo(destination: Path, _manifest: dict[str, Any]) -> None:
            os.mkfifo(destination / "rogue.fifo")

        mutations = (
            ("extra-file", add_extra_file),
            ("extra-directory", add_extra_directory),
            ("artifact-symlink", replace_artifact_with_symlink),
            ("component-symlink", replace_component_with_symlink),
            ("special-node", add_fifo),
        )
        for name, mutate in mutations:
            with self.subTest(name=name):
                destination = self.output_root / f"inventory-{name}"
                manifest = export_pdf_visual_review_bundle(
                    self.source,
                    document_id="DOC-SYNTHETIC-3P",
                    expected_source_sha256=self.source_sha256,
                    page_start=1,
                    page_end=3,
                    output_dir=destination,
                    corpus_root=self.corpus_root,
                )
                mutate(destination, manifest)
                self._assert_bundle_rejected(
                    destination,
                    expected_code="bundle_inventory_invalid",
                )

    def test_renderer_and_png_artifacts_are_structurally_verified(self) -> None:
        mutations: tuple[
            tuple[
                str,
                str,
                Callable[[Path, dict[str, Any]], None],
            ],
            ...,
        ] = (
            (
                "renderer-schema",
                "bundle_renderer_contract_invalid",
                lambda _destination, manifest: manifest.__setitem__(
                    "renderer",
                    {},
                ),
            ),
            (
                "declared-render-width",
                "bundle_render_dimensions_invalid",
                lambda _destination, manifest: manifest["pages"][0][
                    "render"
                ].__setitem__(
                    "width_px",
                    manifest["pages"][0]["render"]["width_px"] + 1,
                ),
            ),
            (
                "renderer-scale-geometry",
                "bundle_render_geometry_invalid",
                lambda _destination, manifest: manifest["renderer"].__setitem__(
                    "scale",
                    2.25,
                ),
            ),
        )
        for name, expected_code, mutate in mutations:
            with self.subTest(name=name):
                destination = self.output_root / f"renderer-{name}"
                manifest = self._export_remote_fixture(
                    destination,
                    captured=True,
                )
                tampered = copy.deepcopy(manifest)
                mutate(destination, tampered)
                self._write_rehashed_manifest(destination, tampered)
                self._assert_bundle_rejected(
                    destination,
                    expected_code=expected_code,
                )

        non_png_destination = self.output_root / "renderer-non-png"
        non_png_manifest = self._export_remote_fixture(
            non_png_destination,
            captured=True,
        )
        non_png_tampered = copy.deepcopy(non_png_manifest)
        render_record = non_png_tampered["pages"][0]["render"]
        render_path = non_png_destination / render_record["path"]
        non_png_payload = b"not a PNG"
        render_path.write_bytes(non_png_payload)
        render_record["sha256"] = hashlib.sha256(non_png_payload).hexdigest()
        self._write_rehashed_manifest(non_png_destination, non_png_tampered)
        self._assert_bundle_rejected(
            non_png_destination,
            expected_code="bundle_render_artifact_invalid",
        )

        annotation_destination = self.output_root / "renderer-annotation-size"
        annotation_manifest = self._export_remote_fixture(
            annotation_destination,
            captured=True,
        )
        annotation_tampered = copy.deepcopy(annotation_manifest)
        annotated_record = annotation_tampered["pages"][0]["annotated"]
        annotated_path = annotation_destination / annotated_record["path"]
        replacement = BytesIO()
        Image.new("RGB", (17, 19), "white").save(replacement, format="PNG")
        annotated_payload = replacement.getvalue()
        annotated_path.write_bytes(annotated_payload)
        annotated_record["sha256"] = hashlib.sha256(annotated_payload).hexdigest()
        self._write_rehashed_manifest(
            annotation_destination,
            annotation_tampered,
        )
        self._assert_bundle_rejected(
            annotation_destination,
            expected_code="bundle_annotated_dimensions_invalid",
        )

    def test_remote_verifier_binds_provider_name_to_canonical_id(self) -> None:
        destination = self.output_root / "canonical-provider-tamper"
        manifest = self._export_remote_fixture(destination, captured=True)
        forged_provider_id = "coherently-forged-provider"

        def forge_provider(
            page_dto: dict[str, Any],
            tampered: dict[str, Any],
        ) -> None:
            authorization = tampered["pipeline"]["visual_authorization"]
            authorization["selected_provider"]["id"] = forged_provider_id
            selected_capability = next(
                provider
                for provider in authorization["providers"]
                if provider["name"] == "bailian"
            )
            selected_capability["id"] = forged_provider_id
            capture = tampered["pipeline"]["visual_capture"]
            capture["provider"]["id"] = forged_provider_id
            capture["route"]["allowed_provider_ids"] = [forged_provider_id]
            _remote_visual_block(page_dto)["provenance"]["provider"]["id"] = (
                forged_provider_id
            )

        self._rewrite_page_artifact(
            destination,
            manifest,
            page_index=1,
            mutate=forge_provider,
        )
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_visual_authorization_invalid",
        )

    def test_remote_verifier_binds_provider_name_to_capture_capability(
        self,
    ) -> None:
        destination = self.output_root / "provider-capability-tamper"
        manifest = self._export_remote_fixture(destination, captured=True)
        tampered = copy.deepcopy(manifest)
        layout_provider_name = "bailian-qwen3.7-plus-layout"
        tampered["pipeline"]["visual_provider"] = layout_provider_name
        authorization = tampered["pipeline"]["visual_authorization"]
        authorization["selected_provider"]["name"] = layout_provider_name
        selected_capability = next(
            provider
            for provider in authorization["providers"]
            if provider["name"] == "bailian"
        )
        selected_capability["name"] = layout_provider_name
        tampered["pipeline"]["visual_capture"]["provider"]["name"] = (
            layout_provider_name
        )
        self._write_rehashed_manifest(destination, tampered)
        self._assert_bundle_rejected(
            destination,
            expected_code="bundle_visual_capture_invalid",
        )

        empty_destination = self.output_root / "provider-capability-empty"
        empty_manifest = self._export_remote_fixture(
            empty_destination,
            captured=False,
        )
        empty_layout = copy.deepcopy(empty_manifest)
        empty_layout["pipeline"]["visual_provider"] = layout_provider_name
        empty_authorization = empty_layout["pipeline"]["visual_authorization"]
        empty_authorization["selected_provider"]["name"] = layout_provider_name
        empty_selected_capability = next(
            provider
            for provider in empty_authorization["providers"]
            if provider["name"] == "bailian"
        )
        empty_selected_capability["name"] = layout_provider_name
        empty_layout["pipeline"]["visual_capture"]["provider"]["name"] = (
            layout_provider_name
        )
        self._write_rehashed_manifest(empty_destination, empty_layout)
        self.assertEqual(
            inspect_pdf_visual_review_bundle(empty_destination),
            empty_layout,
        )

    def test_source_asset_route_binds_exact_page_native_asset_inventory(
        self,
    ) -> None:
        matching_destination = self.output_root / "source-asset-route-matching"
        matching_manifest = self._export_remote_fixture(
            matching_destination,
            captured=True,
        )
        matching_page_record = matching_manifest["pages"][1]["artifact"]
        matching_page = json.loads(
            (matching_destination / matching_page_record["path"]).read_text(
                encoding="utf-8"
            )
        )
        native_asset_sha256 = next(
            block["asset"]["content_sha256"]
            for block in matching_page["blocks"]
            if block["provenance"]["channel"] == "NATIVE"
            and block["source_kind"] in {"image", "figure"}
            and isinstance(block["asset"], dict)
        )
        matching = copy.deepcopy(matching_manifest)
        _set_page_binding_to_source_asset(
            matching,
            page_number=2,
            source_asset_sha256=native_asset_sha256,
        )
        self._write_rehashed_manifest(matching_destination, matching)
        self.assertEqual(
            inspect_pdf_visual_review_bundle(matching_destination),
            matching,
        )

        mismatch_destination = self.output_root / "source-asset-route-mismatch"
        mismatch_manifest = self._export_remote_fixture(
            mismatch_destination,
            captured=True,
        )
        mismatched = copy.deepcopy(mismatch_manifest)
        _set_page_binding_to_source_asset(
            mismatched,
            page_number=2,
            source_asset_sha256="4" * 64,
        )
        self._write_rehashed_manifest(mismatch_destination, mismatched)
        self._assert_bundle_rejected(
            mismatch_destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

        uncaptured_destination = (
            self.output_root / "source-asset-route-uncaptured-mismatch"
        )
        uncaptured_manifest = self._export_remote_fixture(
            uncaptured_destination,
            captured=True,
        )
        uncaptured_tampered = copy.deepcopy(uncaptured_manifest)
        _set_page_binding_to_source_asset(
            uncaptured_tampered,
            page_number=1,
            source_asset_sha256="5" * 64,
        )
        self._write_rehashed_manifest(
            uncaptured_destination,
            uncaptured_tampered,
        )
        self._assert_bundle_rejected(
            uncaptured_destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_risk_flags_are_exactly_recomputed_for_remote_and_off_modes(
        self,
    ) -> None:
        off_destination = self.output_root / "risk-off"
        off_manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=off_destination,
            corpus_root=self.corpus_root,
        )
        self.assertIn("REMOTE_VISUAL_DISABLED", off_manifest["risk_flags"])

        remote_destination = self.output_root / "risk-remote"
        remote_manifest = self._export_remote_fixture(
            remote_destination,
            captured=True,
        )
        self.assertNotIn(
            "REMOTE_VISUAL_DISABLED",
            remote_manifest["risk_flags"],
        )

        page_risk_destination = self.output_root / "risk-page-tamper"
        page_risk_manifest = self._export_remote_fixture(
            page_risk_destination,
            captured=True,
        )
        page_risk_tampered = copy.deepcopy(page_risk_manifest)
        page_risk_tampered["pages"][1]["risk_flags"].append("ZZZ_FORGED")
        self._write_rehashed_manifest(
            page_risk_destination,
            page_risk_tampered,
        )
        self._assert_bundle_rejected(
            page_risk_destination,
            expected_code="bundle_risk_flags_invalid",
        )

        document_risk_destination = self.output_root / "risk-document-tamper"
        document_risk_manifest = self._export_remote_fixture(
            document_risk_destination,
            captured=True,
        )
        document_risk_tampered = copy.deepcopy(document_risk_manifest)
        document_risk_tampered["risk_flags"].append("REMOTE_VISUAL_DISABLED")
        document_risk_tampered["risk_flags"].sort()
        self._write_rehashed_manifest(
            document_risk_destination,
            document_risk_tampered,
        )
        self._assert_bundle_rejected(
            document_risk_destination,
            expected_code="bundle_risk_flags_invalid",
        )

        off_tampered = copy.deepcopy(off_manifest)
        off_tampered["risk_flags"].remove("REMOTE_VISUAL_DISABLED")
        self._write_rehashed_manifest(off_destination, off_tampered)
        self._assert_bundle_rejected(
            off_destination,
            expected_code="bundle_risk_flags_invalid",
        )

    def test_page_risk_flags_surface_fragmentation_and_page_spanning_tables(
        self,
    ) -> None:
        page = {
            "bounds": [0.0, 0.0, 100.0, 100.0],
            "blocks": [
                {
                    "type": "TABLE",
                    "locatable": True,
                    "bbox": [0.0, 0.0, 80.0, 100.0],
                }
            ],
            "counts": {
                "content_blocks": 129,
                "unlocatable": 0,
                "issues": 0,
                "by_type": {"FIGURE": 128, "TABLE": 1},
            },
            "coverage": {
                "page_raster": {"present": False},
                "content_types": [],
            },
        }
        self.assertEqual(
            bundle_module._page_risk_flags(
                page,
                source_ir_status="COMPLETE",
            ),
            [
                "EXCESSIVE_FIGURE_FRAGMENTATION",
                "PAGE_SPANNING_TABLE_REVIEW_REQUIRED",
            ],
        )

        below_threshold = copy.deepcopy(page)
        below_threshold["blocks"][0]["bbox"] = [0.0, 0.0, 79.99, 100.0]
        below_threshold["counts"]["by_type"]["FIGURE"] = 127
        self.assertEqual(
            bundle_module._page_risk_flags(
                below_threshold,
                source_ir_status="COMPLETE",
            ),
            [],
        )

        unlocated_table = copy.deepcopy(page)
        unlocated_table["blocks"][0]["locatable"] = False
        self.assertEqual(
            bundle_module._page_risk_flags(
                unlocated_table,
                source_ir_status="COMPLETE",
            ),
            ["EXCESSIVE_FIGURE_FRAGMENTATION"],
        )

    def test_structure_only_inspection_never_claims_authenticity(self) -> None:
        destination = self.output_root / "structure-only-inspection"
        manifest = self._export_remote_fixture(
            destination,
            captured=True,
        )
        original_pin = manifest["bundle_sha256"]

        def coherent_rewrite(
            page_dto: dict[str, Any],
            tampered: dict[str, Any],
        ) -> None:
            _launder_remote_block_as_native(
                page_dto,
                tampered,
                source_sha256=self.source_sha256,
                preserve_remote_identity=False,
            )

        coherently_rewritten = self._rewrite_page_artifact(
            destination,
            manifest,
            page_index=1,
            mutate=coherent_rewrite,
            synchronize_manifest_coverage=True,
        )

        inspection = inspect_pdf_visual_review_bundle_structure_only(
            destination,
            require_full_document=True,
        )
        self.assertEqual(
            inspection,
            {
                "status": STRUCTURE_ONLY_INSPECTION_STATUS,
                "authenticity_established": False,
                "external_pin_required_for_authenticity": True,
                "validation_scope": {
                    "renderer": {
                        "status": "SELF_CONSISTENCY_VERIFIED",
                        "basis": ("EXACT_RENDERER_SCHEMA_AND_DECODED_PNG_GEOMETRY"),
                        "authenticity_established": False,
                    },
                    "visual_table_summary": {
                        "status": "NOT_PRESENT",
                        "basis": "VISUAL_TABLE_MODE_OFF",
                        "authenticity_established": False,
                    },
                    "document_coverage": {
                        "status": "FULL_DOCUMENT_RECOMPUTED",
                        "basis": "ALL_PAGE_ARTIFACTS",
                        "authenticity_established": False,
                    },
                },
                "manifest": coherently_rewritten,
            },
        )
        self.assertEqual(
            inspect_pdf_visual_review_bundle(
                destination,
                require_full_document=True,
            ),
            coherently_rewritten,
        )
        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=original_pin,
                require_full_document=True,
            )
        self.assertEqual(
            context.exception.code,
            "bundle_expected_sha256_mismatch",
        )

    def test_visual_table_summary_is_an_external_pin_exporter_claim(self) -> None:
        destination = self.output_root / "visual-table-exporter-claim"
        manifest = self._export_remote_fixture(destination, captured=True)
        original_pin = manifest["bundle_sha256"]
        claimed = copy.deepcopy(manifest)
        capture = claimed["pipeline"]["visual_capture"]
        capture["visual_table"] = {
            "mode": "REMOTE_VISUAL",
            "provider": {
                "id": capture["provider"]["id"],
                "version": "exporter-claim-version",
            },
            "authorized_pages": [2],
            "snapshot_count": 999,
        }
        self._write_rehashed_manifest(destination, claimed)

        inspection = inspect_pdf_visual_review_bundle_structure_only(
            destination,
            require_full_document=True,
        )

        self.assertEqual(
            inspection["validation_scope"]["visual_table_summary"],
            {
                "status": "EXPORTER_CLAIM_ONLY_REQUIRES_EXTERNAL_PIN",
                "basis": (
                    "BUNDLE_V1_HAS_NO_PAGE_LEVEL_VISUAL_TABLE_SNAPSHOT_INVENTORY"
                ),
                "authenticity_established": False,
            },
        )
        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=original_pin,
                require_full_document=True,
            )
        self.assertEqual(
            context.exception.code,
            "bundle_expected_sha256_mismatch",
        )

    def test_partial_document_coverage_validates_only_derivable_subset(
        self,
    ) -> None:
        claim_destination = self.output_root / "partial-coverage-claim"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=2,
            page_end=3,
            output_dir=claim_destination,
            corpus_root=self.corpus_root,
        )
        claimed = copy.deepcopy(manifest)
        claimed["coverage"]["document"]["content_types"][0]["observed_blocks"] += 999
        self._write_rehashed_manifest(claim_destination, claimed)

        inspection = inspect_pdf_visual_review_bundle_structure_only(
            claim_destination,
        )
        self.assertEqual(
            inspection["validation_scope"]["document_coverage"],
            {
                "status": (
                    "PARTIAL_DOCUMENT_DERIVABLE_SUBSET_ONLY_REQUIRES_EXTERNAL_PIN"
                ),
                "basis": "EXPORTED_PAGE_LOWER_BOUNDS_ONLY",
                "authenticity_established": False,
            },
        )

        invalid_destination = self.output_root / "partial-coverage-underflow"
        invalid_manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=2,
            page_end=3,
            output_dir=invalid_destination,
            corpus_root=self.corpus_root,
        )
        invalid = copy.deepcopy(invalid_manifest)
        text_coverage = next(
            entry
            for entry in invalid["coverage"]["document"]["content_types"]
            if entry["type"] == "TEXT"
        )
        text_coverage["observed_blocks"] = 0
        self._write_rehashed_manifest(invalid_destination, invalid)
        self._assert_bundle_rejected(
            invalid_destination,
            expected_code="bundle_page_visual_provenance_invalid",
        )

    def test_ai_pre_review_bundle_gate_uses_full_verify_and_external_pin(
        self,
    ) -> None:
        destination = self.output_root / "ai-pre-review-pin-gate"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        with mock.patch.object(
            ai_pre_review_module,
            "verify_pdf_visual_review_bundle",
            wraps=verify_pdf_visual_review_bundle,
        ) as verifier:
            self.assertEqual(
                ai_pre_review_module._verify_full_document_bundle(
                    destination,
                    expected_bundle_sha256=manifest["bundle_sha256"],
                ),
                manifest,
            )
        verifier.assert_called_once_with(
            destination,
            expected_bundle_sha256=manifest["bundle_sha256"],
            require_full_document=True,
        )

    def test_output_is_outside_corpus_and_never_overwrites(self) -> None:
        with self.assertRaises(CorpusPathError):
            export_pdf_visual_review_bundle(
                self.source,
                document_id="DOC-SYNTHETIC-3P",
                expected_source_sha256=self.source_sha256,
                page_start=1,
                page_end=3,
                output_dir=self.corpus_root / "review-bundle",
                corpus_root=self.corpus_root,
            )

        destination = self.output_root / "existing-review"
        destination.mkdir(parents=True)
        sentinel = destination / "keep.txt"
        sentinel.write_text("do not overwrite", encoding="utf-8")
        with self.assertRaisesRegex(
            PdfVisualReviewBundleRejected,
            "bundle_output_exists",
        ):
            export_pdf_visual_review_bundle(
                self.source,
                document_id="DOC-SYNTHETIC-3P",
                expected_source_sha256=self.source_sha256,
                page_start=1,
                page_end=3,
                output_dir=destination,
                corpus_root=self.corpus_root,
            )
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not overwrite")

    def test_remote_runtime_error_never_echoes_customer_content(self) -> None:
        customer_text = "客户原文：泵站压力必须达到七兆帕"
        with mock.patch.object(
            bundle_module,
            "build_authorized_pdf_visual_runtime",
            side_effect=RuntimeError(customer_text),
        ):
            with self.assertRaises(PdfVisualReviewBundleRejected) as context:
                export_pdf_visual_review_bundle(
                    self.source,
                    document_id="DOC-SYNTHETIC-3P",
                    expected_source_sha256=self.source_sha256,
                    page_start=1,
                    page_end=3,
                    output_dir=self.output_root / "runtime-error",
                    corpus_root=self.corpus_root,
                    visual_provider="bailian",
                )
        self.assertEqual(context.exception.code, "bundle_visual_runtime_rejected")
        self.assertEqual(context.exception.cause_code, "visual_runtime_invalid")
        self.assertNotIn(customer_text, str(context.exception))
        self.assertNotIn(customer_text, context.exception.cause_code)

    def test_verifier_detects_artifact_hash_tampering(self) -> None:
        destination = self.output_root / "tamper-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=1,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        render_path = destination / manifest["pages"][0]["render"]["path"]
        render_path.write_bytes(render_path.read_bytes() + b"tampered")

        with self.assertRaisesRegex(
            PdfVisualReviewBundleRejected,
            "bundle_artifact_sha256_mismatch",
        ):
            inspect_pdf_visual_review_bundle(destination)

    def test_verifier_rejects_rehashed_declaration_tampering(self) -> None:
        destination = self.output_root / "declaration-tamper-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        manifest_path = destination / "manifest.json"
        mutations = (
            (
                "bundle_manifest_contract_invalid",
                lambda payload: payload.__setitem__(
                    "bundle_kind",
                    "FORMAL_APPROVAL",
                ),
            ),
            (
                "bundle_review_provenance_invalid",
                lambda payload: payload["review_provenance"].__setitem__(
                    "approval_status",
                    "APPROVED",
                ),
            ),
            (
                "bundle_document_contract_invalid",
                lambda payload: payload["document"].__setitem__("format", "docx"),
            ),
            (
                "bundle_page_inventory_invalid",
                lambda payload: payload["scope"].__setitem__("page_count", 2),
            ),
            (
                "bundle_coverage_page_set_invalid",
                lambda payload: payload["coverage"]["page_set"].__setitem__(
                    "exported_pages",
                    [1, 2],
                ),
            ),
            (
                "bundle_artifact_inventory_invalid",
                lambda payload: payload["entrypoint"].__setitem__(
                    "offline",
                    False,
                ),
            ),
            (
                "bundle_privacy_contract_invalid",
                lambda payload: payload["privacy"].__setitem__(
                    "is_anonymized",
                    True,
                ),
            ),
        )
        for expected_code, mutate in mutations:
            with self.subTest(expected_code=expected_code):
                tampered = copy.deepcopy(manifest)
                mutate(tampered)
                tampered["bundle_sha256"] = canonical_bundle_sha256(tampered)
                manifest_path.write_text(
                    json.dumps(tampered, sort_keys=True),
                    encoding="utf-8",
                )
                with self.assertRaises(PdfVisualReviewBundleRejected) as context:
                    inspect_pdf_visual_review_bundle(destination)
                self.assertEqual(context.exception.code, expected_code)

    def test_verifier_rejects_expected_bundle_hash_mismatch(self) -> None:
        destination = self.output_root / "expected-hash-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )

        self.assertEqual(
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
            ),
            manifest,
        )
        with self.assertRaises(TypeError):
            verify_pdf_visual_review_bundle(destination)
        with self.assertRaisesRegex(
            PdfVisualReviewBundleRejected,
            "bundle_expected_sha256_mismatch",
        ):
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256="0" * 64,
            )

    def test_verifier_rejects_duplicate_manifest_keys_with_original_pin(
        self,
    ) -> None:
        destination = self.output_root / "duplicate-key-review"
        manifest = export_pdf_visual_review_bundle(
            self.source,
            document_id="DOC-SYNTHETIC-3P",
            expected_source_sha256=self.source_sha256,
            page_start=1,
            page_end=3,
            output_dir=destination,
            corpus_root=self.corpus_root,
        )
        manifest_path = destination / "manifest.json"
        original = manifest_path.read_text(encoding="utf-8")
        ambiguous = original.replace(
            "{\n",
            '{\n  "review_provenance": {"approval_status": "APPROVED"},\n',
            1,
        )
        manifest_path.write_text(ambiguous, encoding="utf-8")

        with self.assertRaises(PdfVisualReviewBundleRejected) as context:
            verify_pdf_visual_review_bundle(
                destination,
                expected_bundle_sha256=manifest["bundle_sha256"],
            )
        self.assertEqual(context.exception.code, "bundle_manifest_invalid")

    def test_annotation_keeps_subpixel_boxes_visible_at_low_scale(self) -> None:
        page_dto = {
            "bounds": [0.0, 0.0, 100.0, 100.0],
            "blocks": [
                {
                    "locatable": True,
                    "bbox": [99.94, 99.94, 99.99, 99.99],
                    "type": "TEXT",
                    "label": "TXT-001",
                }
            ],
        }
        image = Image.new("RGB", (100, 100), "white")
        buffer = BytesIO()
        image.save(buffer, format="PNG")

        annotated, count = _annotate_page_png(
            buffer.getvalue(),
            page_dto=page_dto,
            width=100,
            height=100,
        )

        self.assertEqual(count, 1)
        self.assertTrue(annotated.startswith(b"\x89PNG"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _remote_visual_block(page_dto: dict[str, Any]) -> dict[str, Any]:
    return next(
        block
        for block in page_dto["blocks"]
        if block["provenance"].get("channel") == "REMOTE_VISUAL"
    )


def _launder_remote_block_as_native(
    page_dto: dict[str, Any],
    manifest: dict[str, Any],
    *,
    source_sha256: str,
    preserve_remote_identity: bool,
) -> None:
    block = _remote_visual_block(page_dto)
    if not preserve_remote_identity:
        block["id"] = "node-" + ("b" * 32)
        block["source_ref"]["source_id"] = block["id"]
    block["source_ref"]["kind"] = "source_node"
    block["source_kind"] = "paragraph"
    block["classification_basis"] = "source_ir:paragraph"
    adapter = manifest["pipeline"]["adapter"]
    block["provenance"] = {
        "channel": "NATIVE",
        "fact_kind": "SOURCE_NODE",
        "source_sha256": source_sha256,
        "page_image_sha256": None,
        "adapter": {
            "id": adapter["id"],
            "version": adapter["version"],
        },
        "provider": None,
        "confidence": None,
        "coordinate_mapping": {
            "source_space": "PDF_NATIVE_PAGE",
            "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "status": "NATIVE_PAGE_MATRIX_APPLIED",
        },
    }
    page_dto["counts"]["by_channel"]["NATIVE"] += 1
    page_dto["counts"]["by_channel"].pop("REMOTE_VISUAL")
    text_coverage = next(
        entry
        for entry in page_dto["coverage"]["content_types"]
        if entry["type"] == "TEXT"
    )
    text_coverage["channels"] = ["NATIVE"]
    page_dto["coverage"]["remote_text"] = {
        "status": "NOT_EVALUATED",
        "evaluated": False,
        "observation_count": 0,
    }
    page_dto["coverage"]["remote_visual"] = {
        "evaluated_types": [],
        "full_layout_evaluated": False,
    }
    page_dto["coverage"]["text_authority"] = {
        "channel": "NATIVE",
        "status": "READY",
        "reason_codes": [],
    }
    authorization = manifest["pipeline"]["visual_authorization"]
    manifest["pipeline"]["visual_capture"] = _empty_remote_capture(authorization)
    manifest["coverage"]["document"]["remote_text"] = {
        "pages_evaluated": 0,
        "pages_with_observations": 0,
        "observations": 0,
        "total_pages": 3,
    }
    manifest["coverage"]["document"]["remote_visual"] = {
        "pages_evaluated": 0,
        "full_layout_pages": 0,
        "evaluated_types": [],
    }
    manifest["coverage"]["document"]["text_authority"] = {
        "visual_page_count": 0,
        "ready_page_count": 0,
        "degraded_page_numbers": [],
    }


def _set_page_binding_to_source_asset(
    manifest: dict[str, Any],
    *,
    page_number: int,
    source_asset_sha256: str,
) -> None:
    authorization = manifest["pipeline"]["visual_authorization"]
    authorization_binding = next(
        record
        for record in authorization["authorized_pages"]
        if record["page_number"] == page_number
    )
    authorization_binding["page_binding_kind"] = "SOURCE_ASSET_SHA256"
    authorization_binding["page_binding_sha256"] = source_asset_sha256
    capture_route = manifest["pipeline"]["visual_capture"]["route"]
    capture_binding = next(
        record
        for record in capture_route["authorized_pages"]
        if record["page_number"] == page_number
    )
    capture_binding["page_binding_kind"] = "SOURCE_ASSET_SHA256"
    capture_binding["page_binding_sha256"] = source_asset_sha256


def _remote_authorization(source_sha256: str) -> dict[str, object]:
    return {
        "schema_version": "extraction-visual-route/v1",
        "authorization_status": "AUTHORIZED",
        "source_sha256": source_sha256,
        "route_id": "pvr-fixture",
        "route_sha256": "1" * 64,
        "authorized_page_count": 2,
        "authorized_pages": [
            {
                "page_number": 1,
                "input_rotation_cw_degrees": 0,
                "page_binding_kind": "PAGE_IMAGE_SHA256",
                "page_binding_sha256": "1" * 64,
            },
            {
                "page_number": 2,
                "input_rotation_cw_degrees": 0,
                "page_binding_kind": "PAGE_IMAGE_SHA256",
                "page_binding_sha256": "2" * 64,
            },
        ],
        "providers": [
            {
                "name": "bailian",
                "id": "aliyun-bailian-qwen-ocr",
                "authorized": True,
                "selectable": True,
                "reason_code": "",
            }
        ],
        "routing_policy_id": "native-first-exact-page-route/v1",
        "remote_only": True,
        "local_ocr": False,
        "implicit_fallback": False,
        "selected_provider": {
            "name": "bailian",
            "id": "aliyun-bailian-qwen-ocr",
            "version": "fixture-v1",
        },
    }


def _remote_capture(authorization: dict[str, object]) -> dict[str, object]:
    selected_provider = authorization["selected_provider"]
    authorized_pages = authorization["authorized_pages"]
    return {
        "mode": "REMOTE_VISUAL",
        "capability": "TEXT_OCR",
        "status": "CAPTURED",
        "provider": selected_provider,
        "snapshot_count": 1,
        "observation_count": 1,
        "authorized_page_count": authorization["authorized_page_count"],
        "captured_pages": [2],
        "routing": {
            "policy_id": authorization["routing_policy_id"],
            "status": "APPLIED",
            "native_page_count": 2,
            "visual_page_count": 1,
            "visual_pages": [{"page_number": 2, "reasons": ["fixture"]}],
        },
        "evaluated_content_types": ["TEXT"],
        "confidence": {
            "available": 1,
            "unavailable": 0,
            "minimum": 0.9,
            "maximum": 0.9,
            "mean": 0.9,
        },
        "route": {
            "authorization_status": "AUTHORIZED",
            "route_id": authorization["route_id"],
            "route_sha256": authorization["route_sha256"],
            "source_sha256": authorization["source_sha256"],
            "authorized_pages": authorized_pages,
            "allowed_provider_ids": ["aliyun-bailian-qwen-ocr"],
        },
        "coordinate_mapping": {
            "provider_space": "PAGE_IMAGE_TOP_LEFT_PIXELS",
            "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "status": "LEGACY_BBOX_ONLY",
        },
        "visual_table": {
            "mode": "OFF",
            "provider": None,
            "authorized_pages": [],
            "snapshot_count": 0,
        },
        "local_fallback": False,
        "implicit_fallback": False,
        "failure_policy": "BLOCKING",
        "message": "fixture",
    }


def _empty_remote_capture(
    authorization: dict[str, object],
) -> dict[str, object]:
    capture = _remote_capture(authorization)
    capture["capability"] = "TEXT_OCR"
    capture["snapshot_count"] = 0
    capture["observation_count"] = 0
    capture["captured_pages"] = []
    capture["routing"] = {
        "policy_id": authorization["routing_policy_id"],
        "status": "APPLIED",
        "native_page_count": 3,
        "visual_page_count": 0,
        "visual_pages": [],
    }
    capture["evaluated_content_types"] = []
    capture["confidence"] = {
        "available": 0,
        "unavailable": 0,
        "minimum": None,
        "maximum": None,
        "mean": None,
    }
    return capture


def _attach_remote_page(
    preview: dict[str, object],
    authorization: dict[str, object],
) -> None:
    page_comparison = preview["page_comparison"]
    page = page_comparison["pages"][1]
    native_block = next(
        block for block in page["blocks"] if block.get("type") == "TEXT"
    )
    remote_block = copy.deepcopy(native_block)
    remote_block["id"] = "vto-" + ("a" * 32)
    remote_block["source_ref"] = {
        "kind": "visual_text_observation",
        "source_id": remote_block["id"],
    }
    remote_block["source_kind"] = "ocr_observation"
    remote_block["provenance"] = {
        "channel": "REMOTE_VISUAL",
        "fact_kind": "VISUAL_PAGE_OBSERVATION",
        "source_sha256": authorization["source_sha256"],
        "page_image_sha256": "2" * 64,
        "adapter": {
            "id": preview["pipeline"]["source_ir"]["id"],
            "version": preview["pipeline"]["source_ir"]["version"],
        },
        "provider": {
            "id": "aliyun-bailian-qwen-ocr",
            "version": "fixture-v1",
        },
        "confidence": 0.9,
        "coordinate_mapping": {
            "source_space": "PAGE_IMAGE_TOP_LEFT_PIXELS",
            "target_space": "PDF_VISIBLE_PAGE_POINTS_TOP_LEFT",
            "status": "LEGACY_BBOX_ONLY",
        },
    }
    page["blocks"].append(remote_block)
    page["counts"]["blocks"] += 1
    page["counts"]["content_blocks"] += 1
    page["counts"]["locatable"] += 1
    page["counts"]["by_type"]["TEXT"] += 1
    page["counts"]["by_channel"]["REMOTE_VISUAL"] = 1
    text_coverage = next(
        entry for entry in page["coverage"]["content_types"] if entry["type"] == "TEXT"
    )
    text_coverage["observed_count"] += 1
    text_coverage["channels"] = ["NATIVE", "REMOTE_VISUAL"]
    page["coverage"]["remote_text"] = {
        "status": "OBSERVED",
        "evaluated": True,
        "observation_count": 1,
    }
    page["coverage"]["remote_visual"] = {
        "evaluated_types": ["TEXT"],
        "full_layout_evaluated": False,
    }
    page["coverage"]["text_authority"] = {
        "channel": "REMOTE_VISUAL",
        "status": "READY",
        "reason_codes": [],
    }
    page_comparison["coverage"]["remote_text"] = {
        "pages_evaluated": 1,
        "pages_with_observations": 1,
        "observations": 1,
        "total_pages": 3,
    }
    page_comparison["coverage"]["remote_visual"] = {
        "pages_evaluated": 1,
        "full_layout_pages": 0,
        "evaluated_types": ["TEXT"],
    }
    page_comparison["coverage"]["text_authority"] = {
        "visual_page_count": 1,
        "ready_page_count": 1,
        "degraded_page_numbers": [],
    }
    document_text_coverage = next(
        entry
        for entry in page_comparison["coverage"]["content_types"]
        if entry["type"] == "TEXT"
    )
    document_text_coverage["observed_blocks"] += 1


if __name__ == "__main__":
    unittest.main()
