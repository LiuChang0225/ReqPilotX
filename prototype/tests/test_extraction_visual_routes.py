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
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.adapters.documents.pdf_visual_text_runtime import (
    OCR_PROVIDER_AZURE,
    OCR_PROVIDER_BAILIAN,
    OCR_PROVIDER_BAILIAN_FLASH_LAYOUT,
    OCR_PROVIDER_BAILIAN_LAYOUT,
    visual_text_provider_id,
)
from app.services.extraction_visual_routes import (
    DEFAULT_VISUAL_ROUTE_REGISTRY_PATH,
    EXTRACTION_VISUAL_ROUTE_REGISTRY_SCHEMA_VERSION,
    ExtractionVisualRouteRejected,
    build_authorized_pdf_visual_runtime,
    load_authorized_pdf_visual_routes,
    visual_route_capabilities,
)


QC_T_SOURCE_SHA256 = "5db1d344310f2a0a1e7384afb919a5ed4a0889a9af0dbd3bb20977deaac26d6e"
PEF_SOURCE_SHA256 = "5a9d45a36d95b01f0c9bb85a2eca8ca3b0aadbed14addca202c90f34c595736e"
IRPS_SOURCE_SHA256 = "47713c0e8451a4249912fd5b11a3f2ff2e50e059abfe6454198bcf4f89700ea0"


class ExtractionVisualRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = self.root / "visual-routes.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_loader_rejects_annotation_fields_and_empty_routes(self) -> None:
        source_sha256 = "1" * 64
        payload = _registry({"authorized": _document(source_sha256=source_sha256)})
        payload["documents"]["authorized"]["semantic_expectations"] = {
            "must_not_enter_product_route": [None, {"arbitrary": True}]
        }
        self._write(payload)

        with self.assertRaises(ExtractionVisualRouteRejected) as annotated:
            load_authorized_pdf_visual_routes(self.registry)
        self.assertEqual(annotated.exception.code, "visual_route_registry_invalid")

        self._write(
            _registry(
                {
                    "native-only": {
                        "source_sha256": source_sha256,
                        "allowed_visual_provider_ids": [],
                        "page_assets": [],
                    }
                }
            )
        )
        with self.assertRaises(ExtractionVisualRouteRejected) as empty:
            load_authorized_pdf_visual_routes(self.registry)
        self.assertEqual(empty.exception.code, "visual_route_registry_invalid")

    def test_loader_rejects_legacy_silver_schema_and_unknown_page_fields(
        self,
    ) -> None:
        source_sha256 = "1" * 64
        legacy = _registry({"doc": _document(source_sha256=source_sha256)})
        legacy["schema_version"] = "pdf-shadow-silver/v1"
        self._write(legacy)
        with self.assertRaises(ExtractionVisualRouteRejected) as wrong_schema:
            load_authorized_pdf_visual_routes(self.registry)
        self.assertEqual(
            wrong_schema.exception.code,
            "visual_route_registry_invalid",
        )

        unknown_page = _registry({"doc": _document(source_sha256=source_sha256)})
        unknown_page["documents"]["doc"]["page_assets"][0]["fidelity"] = "PASS"
        self._write(unknown_page)
        with self.assertRaises(ExtractionVisualRouteRejected) as extra_field:
            load_authorized_pdf_visual_routes(self.registry)
        self.assertEqual(
            extra_field.exception.code,
            "visual_route_registry_invalid",
        )

    def test_route_hash_changes_when_exact_page_binding_changes(self) -> None:
        source_sha256 = "2" * 64
        self._write(_registry({"doc": _document(source_sha256=source_sha256)}))
        first = load_authorized_pdf_visual_routes(self.registry)[source_sha256]
        changed = _document(source_sha256=source_sha256)
        changed["page_assets"][0]["source_asset_sha256"] = "e" * 64
        self._write(_registry({"doc": changed}))
        second = load_authorized_pdf_visual_routes(self.registry)[source_sha256]

        self.assertNotEqual(first.route_id, second.route_id)
        self.assertNotEqual(first.route_sha256, second.route_sha256)

    def test_default_registry_is_annotation_free_and_decoupled_from_silver(
        self,
    ) -> None:
        require_private_eval("extraction_cases/visual_routes.json")
        require_private_eval("extraction_cases/silver.json")
        product_payload = json.loads(
            DEFAULT_VISUAL_ROUTE_REGISTRY_PATH.read_text(encoding="utf-8")
        )
        silver_path = DEFAULT_VISUAL_ROUTE_REGISTRY_PATH.with_name("silver.json")
        silver_bytes = silver_path.read_bytes()
        silver_payload = json.loads(silver_bytes)
        with self.assertRaises(ExtractionVisualRouteRejected) as silver_registry:
            load_authorized_pdf_visual_routes(silver_path)
        self.assertEqual(
            silver_registry.exception.code,
            "visual_route_registry_invalid",
        )

        self.assertEqual(
            DEFAULT_VISUAL_ROUTE_REGISTRY_PATH.name,
            "visual_routes.json",
        )
        self.assertEqual(
            set(product_payload),
            {"schema_version", "documents"},
        )
        self.assertEqual(
            product_payload["schema_version"],
            "extraction-visual-route-registry/v1",
        )
        for document in product_payload["documents"].values():
            self.assertEqual(
                set(document),
                {
                    "source_sha256",
                    "allowed_visual_provider_ids",
                    "page_assets",
                },
            )

        silver_qc_t = silver_payload["documents"]["DOC-QC-T-1067"]
        self.assertEqual(
            [item["page_number"] for item in silver_qc_t["page_assets"]],
            [11, 29, 30, 31],
        )

        routes = load_authorized_pdf_visual_routes()
        self.assertEqual(
            set(routes),
            {QC_T_SOURCE_SHA256, PEF_SOURCE_SHA256, IRPS_SOURCE_SHA256},
        )
        qc_t_capability = visual_route_capabilities(routes[QC_T_SOURCE_SHA256])
        self.assertEqual(qc_t_capability["authorized_page_count"], 60)
        self.assertEqual(
            [item["page_number"] for item in qc_t_capability["authorized_pages"]],
            list(range(1, 61)),
        )
        self.assertTrue(
            all(
                item["input_rotation_cw_degrees"] == 0
                and item["page_binding_kind"] == "SOURCE_ASSET_SHA256"
                for item in qc_t_capability["authorized_pages"]
            )
        )

        product_documents = product_payload["documents"]
        product_qc_t_by_page = {
            item["page_number"]: item["source_asset_sha256"]
            for item in product_documents["DOC-QC-T-1067"]["page_assets"]
        }
        silver_qc_t_by_page = {
            item["page_number"]: item["source_asset_sha256"]
            for item in silver_qc_t["page_assets"]
        }
        self.assertEqual(
            {page: product_qc_t_by_page[page] for page in silver_qc_t_by_page},
            silver_qc_t_by_page,
        )
        for document_id, expected_count in (
            ("DOC-PEF-SOR", 27),
            ("DOC-IRPS-NCS-CM-PDF", 4),
        ):
            self.assertEqual(
                product_documents[document_id],
                {
                    key: silver_payload["documents"][document_id][key]
                    for key in (
                        "source_sha256",
                        "allowed_visual_provider_ids",
                        "page_assets",
                    )
                },
            )
            self.assertEqual(
                len(product_documents[document_id]["page_assets"]),
                expected_count,
            )

    def test_duplicate_source_routes_fail_closed(self) -> None:
        source_sha256 = "3" * 64
        self._write(
            _registry(
                {
                    "first": _document(source_sha256=source_sha256),
                    "second": _document(source_sha256=source_sha256),
                }
            )
        )

        with self.assertRaisesRegex(
            ExtractionVisualRouteRejected,
            "More than one controlled visual route",
        ):
            load_authorized_pdf_visual_routes(self.registry)

    def test_page_requires_exactly_one_valid_hash_binding(self) -> None:
        for binding in (
            {},
            {"source_asset_sha256": "a" * 64, "page_image_sha256": "b" * 64},
        ):
            with self.subTest(binding=binding):
                document = _document(source_sha256="4" * 64)
                document["page_assets"][0] = {"page_number": 1, **binding}
                self._write(_registry({"doc": document}))
                with self.assertRaises(ExtractionVisualRouteRejected) as raised:
                    load_authorized_pdf_visual_routes(self.registry)
                self.assertEqual(raised.exception.code, "visual_route_registry_invalid")

    def test_capability_marks_azure_unselectable_for_explicit_rotation(self) -> None:
        source_sha256 = "5" * 64
        document = _document(source_sha256=source_sha256)
        document["page_assets"][0]["input_rotation_cw_degrees"] = 90
        self._write(_registry({"doc": document}))
        route = load_authorized_pdf_visual_routes(self.registry)[source_sha256]

        capability = visual_route_capabilities(route)
        providers = {provider["name"]: provider for provider in capability["providers"]}

        self.assertEqual(capability["authorization_status"], "AUTHORIZED")
        self.assertTrue(providers[OCR_PROVIDER_BAILIAN_LAYOUT]["selectable"])
        self.assertEqual(
            providers[OCR_PROVIDER_BAILIAN_LAYOUT]["id"],
            providers[OCR_PROVIDER_BAILIAN]["id"],
        )
        self.assertTrue(providers[OCR_PROVIDER_BAILIAN_FLASH_LAYOUT]["selectable"])
        self.assertEqual(
            providers[OCR_PROVIDER_BAILIAN_FLASH_LAYOUT]["id"],
            providers[OCR_PROVIDER_BAILIAN]["id"],
        )
        self.assertTrue(providers[OCR_PROVIDER_BAILIAN]["selectable"])
        self.assertFalse(providers[OCR_PROVIDER_AZURE]["selectable"])
        self.assertEqual(
            providers[OCR_PROVIDER_AZURE]["reason_code"],
            "explicit_rotation_not_supported",
        )

    def test_local_provider_is_rejected_before_runtime_or_credentials(self) -> None:
        source_bytes = b"reviewed source"
        source = self.root / "source.pdf"
        source.write_bytes(source_bytes)
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        self._write(_registry({"doc": _document(source_sha256=source_sha256)}))

        with patch(
            "app.services.extraction_visual_routes.build_pdf_visual_text_runtime_from_silver"
        ) as runtime_factory:
            with self.assertRaises(ExtractionVisualRouteRejected) as raised:
                build_authorized_pdf_visual_runtime(
                    source=source,
                    source_sha256=source_sha256,
                    provider_name="local-tesseract",
                    registry_path=self.registry,
                )

        self.assertEqual(raised.exception.code, "visual_provider_not_supported")
        runtime_factory.assert_not_called()

    def test_source_hash_mismatch_fails_before_remote_provider_resolution(self) -> None:
        source = self.root / "source.pdf"
        source.write_bytes(b"different source bytes")
        self._write(_registry({"doc": _document(source_sha256="6" * 64)}))

        with self.assertRaises(ExtractionVisualRouteRejected) as raised:
            build_authorized_pdf_visual_runtime(
                source=source,
                source_sha256="6" * 64,
                provider_name=OCR_PROVIDER_BAILIAN,
                registry_path=self.registry,
            )

        self.assertEqual(raised.exception.code, "visual_route_source_mismatch")

    def _write(self, payload: dict[str, object]) -> None:
        self.registry.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )


def _registry(documents: dict[str, dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": EXTRACTION_VISUAL_ROUTE_REGISTRY_SCHEMA_VERSION,
        "documents": documents,
    }


def _document(*, source_sha256: str) -> dict[str, object]:
    return {
        "source_sha256": source_sha256,
        "allowed_visual_provider_ids": [
            visual_text_provider_id(OCR_PROVIDER_BAILIAN),
            visual_text_provider_id(OCR_PROVIDER_AZURE),
        ],
        "page_assets": [
            {
                "page_number": 1,
                "input_rotation_cw_degrees": 0,
                "source_asset_sha256": "d" * 64,
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
