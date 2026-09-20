from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import fitz
from fastapi import Response
from pydantic import ValidationError

from app import db
if __package__:
    from .application_under_test import main
else:
    from application_under_test import main
from app.main import app as http_app

from app.application.extraction.build_shadow_preview import (
    build_extraction_shadow_preview,
)
from app.services import extraction_shadow_runs
from app.services.extraction_shadow_runs import (
    ExtractionShadowRunRejected,
    SHADOW_PAGE_SCHEMA_VERSION,
    SHADOW_RUN_SCHEMA_VERSION,
    build_extraction_shadow_artifacts,
    get_extraction_shadow_page_path,
    get_extraction_shadow_run,
    get_latest_extraction_shadow_run,
    purge_extraction_shadow_runs,
    start_extraction_shadow_run,
    wait_for_extraction_shadow_run,
)
from app.services.extraction_visual_routes import ExtractionVisualRouteRejected


main.app = http_app


class ExtractionShadowArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "three-pages.pdf"
        _write_three_page_pdf(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_splits_full_preview_without_losing_page_facts(self) -> None:
        preview = build_extraction_shadow_preview(
            self.source,
            collection="document_outline",
            limit=5_000,
        )

        metadata, pages, details = build_extraction_shadow_artifacts(
            preview,
            run_id="a" * 64,
            project_id="project",
            document_id="document",
        )

        self.assertNotIn("document_outline", metadata)
        self.assertNotIn("tables", metadata)
        self.assertEqual(metadata["page_comparison"]["pages"], [])
        self.assertEqual(
            metadata["page_comparison"]["coverage"],
            preview["page_comparison"]["coverage"],
        )
        self.assertEqual(
            metadata["page_comparison"]["content_type_contract"],
            preview["page_comparison"]["content_type_contract"],
        )
        self.assertEqual(set(pages), {1, 2, 3})
        self.assertEqual(
            [pages[index]["page_comparison"]["pages"][0] for index in (1, 2, 3)],
            preview["page_comparison"]["pages"],
        )
        self.assertTrue(
            all(
                page["schema_version"] == SHADOW_PAGE_SCHEMA_VERSION
                for page in pages.values()
            )
        )
        self.assertEqual(details["findings"], preview["findings"])
        self.assertEqual(details["diagnostics"], preview["diagnostics"])
        self.assertEqual(
            details["page_comparison"]["unassigned_blocks"],
            preview["page_comparison"]["unassigned_blocks"],
        )


class ExtractionShadowRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.exit_stack = ExitStack()
        self.exit_stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        self.exit_stack.enter_context(patch.object(db, "STORAGE_DIR", self.root))
        db.init_db()

        self.source = self.root / "run-source.pdf"
        _write_three_page_pdf(self.source)
        source_sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.project = db.create_project(name="Page run", customer_name="Customer")
        self.document = db.add_sor_document(
            project_id=self.project["id"],
            file_name=self.source.name,
            file_type="pdf",
            file_path=str(self.source),
            extracted_text="fixture",
            source_sha256=source_sha256,
            source_size=self.source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )

    def tearDown(self) -> None:
        self.exit_stack.close()
        self.temporary.cleanup()

    def test_single_flight_builds_page_files_and_then_hits_cache(self) -> None:
        real_builder = extraction_shadow_runs.build_extraction_shadow_preview
        with patch.object(
            extraction_shadow_runs,
            "build_extraction_shadow_preview",
            wraps=real_builder,
        ) as builder:
            first = start_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
                initial_page=2,
            )
            second = start_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
                initial_page=1,
            )
            self.assertEqual(first["run_id"], second["run_id"])
            ready = wait_for_extraction_shadow_run(first["run_id"])
            self.assertEqual(ready["run_status"], "READY")
            self.assertEqual(builder.call_count, 1)

            cached = start_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
            )
            self.assertTrue(cached["cache_hit"])
            self.assertEqual(cached["run_status"], "READY")
            self.assertEqual(builder.call_count, 1)
            latest = get_latest_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
            )
            self.assertIsNotNone(latest)
            self.assertEqual(latest["run_id"], first["run_id"])
            self.assertTrue(latest["cache_hit"])

        status = get_extraction_shadow_run(
            project_id=self.project["id"],
            document=self.document,
            run_id=first["run_id"],
        )
        self.assertEqual(status["schema_version"], SHADOW_RUN_SCHEMA_VERSION)
        self.assertEqual(status["progress"]["page_count"], 3)
        self.assertEqual(status["progress"]["pages_ready"], 3)
        self.assertEqual(
            status["cache_identity"]["structure_resolver"]["version"],
            extraction_shadow_runs.DEFAULT_STRUCTURE_RESOLVER_VERSION,
        )
        self.assertEqual(status["preview"]["page_comparison"]["pages"], [])

        for page_number in (1, 2, 3):
            path = get_extraction_shadow_page_path(
                project_id=self.project["id"],
                document=self.document,
                run_id=first["run_id"],
                page_number=page_number,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["page_number"], page_number)
            self.assertEqual(len(payload["page_comparison"]["pages"]), 1)

    def test_latest_run_lookup_does_not_create_background_work(self) -> None:
        latest = get_latest_extraction_shadow_run(
            project_id=self.project["id"],
            document=self.document,
        )

        self.assertIsNone(latest)
        self.assertEqual(
            list(extraction_shadow_runs.extraction_shadow_cache_root().iterdir()),
            [],
        )

    def test_page_access_fails_closed_after_source_changes(self) -> None:
        started = start_extraction_shadow_run(
            project_id=self.project["id"],
            document=self.document,
        )
        ready = wait_for_extraction_shadow_run(started["run_id"])
        self.assertEqual(ready["run_status"], "READY")
        self.source.write_bytes(self.source.read_bytes() + b"\n")

        with self.assertRaises(ExtractionShadowRunRejected) as context:
            get_extraction_shadow_page_path(
                project_id=self.project["id"],
                document=self.document,
                run_id=started["run_id"],
                page_number=1,
            )

        self.assertEqual(context.exception.code, "source_hash_mismatch")
        self.assertEqual(context.exception.status_code, 409)

    def test_generated_versions_must_match_cache_identity(self) -> None:
        mismatched = build_extraction_shadow_preview(
            self.source,
            expected_source_sha256=self.document["source_sha256"],
            collection="document_outline",
            limit=5_000,
        )
        mismatched["pipeline"]["structure"]["version"] = "unexpected-version"
        with patch.object(
            extraction_shadow_runs,
            "build_extraction_shadow_preview",
            return_value=mismatched,
        ):
            started = start_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
            )
            failed = wait_for_extraction_shadow_run(started["run_id"])

        self.assertEqual(failed["run_status"], "FAILED")
        self.assertEqual(failed["error"]["code"], "shadow_run_identity_mismatch")

    def test_run_is_document_scoped_and_purge_removes_only_derived_cache(self) -> None:
        started = start_extraction_shadow_run(
            project_id=self.project["id"],
            document=self.document,
        )
        ready = wait_for_extraction_shadow_run(started["run_id"])
        self.assertEqual(ready["run_status"], "READY")
        other_document = db.add_sor_document(
            project_id=self.project["id"],
            file_name="other.pdf",
            file_type="pdf",
            file_path=str(self.source),
            extracted_text="fixture",
            source_sha256=self.document["source_sha256"],
            source_size=self.source.stat().st_size,
            declared_extension=".pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )

        with self.assertRaises(ExtractionShadowRunRejected) as context:
            get_extraction_shadow_page_path(
                project_id=self.project["id"],
                document=other_document,
                run_id=started["run_id"],
                page_number=1,
            )
        self.assertEqual(context.exception.status_code, 404)

        run_dir = (
            extraction_shadow_runs.extraction_shadow_cache_root() / started["run_id"]
        )
        self.assertTrue(run_dir.is_dir())
        purge_extraction_shadow_runs(self.document["id"])
        self.assertFalse(run_dir.exists())
        self.assertTrue(self.source.is_file())

    def test_api_returns_accepted_status_then_page_file(self) -> None:
        response = Response()
        started = main.start_document_restoration_run(
            self.project["id"],
            self.document["id"],
            main.RestorationRunRequest(initial_page=2),
            response,
        )
        self.assertIn(response.status_code, {200, 202})
        self.assertIn("no-store", response.headers["Cache-Control"])
        ready = wait_for_extraction_shadow_run(started["run_id"])
        self.assertEqual(ready["run_status"], "READY")
        latest_response = Response()
        latest = main.get_latest_document_restoration_run(
            self.project["id"],
            self.document["id"],
            latest_response,
        )
        self.assertEqual(latest["run_id"], started["run_id"])
        self.assertIn("no-store", latest_response.headers["Cache-Control"])

        file_response = main.get_document_restoration_page(
            self.project["id"],
            self.document["id"],
            started["run_id"],
            2,
        )
        self.assertEqual(file_response.media_type, "application/json")
        payload = json.loads(Path(str(file_response.path)).read_text(encoding="utf-8"))
        self.assertEqual(payload["page_number"], 2)

    def test_capability_endpoint_is_source_scoped_and_does_not_prepare_provider(
        self,
    ) -> None:
        capability = {
            "schema_version": "extraction-visual-route/v1",
            "authorization_status": "AUTHORIZED",
            "source_sha256": None,
            "route_id": "pvr-test",
            "route_sha256": "c" * 64,
            "authorized_page_count": 2,
            "authorized_pages": [],
            "providers": [],
            "remote_only": True,
            "local_ocr": False,
            "implicit_fallback": False,
        }
        with (
            patch.object(
                extraction_shadow_runs,
                "resolve_authorized_pdf_visual_route",
                return_value=object(),
            ) as route_resolver,
            patch.object(
                extraction_shadow_runs,
                "visual_route_capabilities",
                return_value=capability,
            ),
            patch.object(
                extraction_shadow_runs,
                "build_authorized_pdf_visual_runtime",
            ) as runtime_builder,
            patch.object(
                extraction_shadow_runs,
                "_inspect_document_source",
            ) as full_source_inspector,
        ):
            response = Response()
            result = main.get_document_restoration_capabilities(
                self.project["id"],
                self.document["id"],
                response,
            )

        route_resolver.assert_called_once_with(self.document["source_sha256"])
        runtime_builder.assert_not_called()
        full_source_inspector.assert_not_called()
        self.assertEqual(result["document_id"], self.document["id"])
        self.assertEqual(result["source_sha256"], self.document["source_sha256"])
        self.assertEqual(result["route_id"], "pvr-test")
        self.assertFalse(result["local_ocr"])
        self.assertEqual(result["identity_basis"], "REGISTERED_UPLOAD_SHA256")
        self.assertEqual(
            result["runtime_source_revalidation"],
            "REQUIRED_BEFORE_PROVIDER",
        )
        self.assertFalse(result["provider_credentials_checked"])
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_capability_endpoint_keeps_legacy_hashless_upload_native_only(self) -> None:
        legacy_document = {
            **self.document,
            "source_sha256": None,
            "source_size": None,
        }
        with (
            patch.object(
                extraction_shadow_runs,
                "resolve_authorized_pdf_visual_route",
            ) as route_resolver,
            patch.object(
                extraction_shadow_runs,
                "build_authorized_pdf_visual_runtime",
            ) as runtime_builder,
            patch.object(
                extraction_shadow_runs,
                "_inspect_document_source",
            ) as full_source_inspector,
        ):
            result = extraction_shadow_runs.get_extraction_shadow_visual_capabilities(
                project_id=self.project["id"],
                document=legacy_document,
            )

        route_resolver.assert_not_called()
        runtime_builder.assert_not_called()
        full_source_inspector.assert_not_called()
        self.assertEqual(
            result["authorization_status"],
            "SOURCE_IDENTITY_UNAVAILABLE",
        )
        self.assertEqual(
            result["reason_code"],
            "visual_route_source_identity_unavailable",
        )
        self.assertIsNone(result["source_sha256"])
        self.assertEqual(result["providers"], [])
        self.assertFalse(result["local_ocr"])
        self.assertFalse(result["provider_credentials_checked"])

    def test_remote_run_binds_provider_version_and_route_into_cache_identity(
        self,
    ) -> None:
        provider_name = "bailian"
        visual_identity = {
            "mode": "REMOTE_VISUAL",
            "provider_name": provider_name,
            "provider_id": "fixture-provider",
            "provider_version": "provider-v7",
            "route_id": "pvr-fixture",
            "route_sha256": "b" * 64,
            "authorized_page_count": 3,
        }
        fake_runtime_value = object()
        fake_authorized = SimpleNamespace(
            runtime=SimpleNamespace(
                pdf_adapter=SimpleNamespace(
                    adapter_version=extraction_shadow_runs.PDF_SOURCE_IR_ADAPTER_VERSION
                ),
                visual_text_provider=fake_runtime_value,
            ),
            cache_identity=lambda: dict(visual_identity),
        )
        real_builder = extraction_shadow_runs.build_extraction_shadow_preview
        captured: dict[str, object] = {}

        def build_remote_fixture(path: str, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            preview = real_builder(
                path,
                expected_source_sha256=str(kwargs["expected_source_sha256"]),
                visual_provider="off",
                collection=str(kwargs["collection"]),
                offset=int(kwargs["offset"]),
                limit=int(kwargs["limit"]),
            )
            preview["visual_provider"] = provider_name
            preview["ocr"] = {
                "mode": "REMOTE_VISUAL",
                "provider": {
                    "id": visual_identity["provider_id"],
                    "version": visual_identity["provider_version"],
                },
                "route": {
                    "route_id": visual_identity["route_id"],
                    "route_sha256": visual_identity["route_sha256"],
                },
                "authorized_page_count": visual_identity["authorized_page_count"],
                "local_fallback": False,
                "implicit_fallback": False,
            }
            return preview

        with (
            patch.object(
                extraction_shadow_runs,
                "_prepare_authorized_visual_runtime",
                return_value=fake_authorized,
            ),
            patch.object(
                extraction_shadow_runs,
                "visual_runtime_audit",
                return_value={"route_id": "pvr-fixture"},
            ),
            patch.object(
                extraction_shadow_runs,
                "build_extraction_shadow_preview",
                side_effect=build_remote_fixture,
            ),
        ):
            started = start_extraction_shadow_run(
                project_id=self.project["id"],
                document=self.document,
                visual_provider=provider_name,
            )
            ready = wait_for_extraction_shadow_run(started["run_id"])

        self.assertEqual(ready["run_status"], "READY")
        self.assertEqual(ready["cache_identity"]["visual_runtime"], visual_identity)
        self.assertEqual(ready["cache_identity"]["visual_provider"], provider_name)
        self.assertIs(captured["visual_runtime"], fake_authorized.runtime)
        self.assertEqual(captured["visual_authorization"], {"route_id": "pvr-fixture"})
        self.assertEqual(captured["visual_provider"], provider_name)

    def test_remote_route_failure_is_explicit_and_never_falls_back(self) -> None:
        with (
            patch.object(
                extraction_shadow_runs,
                "build_authorized_pdf_visual_runtime",
                side_effect=ExtractionVisualRouteRejected(
                    "visual_provider_not_configured",
                    "The selected remote visual provider is not configured.",
                ),
            ),
            patch.object(
                extraction_shadow_runs,
                "build_extraction_shadow_preview",
            ) as preview_builder,
        ):
            with self.assertRaises(ExtractionShadowRunRejected) as raised:
                start_extraction_shadow_run(
                    project_id=self.project["id"],
                    document=self.document,
                    visual_provider="bailian",
                )

        self.assertEqual(raised.exception.code, "visual_provider_not_configured")
        self.assertEqual(raised.exception.status_code, 422)
        preview_builder.assert_not_called()

    def test_run_request_forbids_local_ocr_provider(self) -> None:
        request = main.RestorationRunRequest(
            visual_provider="bailian-qwen3.6-flash-layout"
        )
        self.assertEqual(
            request.visual_provider,
            "bailian-qwen3.6-flash-layout",
        )
        with self.assertRaises(ValidationError):
            main.RestorationRunRequest(visual_provider="local-tesseract")


def _write_three_page_pdf(path: Path) -> None:
    document = fitz.open()
    for page_number in range(1, 4):
        page = document.new_page(width=420, height=300)
        page.insert_text((40, 45), f"{page_number} Section {page_number}", fontsize=16)
        page.insert_text(
            (40, 90),
            f"The supplier shall provide page {page_number} evidence.",
            fontsize=11,
        )
    document.save(path)
    document.close()


if __name__ == "__main__":
    unittest.main()
