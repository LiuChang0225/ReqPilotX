from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import io
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pymupdf
from app.adapters.storage.local_object_store import LocalObjectStore
from app.adapters.documents.pdf_source_ir_adapter import (
    PageStagingAuthorityLostError,
    PdfSourceIRAdapter,
)
from app.adapters.documents.preflight import LocalDocumentPreflight
from app.application.extraction.processing_pipeline import (
    build_local_structure_document_result,
    build_source_ir_for_plan,
    processing_plan_visual_strategy,
)
from app.application.extraction.source_asset_objects import (
    build_source_asset_loader,
    build_source_asset_publisher,
    recover_stale_source_asset_publications,
    release_source_asset_objects_after_document_delete,
    runtime_source_asset_publisher,
    source_asset_publication_attempt,
)
from app.application.extraction import document_jobs, source_asset_objects
from app.domain.extraction.processing_profile import (
    DocumentProcessingProfileV1,
    ResolvedProviderBindingsV1,
    build_local_rules_profile,
    build_project_processing_profile,
    build_resolved_processing_plan,
)
from app.domain.extraction.source_ir import SourceAssetKind, SourceAssetRef
from app.ports.object_store import StoredObject
from app.infrastructure.runtime_settings import DatabaseBackend


def _asset(payload: bytes, *, extension: str = "png") -> SourceAssetRef:
    content_sha256 = hashlib.sha256(payload).hexdigest()
    return SourceAssetRef(
        asset_id=f"asset-{content_sha256[:32]}",
        kind=SourceAssetKind.IMAGE,
        content_sha256=content_sha256,
        media_type="image/png",
        storage_key=(
            f"source-assets/{content_sha256[:2]}/"
            f"{content_sha256}.{extension}"
        ),
    )


class _BadPostPutStore:
    def __init__(self) -> None:
        self.stat_calls = 0
        self.put_calls = 0

    def stat(self, key: str) -> StoredObject | None:
        self.stat_calls += 1
        if self.stat_calls == 1:
            return None
        return StoredObject(
            key=key,
            size=5,
            sha256="0" * 64,
            media_type="image/png",
        )

    def put(self, **_kwargs: object) -> StoredObject:
        self.put_calls += 1
        return StoredObject(
            key="ignored",
            size=5,
            sha256="f" * 64,
            media_type="image/png",
        )

    def open(self, _key: str) -> io.BytesIO:
        return io.BytesIO()

    def delete(self, _key: str) -> bool:
        return False


class _DeclaredObjectStore:
    def __init__(self, *, payload: bytes, sha256: str, size: int) -> None:
        self.payload = payload
        self.sha256 = sha256
        self.size = size

    def stat(self, key: str) -> StoredObject:
        return StoredObject(
            key=key,
            size=self.size,
            sha256=self.sha256,
            media_type="image/png",
        )

    def open(self, _key: str) -> io.BytesIO:
        return io.BytesIO(self.payload)

    def put(self, **_kwargs: object) -> StoredObject:  # pragma: no cover
        raise AssertionError("put was not expected")

    def delete(self, _key: str) -> bool:  # pragma: no cover
        raise AssertionError("delete was not expected")


class SourceAssetObjectTests(unittest.TestCase):
    def test_page_staging_io_failure_preserves_formal_source_and_structure_hashes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "staging-side-channel.pdf"
            document = pymupdf.open()
            page = document.new_page(width=320, height=240)
            page.insert_text((40, 80), "Supplier shall provide evidence")
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            profile = build_local_rules_profile(project_id="project-1")
            plan = build_resolved_processing_plan(
                project_id="project-1",
                document_id="document-1",
                source_sha256=report.source_sha256,
                profile=profile,
                document=profile.document,
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf"
                ),
                connection_revision=0,
            )
            baseline = build_local_structure_document_result(
                source,
                plan=plan,
                result_runtime_sha256="d" * 64,
            )

            def unavailable(_candidate: dict) -> None:
                raise OSError("object store unavailable")

            degraded_staging = build_local_structure_document_result(
                source,
                plan=plan,
                result_runtime_sha256="d" * 64,
                page_staging_callback=unavailable,
            )

        self.assertEqual(
            baseline["pipeline"]["source_ir"]["canonical_sha256"],
            degraded_staging["pipeline"]["source_ir"]["canonical_sha256"],
        )
        self.assertEqual(
            baseline["pipeline"]["structure"]["canonical_sha256"],
            degraded_staging["pipeline"]["structure"]["canonical_sha256"],
        )

    def test_page_staging_io_failure_degrades_status_without_stopping_delivery(
        self,
    ) -> None:
        updates: list[dict[str, object]] = []
        callback = document_jobs._build_document_extraction_page_staging_callback(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            worker_id="worker-1",
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
        )
        with (
            mock.patch(
                "app.application.extraction.source_page_facts."
                "persist_document_extraction_staged_page",
                side_effect=OSError("object store unavailable"),
            ),
            mock.patch.object(
                document_jobs,
                "_update_llm_extraction_job",
                side_effect=lambda _document_id, _job_id, **values: (
                    updates.append(values) or values
                ),
            ),
        ):
            callback({"page_number": 1})

        self.assertEqual(updates[0]["page_staging_state"], "DEGRADED")
        self.assertEqual(
            updates[0]["page_staging_error_code"],
            "page_staging_io_failed",
        )

    def test_page_staging_authority_and_cancel_fences_remain_fail_closed(self) -> None:
        callback = document_jobs._build_document_extraction_page_staging_callback(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            job_id="job-1",
            attempt_generation=7,
            worker_id="worker-1",
            processing_plan_sha256="b" * 64,
            result_runtime_sha256="c" * 64,
        )
        with (
            mock.patch(
                "app.application.extraction.source_page_facts."
                "persist_document_extraction_staged_page",
                side_effect=PageStagingAuthorityLostError("lease lost"),
            ),
            self.assertRaises(PageStagingAuthorityLostError),
        ):
            callback({"page_number": 1})

        with (
            mock.patch(
                "app.application.extraction.source_page_facts."
                "persist_document_extraction_staged_page",
                side_effect=OSError("object store unavailable"),
            ),
            mock.patch.object(
                document_jobs,
                "_update_llm_extraction_job",
                return_value=None,
            ),
            self.assertRaises(PageStagingAuthorityLostError),
        ):
            callback({"page_number": 1})

    def test_page_staging_status_reports_completed_browsable_lag(self) -> None:
        job = {
            "status": "RUNNING",
            "stage": "source_ir",
            "job_id": "job-1",
            "source_sha256": "a" * 64,
            "completed_pages": 5,
            "page_staging_state": "DEGRADED",
            "page_staging_error_code": "page_staging_io_failed",
        }
        with mock.patch(
            "app.application.extraction.source_page_facts."
            "public_document_extraction_page_staging",
            return_value={
                "schema_version": "document-extraction-page-staging-summary/v1",
                "state": "STAGED_READ_ONLY",
                "ready_page_count": 3,
                "ready_page_ranges": [
                    {"start_page": 1, "end_page": 3}
                ],
            },
        ):
            response = document_jobs._build_llm_extraction_job_response(
                "project-1",
                "document-1",
                job=job,
            )

        staging = response["job"]["page_staging"]
        self.assertEqual(staging["state"], "DEGRADED")
        self.assertEqual(staging["completed_page_count"], 5)
        self.assertEqual(staging["ready_page_count"], 3)
        self.assertEqual(staging["browsable_lag_page_count"], 2)
        self.assertEqual(staging["error_code"], "page_staging_io_failed")

    def test_processing_pipeline_injects_publisher_into_native_pdf_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "native.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_image(
                pymupdf.Rect(20, 20, 100, 100),
                stream=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
                    b"\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
                    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
                ),
            )
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            profile = build_local_rules_profile(project_id="project-1")
            plan = build_resolved_processing_plan(
                project_id="project-1",
                document_id="document-1",
                source_sha256=report.source_sha256,
                profile=profile,
                document=profile.document,
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf"
                ),
                connection_revision=0,
            )
            published: list[str] = []

            with mock.patch(
                "app.application.extraction.source_asset_objects."
                "runtime_source_asset_publisher",
                return_value=lambda asset, _payload: published.append(
                    asset.storage_key
                ),
            ):
                snapshot = build_source_ir_for_plan(source, plan=plan)

            self.assertEqual(len(snapshot.assets), 1)
            self.assertEqual(published, [snapshot.assets[0].storage_key])

    def test_processing_pipeline_forwards_publisher_to_composable_pdf_route(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "composable.pdf"
            document = pymupdf.open()
            page = document.new_page()
            page.insert_image(
                pymupdf.Rect(20, 20, 100, 100),
                stream=(
                    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
                    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
                    b"\x90wS\xde\x00\x00\x00\x0cIDAT\x08\xd7c\xf8\xcf\xc0\x00\x00"
                    b"\x03\x01\x01\x00\xc9\xfe\x92\xef\x00\x00\x00\x00IEND\xaeB`\x82"
                ),
            )
            document.save(source)
            document.close()
            report = LocalDocumentPreflight().inspect(source)
            processing = DocumentProcessingProfileV1(
                primary_parser="pymupdf",
                enhancement_chain=("azure_document_intelligence",),
            )
            profile = build_project_processing_profile(
                project_id="project-1",
                document=processing,
                llm_provider="none",
                knowledge_backend="none",
                revision=0,
            )
            plan = build_resolved_processing_plan(
                project_id="project-1",
                document_id="document-1",
                source_sha256=report.source_sha256,
                profile=profile,
                document=processing,
                llm_provider="none",
                provider_bindings=ResolvedProviderBindingsV1(
                    primary_parser_provider="local:pymupdf",
                    enhancement_providers=(
                        "azure:document-intelligence:prebuilt-layout",
                    ),
                ),
                connection_revision=0,
            )
            published: list[str] = []

            def publisher(asset, _payload) -> None:
                published.append(asset.storage_key)

            forwarded: list[object] = []

            def build_adapter(
                _plan: object,
                *,
                source_asset_publisher=None,
                page_progress_callback=None,
            ):
                self.assertIsNone(page_progress_callback)
                forwarded.append(source_asset_publisher)
                return PdfSourceIRAdapter(
                    source_asset_publisher=source_asset_publisher
                )

            with (
                mock.patch(
                    "app.application.extraction.source_asset_objects."
                    "runtime_source_asset_publisher",
                    return_value=publisher,
                ),
                mock.patch(
                    "app.application.extraction.processing_visual_chain."
                    "build_composable_pdf_source_ir_adapter",
                    side_effect=build_adapter,
                ),
                mock.patch(
                    "app.application.extraction.processing_visual_chain."
                    "page_processing_receipts_from_adapter",
                    return_value=(),
                ),
            ):
                snapshot = build_source_ir_for_plan(source, plan=plan)

            self.assertEqual(forwarded, [publisher])
            self.assertEqual(published, [snapshot.assets[0].storage_key])

    def test_publisher_requires_post_put_stat_identity(self) -> None:
        payload = b"asset"
        store = _BadPostPutStore()

        with self.assertRaisesRegex(
            RuntimeError,
            "storage key is bound to different bytes",
        ):
            build_source_asset_publisher(store)(_asset(payload), payload)

        self.assertEqual(store.put_calls, 1)
        self.assertEqual(store.stat_calls, 2)

    def test_metadata_failure_leaves_shared_content_object_for_safe_retry(self) -> None:
        payload = b"shared-content-addressed-asset"
        asset = _asset(payload)
        registrations: list[dict[str, object]] = []

        def fail_registration(**_values: object) -> None:
            raise RuntimeError("metadata unavailable")

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            with self.assertRaisesRegex(RuntimeError, "metadata unavailable"):
                build_source_asset_publisher(
                    store,
                    register_metadata=fail_registration,
                )(asset, payload)

            # A content-addressed object may already be observed by another
            # worker.  Registration failure must leave it intact, not perform
            # an unsafe compensating delete.
            self.assertIsNotNone(store.stat(asset.storage_key))

            build_source_asset_publisher(
                store,
                register_metadata=lambda **values: registrations.append(values),
            )(asset, payload)
            self.assertEqual(len(registrations), 1)
            self.assertEqual(
                registrations[0]["storage_key"],
                asset.storage_key,
            )

    def test_prepublication_metadata_failure_does_not_create_object(self) -> None:
        payload = b"new-owned-content-addressed-asset"
        asset = _asset(payload)

        def reject_before_put(**_values: object) -> None:
            raise RuntimeError("reference registration unavailable")

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            with self.assertRaisesRegex(
                RuntimeError,
                "reference registration unavailable",
            ):
                build_source_asset_publisher(
                    store,
                    prepare_metadata=reject_before_put,
                )(asset, payload)

            self.assertIsNone(store.stat(asset.storage_key))

    def test_post_put_lost_lease_reclaims_new_object(self) -> None:
        payload = b"object-written-after-stale-attempt-reap"
        asset = _asset(payload)

        @contextmanager
        def deletion_claim(_storage_key: str):
            yield True

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            with (
                mock.patch.object(
                    source_asset_objects.db,
                    "claim_unreferenced_source_asset_object_deletion",
                    side_effect=deletion_claim,
                ),
                self.assertRaisesRegex(RuntimeError, "lost its job lease fence"),
            ):
                build_source_asset_publisher(
                    store,
                    prepare_metadata=lambda **_values: None,
                    confirm_metadata=lambda **_values: False,
                )(asset, payload)

            self.assertIsNone(store.stat(asset.storage_key))

    def test_gc_between_initial_stat_and_registration_cannot_mark_legacy(self) -> None:
        payload = b"asset-rebuilt-after-fenced-gc"
        asset = _asset(payload)
        prepared: list[dict[str, object]] = []
        confirmed: list[dict[str, object]] = []

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            store.put(
                key=asset.storage_key,
                source=io.BytesIO(payload),
                expected_sha256=asset.content_sha256,
                media_type=asset.media_type,
            )

            def prepare(**values: object) -> None:
                prepared.append(values)
                # Deterministic interleaving: the publisher's first stat saw
                # bytes, then fenced GC removed them before DB registration.
                store.delete(asset.storage_key)

            def confirm(**values: object) -> bool:
                confirmed.append(values)
                return True

            build_source_asset_publisher(
                store,
                prepare_metadata=prepare,
                confirm_metadata=confirm,
            )(asset, payload)

            self.assertIsNotNone(store.stat(asset.storage_key))
            self.assertNotIn("object_preexisted", prepared[0])
            self.assertNotIn("object_preexisted", confirmed[0])

    def test_result_artifact_post_put_lost_owner_is_reclaimed(self) -> None:
        source_sha256 = "a" * 64

        @contextmanager
        def deletion_claim(_storage_key: str):
            yield True

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            container = SimpleNamespace(object_store=store)
            staged: list[str] = []
            with (
                mock.patch.object(
                    source_asset_objects.RUNTIME_STATE,
                    "container",
                    container,
                ),
                mock.patch.object(
                    document_jobs.db,
                    "stage_extraction_result_artifact_reference",
                    side_effect=lambda **values: staged.append(
                        str(values["storage_key"])
                    ),
                ),
                mock.patch.object(
                    document_jobs.db,
                    "confirm_extraction_result_artifact_reference",
                    return_value=False,
                ),
                mock.patch.object(
                    document_jobs.db,
                    "abort_extraction_result_artifact_reference",
                    side_effect=lambda **_values: (),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "claim_unreferenced_extraction_result_artifact_deletion",
                    side_effect=deletion_claim,
                ),
                self.assertRaisesRegex(RuntimeError, "lost ownership"),
            ):
                document_jobs._persist_document_extraction_result_artifact(
                    {"status": "READY"},
                    project_id="project-a",
                    document_id="document-a",
                    source_sha256=source_sha256,
                    job_id="job-a",
                    attempt_generation=3,
                    worker_id="worker-a",
                )

            self.assertEqual(len(staged), 1)
            self.assertIsNone(store.stat(staged[0]))

    def test_durable_attempt_commits_staging_only_after_success(self) -> None:
        payload = b"successful-attempt-asset"
        asset = _asset(payload)
        events: list[tuple[str, object]] = []
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            container = SimpleNamespace(object_store=store)
            with (
                mock.patch.object(source_asset_objects, "RUNTIME_SETTINGS", settings),
                mock.patch.object(
                    source_asset_objects.RUNTIME_STATE,
                    "container",
                    container,
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "reap_stale_source_asset_staging_references",
                    return_value=(),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "list_unreferenced_managed_source_asset_objects",
                    return_value=(),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "register_source_asset_staging_reference",
                    side_effect=lambda **values: events.append(
                        ("stage", values["storage_key"])
                    ),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "confirm_source_asset_staging_reference",
                    return_value=True,
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "seal_source_asset_publication_attempt",
                    side_effect=lambda **values: (
                        events.append(("seal", values["attempt_generation"]))
                    ),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "abort_source_asset_publication_attempt",
                ) as abort,
            ):
                with source_asset_publication_attempt(
                    project_id="project-a",
                    document_id="document-a",
                    source_sha256="a" * 64,
                    job_id="job-a",
                    attempt_generation=3,
                    worker_id="worker-a",
                ) as attempt:
                    publisher = runtime_source_asset_publisher()
                    self.assertIsNotNone(publisher)
                    assert publisher is not None
                    publisher(asset, payload)
                    attempt.seal()

            self.assertEqual(
                events,
                [("stage", asset.storage_key), ("seal", 3)],
            )
            abort.assert_not_called()
            self.assertIsNotNone(store.stat(asset.storage_key))

    def test_failed_retry_aborts_only_its_staging_manifest(self) -> None:
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        previous_success = {"source-assets/existing-success.png"}

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            container = SimpleNamespace(object_store=store)
            with (
                mock.patch.object(source_asset_objects, "RUNTIME_SETTINGS", settings),
                mock.patch.object(
                    source_asset_objects.RUNTIME_STATE,
                    "container",
                    container,
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "reap_stale_source_asset_staging_references",
                    return_value=(),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "list_unreferenced_managed_source_asset_objects",
                    return_value=(),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "abort_source_asset_publication_attempt",
                    return_value=(),
                ) as abort,
                mock.patch.object(
                    source_asset_objects.db,
                    "seal_source_asset_publication_attempt",
                ) as seal,
            ):
                with self.assertRaisesRegex(RuntimeError, "parse failed"):
                    with source_asset_publication_attempt(
                        project_id="project-a",
                        document_id="document-a",
                        source_sha256="a" * 64,
                        job_id="job-retry",
                        attempt_generation=4,
                        worker_id="worker-a",
                    ):
                        raise RuntimeError("parse failed")

            abort.assert_called_once_with(
                job_id="job-retry",
                attempt_generation=4,
            )
            seal.assert_not_called()
            self.assertEqual(previous_success, {"source-assets/existing-success.png"})

    def test_shared_object_survives_first_document_release(self) -> None:
        payload = b"shared-across-two-documents"
        asset = _asset(payload)
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)
        remaining_refs = 2

        @contextmanager
        def deletion_claim(_storage_key: str):
            yield remaining_refs == 0

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            store.put(
                key=asset.storage_key,
                source=io.BytesIO(payload),
                expected_sha256=asset.content_sha256,
                media_type=asset.media_type,
            )
            container = SimpleNamespace(object_store=store)
            with (
                mock.patch.object(source_asset_objects, "RUNTIME_SETTINGS", settings),
                mock.patch.object(
                    source_asset_objects.RUNTIME_STATE,
                    "container",
                    container,
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "claim_unreferenced_source_asset_object_deletion",
                    side_effect=deletion_claim,
                ),
            ):
                remaining_refs = 1
                self.assertEqual(
                    release_source_asset_objects_after_document_delete(
                        (asset.storage_key,)
                    ),
                    0,
                )
                self.assertIsNotNone(store.stat(asset.storage_key))

                remaining_refs = 0
                self.assertEqual(
                    release_source_asset_objects_after_document_delete(
                        (asset.storage_key,)
                    ),
                    1,
                )
                self.assertIsNone(store.stat(asset.storage_key))

    def test_stale_attempt_recovery_retries_zero_ref_gc(self) -> None:
        payload = b"crashed-attempt-object"
        asset = _asset(payload)
        settings = SimpleNamespace(database_backend=DatabaseBackend.POSTGRESQL)

        @contextmanager
        def deletion_claim(_storage_key: str):
            yield True

        with tempfile.TemporaryDirectory() as directory:
            store = LocalObjectStore(Path(directory))
            store.put(
                key=asset.storage_key,
                source=io.BytesIO(payload),
                expected_sha256=asset.content_sha256,
                media_type=asset.media_type,
            )
            with (
                mock.patch.object(source_asset_objects, "RUNTIME_SETTINGS", settings),
                mock.patch.object(
                    source_asset_objects.RUNTIME_STATE,
                    "container",
                    SimpleNamespace(object_store=store),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "reap_stale_source_asset_staging_references",
                    return_value=(asset.storage_key,),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "list_unreferenced_managed_source_asset_objects",
                    return_value=(asset.storage_key,),
                ),
                mock.patch.object(
                    source_asset_objects.db,
                    "claim_unreferenced_source_asset_object_deletion",
                    side_effect=deletion_claim,
                ),
            ):
                self.assertEqual(recover_stale_source_asset_publications(), 1)

            self.assertIsNone(store.stat(asset.storage_key))

    def test_migration_protects_legacy_assets_and_tracks_durable_refcounts(
        self,
    ) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0022_source_asset_lifecycle.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("source_asset_references", migration)
        self.assertIn("source_asset_staging_references", migration)
        self.assertIn("source_asset_reference_counts", migration)
        self.assertIn("legacy_protected", migration)
        self.assertIn("WHERE object_kind = 'source-ir-asset'", migration)
        self.assertIn("REFERENCES stored_objects", migration)
        self.assertIn("REFERENCES runtime_jobs", migration)
        self.assertIn("REFERENCES sor_documents", migration)

    def test_asset_lifecycle_version_is_bound_into_result_runtime_hash(self) -> None:
        profile = build_local_rules_profile(project_id="project-1")
        plan = build_resolved_processing_plan(
            project_id="project-1",
            document_id="document-1",
            source_sha256="a" * 64,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=0,
        )
        current = document_jobs._document_extraction_result_runtime_sha256(plan)
        with mock.patch.object(
            document_jobs,
            "SOURCE_ASSET_PUBLICATION_RUNTIME_VERSION",
            "source-asset-publication/future",
        ):
            future = document_jobs._document_extraction_result_runtime_sha256(plan)
        self.assertNotEqual(current, future)
        with mock.patch.object(
            document_jobs,
            "EXTRACTION_RESULT_ARTIFACT_PUBLICATION_RUNTIME_VERSION",
            "extraction-result-artifact-publication/future",
        ):
            artifact_future = (
                document_jobs._document_extraction_result_runtime_sha256(plan)
            )
        self.assertNotEqual(current, artifact_future)

    def test_result_artifact_migration_is_durable_and_legacy_safe(self) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0023_extraction_result_artifact_lifecycle.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("extraction_result_artifact_managed_objects", migration)
        self.assertIn("extraction_result_artifact_staging_references", migration)
        self.assertIn("extraction_result_artifact_cas_operations", migration)
        self.assertIn("legacy_protected", migration)
        self.assertIn("REFERENCES runtime_jobs", migration)
        self.assertIn("REFERENCES sor_documents", migration)
        self.assertIn("expires_at", migration)

    def test_completed_page_staging_migration_is_attempt_scoped_and_nonformal(
        self,
    ) -> None:
        migration = (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "postgresql"
            / "0028_document_extraction_page_staging.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("document_extraction_page_staging_references", migration)
        self.assertIn("attempt_generation", migration)
        self.assertIn("processing_plan_sha256", migration)
        self.assertIn("result_runtime_sha256", migration)
        self.assertIn("confirmed_at", migration)
        self.assertIn("REFERENCES runtime_jobs", migration)
        self.assertIn("REFERENCES source_asset_managed_objects", migration)

    def test_durable_worker_rejects_changed_connection_before_any_work(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        document_profile = DocumentProcessingProfileV1(
            primary_parser="pymupdf",
            auto_escalation=False,
        )
        profile = build_project_processing_profile(
            project_id="project-1",
            document=document_profile,
            llm_provider="aliyun",
            knowledge_backend="none",
            revision=1,
        )
        plan = build_resolved_processing_plan(
            project_id="project-1",
            document_id="document-1",
            source_sha256=source_sha256,
            profile=profile,
            document=document_profile,
            llm_provider="aliyun",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf",
                llm_transport_provider="aliyun",
            ),
            connection_revision=22,
        )
        durable = SimpleNamespace(
            payload={
                "project_id": "project-1",
                "document_id": "document-1",
                "source_sha256": source_sha256,
                "processing_plan": plan.model_dump(mode="json"),
                "result_runtime_sha256": (
                    document_jobs._document_extraction_result_runtime_sha256(plan)
                ),
                "visual_strategy": processing_plan_visual_strategy(plan),
            },
            job_id="job-connection-mismatch",
        )
        with (
            mock.patch(
                "app.application.extraction.review_support."
                "_require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": source_sha256,
                },
            ),
            mock.patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=23,
            ),
            mock.patch(
                "app.application.extraction.provider_support."
                "_build_llm_shadow_semantic_provider",
                side_effect=AssertionError("provider must not be built"),
            ) as provider_builder,
            mock.patch.object(
                document_jobs,
                "_build_document_extraction_page_staging_callback",
                side_effect=AssertionError("staging must not start"),
            ) as staging_builder,
            self.assertRaises(document_jobs.NonRetryableJobError) as caught,
        ):
            document_jobs.run_durable_document_extraction(durable, object())

        self.assertEqual(
            caught.exception.error_code,
            "document_extraction_connection_revision_mismatch",
        )
        provider_builder.assert_not_called()
        staging_builder.assert_not_called()

    def test_durable_worker_commits_assets_only_after_result_artifact_exists(
        self,
    ) -> None:
        source_sha256 = "a" * 64
        profile = build_local_rules_profile(project_id="project-1")
        plan = build_resolved_processing_plan(
            project_id="project-1",
            document_id="document-1",
            source_sha256=source_sha256,
            profile=profile,
            document=profile.document,
            llm_provider="none",
            provider_bindings=ResolvedProviderBindingsV1(
                primary_parser_provider="local:pymupdf"
            ),
            connection_revision=0,
        )
        events: list[str] = []
        staged_bindings: list[dict[str, object]] = []
        completed_result = {
            "project_id": "project-1",
            "document_id": "document-1",
            "processing_plan": plan.model_dump(mode="json"),
        }

        class Context:
            def lease_fence(self):
                return SimpleNamespace(
                    job_id="job-1",
                    generation=7,
                    worker_id="worker-1",
                )

            def begin_publication(self):
                events.append("begin")

            def heartbeat(self, progress=None):
                return SimpleNamespace(cancel_requested=False)

            def cancellation_requested(self):
                return False

        class Attempt:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def seal(self):
                events.append("seal")

        durable = SimpleNamespace(
            payload={
                "project_id": "project-1",
                "document_id": "document-1",
                "source_sha256": source_sha256,
                "processing_plan": plan.model_dump(mode="json"),
                "result_runtime_sha256": (
                    document_jobs._document_extraction_result_runtime_sha256(plan)
                ),
                "visual_strategy": {"mode": "OFF"},
                "structure_revision_chain_sha256": "",
            },
            job_id="job-1",
        )

        def run_worker(**worker_kwargs):
            self.assertTrue(callable(worker_kwargs.get("page_staging_callback")))
            worker_kwargs["page_staging_callback"]({"page_number": 1})
            document_jobs._LLM_EXTRACTION_JOBS["document-1"].update(
                status="COMPLETED",
                result=completed_result,
            )

        with (
            mock.patch(
                "app.application.extraction.review_support._require_project_document",
                return_value={
                    "id": "document-1",
                    "project_id": "project-1",
                    "source_sha256": source_sha256,
                },
            ),
            mock.patch(
                "app.application.extraction.processing_pipeline."
                "processing_plan_visual_strategy",
                return_value={"mode": "OFF"},
            ),
            mock.patch.object(
                document_jobs,
                "_validated_structure_revisions_for_document_rerun",
                return_value=(),
            ),
            mock.patch.object(
                document_jobs,
                "get_runtime_connection_revision",
                return_value=0,
            ),
            mock.patch.object(
                document_jobs,
                "_llm_extraction_snapshot_from_durable",
                return_value={"job_id": "job-1"},
            ),
            mock.patch.object(
                document_jobs,
                "_run_local_structure_document_extraction_worker",
                side_effect=run_worker,
            ),
            mock.patch(
                "app.application.extraction.source_asset_objects."
                "source_asset_publication_attempt",
                return_value=Attempt(),
            ),
            mock.patch.object(
                document_jobs,
                "_persist_document_extraction_result_artifact",
                side_effect=lambda *_args, **_kwargs: (
                    events.append("artifact")
                    or {
                        "storage_key": "runtime/result.json",
                        "sha256": "b" * 64,
                    }
                ),
            ),
            mock.patch(
                "app.application.extraction.source_page_facts."
                "persist_document_extraction_staged_page",
                side_effect=lambda _candidate, **values: staged_bindings.append(
                    values
                ),
            ),
        ):
            result = document_jobs.run_durable_document_extraction(
                durable,
                Context(),
            )

        self.assertEqual(events, ["begin", "artifact", "seal"])
        self.assertEqual(result["storage_key"], "runtime/result.json")
        self.assertEqual(len(staged_bindings), 1)
        self.assertEqual(staged_bindings[0]["job_id"], "job-1")
        self.assertEqual(staged_bindings[0]["attempt_generation"], 7)
        self.assertEqual(staged_bindings[0]["worker_id"], "worker-1")
        self.assertEqual(
            staged_bindings[0]["processing_plan_sha256"],
            plan.canonical_sha256,
        )

    def test_loader_rejects_declared_hash_or_size_mismatch(self) -> None:
        payload = b"verified-payload"
        expected_sha256 = hashlib.sha256(payload).hexdigest()
        cases = (
            _DeclaredObjectStore(
                payload=payload,
                sha256="0" * 64,
                size=len(payload),
            ),
            _DeclaredObjectStore(
                payload=payload,
                sha256=expected_sha256,
                size=len(payload) + 1,
            ),
        )
        for store in cases:
            with self.subTest(store=store):
                self.assertIsNone(
                    build_source_asset_loader(store)(
                        "source-assets/00/object.png",
                        expected_sha256,
                        1024,
                    )
                )


if __name__ == "__main__":
    unittest.main()
