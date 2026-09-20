from __future__ import annotations

# Test runtime isolation must be activated before importing application modules.
# ruff: noqa: E402

if __package__:
    from .runtime_isolation import activate_test_runtime
else:
    from runtime_isolation import activate_test_runtime

activate_test_runtime()

import hashlib
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import fitz
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image as PILImage
from pydantic import ValidationError

from app import db
from app.adapters.persistence.sql_source_capture import (
    SqlSourceCaptureRepository,
    SqlSourceFactsReader,
)
from app.adapters.persistence.sql_reading import SqlReadingRepository
from app.adapters.persistence.sql_source_visual_capture import (
    SqlSourceVisualCaptureRepository,
)
from app.adapters.storage.local_object_store import LocalObjectStore
from app.application import source_visual_capture as visual_capture_app
from app.application.build_source_ir import build_source_ir
from app.application.errors import ApplicationError
from app.api.errors import application_error_handler
from app.api.routers import source_capture as source_capture_router
from app.application.reading import ReadingService
from app.application.source_capture import capture_current_document_source
from app.application.source_visual_capture import (
    SourceVisualCaptureService,
    build_reading_visual_provider,
)
from app.domain.source_visual_capture import (
    MAX_VISUAL_CAPTURE_PAGES,
    CaptureQualityManifestV1,
    CaptureQualityPageV1,
    CaptureQualityStatus,
    PageCaptureStatus,
    PageReadableMode,
    RequiredVisualCapability,
    VisualCaptureGenerationV1,
    VisualPageContentState,
    visual_observation_text,
    stable_visual_capture_generation_id,
    visual_text_run_from_observation,
)
from app.domain.extraction.source_ir import (
    NativeDocumentFormat,
    NativeLocator,
    SourceDiagnostic,
    SourceNode,
    SourceNodeKind,
    stable_node_id,
)
from app.domain.reading import (
    ReadingAction,
    ReadingActionRequestV1,
    ReadingConversationCreateRequestV1,
    ReadingMessageCreateRequestV1,
    ReadingNoteCreateRequestV1,
    ReadingPrepareRequestV1,
    ReadingSourceRefV1,
)
from app.domain.source_visual_text import (
    VISUAL_CONTENT_TYPES,
    VisualLayoutTable,
    VisualLayoutTableCell,
    VisualTextObservation,
    VisualTextSnapshot,
    stable_visual_text_observation_id,
    stable_visual_text_snapshot_id,
)
from app.ports.source_capture_repository import SourceCaptureRecord
from app.ports.source_visual_capture_repository import (
    SourceVisualCaptureRepositoryConflict,
    VisualPageFailureRecord,
)
from app.ports.reading_provider import (
    FULL_EFFECTIVE_TEXT_DISCLOSURE,
    ReadingChatProviderResult,
    ReadingProviderResult,
)
from app.ports.visual_text_provider import (
    VisualTextProviderRejected,
    VisualTextProviderRejectionCode,
)


_NOW = datetime(2026, 8, 18, 8, 0, tzinfo=timezone.utc)


class _FakeVisualProvider:
    provider_id = "aliyun-bailian-qwen-page-layout"
    provider_version = "test-qwen-page-layout/1"

    def __init__(
        self,
        *,
        fail: bool = False,
        content: str = "text",
        provider_version: str | None = None,
    ) -> None:
        self.fail = fail
        self.content = content
        if provider_version is not None:
            self.provider_version = provider_version
        self.calls = []

    def recognize_page(self, page_input):
        self.calls.append(page_input)
        if self.fail:
            raise VisualTextProviderRejected(
                VisualTextProviderRejectionCode.PROVIDER_FAILED,
                "bounded provider failure",
            )
        observations = ()
        if self.content != "blank":
            observation_id = stable_visual_text_observation_id(
                source_sha256=page_input.source_sha256,
                page_image_sha256=page_input.page_image_sha256,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_number=page_input.page_number,
                observation_index=0,
                bbox=page_input.page_bbox,
            )
            observation = VisualTextObservation(
                observation_id=observation_id,
                observation_index=0,
                source_sha256=page_input.source_sha256,
                page_number=page_input.page_number,
                bbox=page_input.page_bbox,
                raw_text=("" if self.content == "figure" else "视觉识别文字"),
                content_type=("FIGURE" if self.content == "figure" else "TEXT"),
                confidence=0.99,
                provider_id=self.provider_id,
                provider_version=self.provider_version,
                page_image_sha256=page_input.page_image_sha256,
            )
            observations = (observation,)
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
            evaluated_content_types=VISUAL_CONTENT_TYPES,
            observations=observations,
        )


class _FakeReadingProvider:
    def __init__(self) -> None:
        self.action_calls = []
        self.chat_calls = []

    def generate(self, *, action, source_text, target_language):
        self.action_calls.append((action, source_text, target_language))
        return ReadingProviderResult(
            output_text=f"阅读动作：{source_text}",
            provider_id="test-reading",
            model_name="test-reading-model",
            prompt_version="reading-action/v1",
        )

    def generate_chat(self, *, question, history, context_chunks, coverage_mode):
        self.chat_calls.append(
            {
                "question": question,
                "history": history,
                "context_chunks": context_chunks,
                "coverage_mode": coverage_mode,
            }
        )
        citation_ids = (context_chunks[0].citation_id,) if context_chunks else ()
        return ReadingChatProviderResult(
            output_text=(
                f"仅依据所选内容，文档仍有未识别页【{citation_ids[0]}】"
                if coverage_mode == "SELECTED_CONTEXT" and citation_ids
                else (
                    f"{FULL_EFFECTIVE_TEXT_DISCLOSURE}【{citation_ids[0]}】"
                    if coverage_mode == "FULL_EFFECTIVE_TEXT" and citation_ids
                    else f"{FULL_EFFECTIVE_TEXT_DISCLOSURE}，原文未提供"
                )
                if coverage_mode == "FULL_EFFECTIVE_TEXT"
                else f"阅读回答【{citation_ids[0]}】"
                if citation_ids
                else "原文未提供"
            ),
            citation_ids=citation_ids,
            provider_id="test-reading",
            model_name="test-reading-model",
            prompt_version="reading-chat/v1",
        )


class _BlockingReadingProvider(_FakeReadingProvider):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def generate_chat(self, **kwargs):
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise TimeoutError("test did not release reading provider")
        return super().generate_chat(**kwargs)


class _BundleSwitchingReadingRepository(SqlReadingRepository):
    def __init__(self) -> None:
        super().__init__()
        self.before_artifact = None
        self.before_note = None
        self.before_chat = None

    @staticmethod
    def _fire(callback) -> None:
        if callback is not None:
            callback()

    def append_artifact(self, artifact, *, expected_effective_bundle_id):
        callback, self.before_artifact = self.before_artifact, None
        self._fire(callback)
        return super().append_artifact(
            artifact,
            expected_effective_bundle_id=expected_effective_bundle_id,
        )

    def append_note(self, note, *, expected_effective_bundle_id):
        callback, self.before_note = self.before_note, None
        self._fire(callback)
        return super().append_note(
            note,
            expected_effective_bundle_id=expected_effective_bundle_id,
        )

    def append_chat_turn(self, **kwargs):
        callback, self.before_chat = self.before_chat, None
        self._fire(callback)
        return super().append_chat_turn(**kwargs)


class _MissingSourceFactsReader:
    def get_capture(self, **_kwargs):
        return None


class _EmptyVisualRepository:
    def get_active_generation(self, **_kwargs):
        return None

    def get_page(self, **_kwargs):
        return None

    def list_pages(self, _generation_id):
        return ()

    def latest_page_failure(self, **_kwargs):
        return None

    def append_generation(self, _generation):
        raise AssertionError("read-only manifest must not append")

    def append_page(self, _page):
        raise AssertionError("read-only manifest must not append")

    def append_page_failure(self, _failure: VisualPageFailureRecord):
        raise AssertionError("read-only manifest must not append")

    def purge_document(self, **_kwargs):
        raise AssertionError("read-only manifest must not purge")


class SourceVisualCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(db, "DB_PATH", self.root / "app.db"))
        db.init_db()
        self.project = db.create_project("Visual capture", "Customer")
        self.source_repository = SqlSourceCaptureRepository()
        self.visual_repository = SqlSourceVisualCaptureRepository()
        self.object_store = LocalObjectStore(self.root / "objects")
        self.pdf_path = self._write_mixed_pdf()
        self.document = self._add_document(self.pdf_path)
        self.capture = capture_current_document_source(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            repository=self.source_repository,
            object_store=self.object_store,
            clock=lambda: _NOW,
        )

    def tearDown(self) -> None:
        self.stack.close()

    def _service(self, provider: _FakeVisualProvider) -> SourceVisualCaptureService:
        return SourceVisualCaptureService(
            visual_repository=self.visual_repository,
            source_facts_reader=SqlSourceFactsReader(),
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            provider_factory=lambda: provider,
            clock=lambda: _NOW,
        )

    def test_risk_page_capture_is_resumable_and_public_dto_is_not_duplicated(self) -> None:
        provider = _FakeVisualProvider()
        service = self._service(provider)

        before = service.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(before.status, CaptureQualityStatus.PARTIAL)
        self.assertEqual(before.visual_required_page_count, 1)
        self.assertEqual(before.pages[0].capture_status, PageCaptureStatus.PENDING)
        self.assertEqual(before.pages[1].readable_mode, PageReadableMode.NATIVE)
        self.assertEqual(provider.calls, [])

        first = service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        second = service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(provider.calls), 1)
        self.assertFalse(hasattr(first, "visual_text_snapshot"))
        self.assertEqual(first.width_px, round(first.page_bbox[2] * 2))
        self.assertEqual(first.height_px, round(first.page_bbox[3] * 2))
        self.assertEqual(len(first.page_image_sha256), 64)
        after = service.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(after.status, CaptureQualityStatus.READY)
        self.assertNotEqual(before.effective_bundle_id, after.effective_bundle_id)
        self.assertEqual(after.pages[0].readable_mode, PageReadableMode.VISUAL)
        self.assertEqual(first.provider_id, "aliyun-bailian-qwen-page-layout")
        self.assertEqual(after.pages[0].provider_id, first.provider_id)
        replay_provider = _FakeVisualProvider()
        replay_current_generation = SourceVisualCaptureService(
            visual_repository=self.visual_repository,
            source_facts_reader=SqlSourceFactsReader(),
            provider_factory=lambda: replay_provider,
        ).capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        self.assertEqual(replay_current_generation, first)
        self.assertEqual(replay_provider.calls, [])

        with self.assertRaises(ApplicationError) as rejected:
            service.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=2,
            )
        self.assertEqual(rejected.exception.code, "visual_page_not_required")
        self.assertEqual(len(provider.calls), 1)

    def test_neutral_source_capture_api_is_read_only_on_get_and_resumable_on_post(self) -> None:
        provider = _FakeVisualProvider()
        service = self._service(provider)
        app = FastAPI()
        app.add_exception_handler(ApplicationError, application_error_handler)
        app.include_router(source_capture_router.build())
        prefix = (
            f"/api/projects/{self.project['id']}/sor-documents/"
            f"{self.document['id']}/source-capture"
        )
        with patch.object(
            visual_capture_app,
            "SourceVisualCaptureService",
            return_value=service,
        ):
            client = TestClient(app)
            quality = client.get(f"{prefix}/quality")
            self.assertEqual(quality.status_code, 200)
            self.assertEqual(quality.json()["status"], "PARTIAL")
            self.assertEqual(provider.calls, [])

            captured = client.post(f"{prefix}/visual/pages/1")
            replay = client.post(f"{prefix}/visual/pages/1")
            fetched = client.get(f"{prefix}/visual/pages/1")
        self.assertEqual(captured.status_code, 200)
        self.assertEqual(replay.json(), captured.json())
        self.assertEqual(fetched.json(), captured.json())
        self.assertEqual(len(provider.calls), 1)
        self.assertNotIn("visual_text_snapshot", captured.json())
        self.assertEqual(
            captured.json()["visual_snapshot_id"],
            fetched.json()["visual_snapshot_id"],
        )

    def test_failed_capture_stays_partial_and_records_safe_failure(self) -> None:
        provider = _FakeVisualProvider(fail=True)
        service = self._service(provider)

        with self.assertRaises(ApplicationError) as failure:
            service.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )
        self.assertEqual(failure.exception.code, "visual_page_capture_failed")
        manifest = service.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(manifest.status, CaptureQualityStatus.PARTIAL)
        self.assertEqual(manifest.failed_page_count, 1)
        self.assertEqual(
            manifest.pages[0].failure_code,
            "pdf_visual_text_provider_failed",
        )
        self.assertEqual(manifest.pages[0].readable_mode, PageReadableMode.NONE)

    def test_verified_empty_layout_page_is_ready_without_selectable_runs(self) -> None:
        service = self._service(_FakeVisualProvider(content="blank"))

        page = service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        self.assertEqual(page.content_state, VisualPageContentState.VERIFIED_EMPTY)
        self.assertEqual(page.runs, ())
        manifest = service.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(manifest.status, CaptureQualityStatus.READY)
        self.assertEqual(manifest.pages[0].readable_mode, PageReadableMode.VISUAL)
        self.assertEqual(manifest.visual_ready_page_count, 1)
        reading_provider = _FakeReadingProvider()
        reading_service = ReadingService(
            repository=SqlReadingRepository(),
            provider_factory=lambda: reading_provider,
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            clock=lambda: _NOW,
        )
        reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="verified empty"),
        )
        turn = reading_service.send_message(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            conversation_id=conversation.conversation_id,
            request=ReadingMessageCreateRequestV1(
                source_sha256=self.document["source_sha256"],
                content="概括可读文字",
            ),
        )
        self.assertEqual(turn.retrieval.coverage_mode, "FULL_EFFECTIVE_TEXT")
        self.assertEqual(turn.retrieval.covered_page_numbers, (1, 2))
        self.assertIn(FULL_EFFECTIVE_TEXT_DISCLOSURE, turn.assistant_message.content)

    def test_pure_figure_page_does_not_claim_visual_text_readiness(self) -> None:
        service = self._service(_FakeVisualProvider(content="figure"))

        with self.assertRaises(ApplicationError) as failure:
            service.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )
        self.assertEqual(failure.exception.code, "visual_page_capture_failed")
        self.assertEqual(
            failure.exception.detail["failure_code"],
            "visual_image_semantics_not_supported",
        )
        manifest = service.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(manifest.status, CaptureQualityStatus.PARTIAL)
        self.assertEqual(manifest.pages[0].readable_mode, PageReadableMode.NONE)
        self.assertEqual(
            manifest.pages[0].failure_code,
            "visual_image_semantics_not_supported",
        )

    def test_concurrent_same_page_capture_makes_one_provider_call(self) -> None:
        provider = _FakeVisualProvider()
        service = self._service(provider)

        def capture_once():
            return service.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(capture_once)
            second_future = executor.submit(capture_once)
            first = first_future.result(timeout=5)
            second = second_future.result(timeout=5)
        self.assertEqual(first, second)
        self.assertEqual(len(provider.calls), 1)

        stored = self.visual_repository.get_page(
            generation_id=first.generation_id,
            page_number=1,
        )
        assert stored is not None
        replay = stored.model_copy(
            update={"created_at_utc": stored.created_at_utc + timedelta(seconds=1)}
        )
        self.assertEqual(self.visual_repository.append_page(replay), stored)

    def test_provider_upgrade_activates_new_generation_and_rejects_old_late_writes(self) -> None:
        provider_a = _FakeVisualProvider(provider_version="layout-provider/A")
        service_a = self._service(provider_a)
        page_a = service_a.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        internal_a = self.visual_repository.get_page(
            generation_id=page_a.generation_id,
            page_number=1,
        )
        assert internal_a is not None

        reading_repository = SqlReadingRepository()
        reading_service = ReadingService(
            repository=reading_repository,
            provider_factory=lambda: _FakeReadingProvider(),
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            clock=lambda: _NOW,
        )
        old_context = reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        old_conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="provider A"),
        )

        provider_b = _FakeVisualProvider(provider_version="layout-provider/B")
        page_b = self._service(provider_b).capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        self.assertNotEqual(page_b.generation_id, page_a.generation_id)
        self.assertNotEqual(page_b.visual_snapshot_id, page_a.visual_snapshot_id)
        self.assertEqual(len(provider_b.calls), 1)
        current_manifest = service_a.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(current_manifest.status, CaptureQualityStatus.READY)
        self.assertEqual(
            current_manifest.active_visual_generation_id,
            page_b.generation_id,
        )
        self.assertNotEqual(current_manifest.effective_bundle_id, old_context.effective_bundle_id)
        bundle_b = current_manifest.effective_bundle_id
        with self.assertRaises(ApplicationError) as downgrade:
            service_a.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )
        self.assertEqual(
            downgrade.exception.code,
            "visual_capture_generation_superseded",
        )
        after_downgrade = service_a.quality(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        self.assertEqual(
            after_downgrade.active_visual_generation_id,
            page_b.generation_id,
        )
        self.assertEqual(after_downgrade.effective_bundle_id, bundle_b)
        self.assertEqual(len(provider_a.calls), 1)

        with self.assertRaises(SourceVisualCaptureRepositoryConflict):
            self.visual_repository.append_page(internal_a)
        with self.assertRaises(SourceVisualCaptureRepositoryConflict):
            self.visual_repository.append_page_failure(
                VisualPageFailureRecord(
                    attempt_id="late-provider-a-failure",
                    generation_id=page_a.generation_id,
                    project_id=self.project["id"],
                    sor_document_id=self.document["id"],
                    source_sha256=self.document["source_sha256"],
                    page_number=1,
                    failure_code="late_provider_a",
                    created_at_utc=_NOW + timedelta(seconds=1),
                )
            )

        with self.assertRaises(ApplicationError) as stale_send:
            reading_service.send_message(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                conversation_id=old_conversation.conversation_id,
                request=ReadingMessageCreateRequestV1(
                    source_sha256=self.document["source_sha256"],
                    content="继续",
                ),
            )
        self.assertEqual(stale_send.exception.code, "reading_context_bundle_changed")
        reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        conversations = reading_repository.list_conversations(
            old_context.reading_session_id
        )
        old = next(
            item
            for item in conversations
            if item.conversation_id == old_conversation.conversation_id
        )
        self.assertEqual(old.status.value, "STALE")

    def test_get_quality_is_read_only_when_base_capture_is_missing(self) -> None:
        calls = []
        service = SourceVisualCaptureService(
            visual_repository=_EmptyVisualRepository(),
            source_facts_reader=_MissingSourceFactsReader(),
            source_capture=lambda **kwargs: calls.append(kwargs),
            current_document=lambda **_kwargs: {
                "source_sha256": self.document["source_sha256"]
            },
        )

        with self.assertRaises(ApplicationError) as missing:
            service.quality(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
            )
        self.assertEqual(missing.exception.code, "source_capture_not_prepared")
        self.assertEqual(calls, [])

    def test_visual_page_is_document_scoped_and_purged_with_document(self) -> None:
        service = self._service(_FakeVisualProvider())
        service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        second_document = self._add_document(self.pdf_path, file_name="same-bytes.pdf")
        capture_current_document_source(
            project_id=self.project["id"],
            sor_document_id=second_document["id"],
            repository=self.source_repository,
            object_store=self.object_store,
            clock=lambda: _NOW,
        )
        with self.assertRaises(ApplicationError) as missing:
            service.get_page(
                project_id=self.project["id"],
                sor_document_id=second_document["id"],
                page_number=1,
            )
        self.assertEqual(missing.exception.code, "visual_page_not_found")

        db.delete_sor_document(self.document["id"])
        with db.get_conn() as connection:
            for table in (
                "source_visual_capture_generations",
                "source_visual_capture_bindings",
                "source_visual_page_snapshots",
                "source_visual_page_attempts",
            ):
                count = connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                self.assertEqual(int(count), 0)

    def test_payload_hash_tampering_is_rejected(self) -> None:
        service = self._service(_FakeVisualProvider())
        page = service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        with db.get_conn() as connection:
            connection.execute(
                """
                UPDATE source_visual_page_snapshots
                SET payload_json = '{}'
                WHERE generation_id = ? AND page_number = 1
                """,
                (page.generation_id,),
            )
        with self.assertRaisesRegex(RuntimeError, "payload hash mismatch"):
            self.visual_repository.get_page(
                generation_id=page.generation_id,
                page_number=1,
            )

    def test_manifest_status_cannot_overclaim_unreadable_pages(self) -> None:
        pending = CaptureQualityPageV1(
            page_number=1,
            readable_mode=PageReadableMode.NONE,
            capture_status=PageCaptureStatus.PENDING,
            native_character_count=0,
            native_cjk_character_count=0,
            native_selectable=False,
            risk_codes=("pdf_full_page_image_without_visible_text",),
            required_capability=RequiredVisualCapability.LAYOUT,
        )
        with self.assertRaisesRegex(ValidationError, "manifest status"):
            CaptureQualityManifestV1(
                project_id="project",
                sor_document_id="document",
                source_sha256="a" * 64,
                base_capture_id="capture",
                base_source_ir_id="source-ir",
                effective_bundle_id="bundle",
                status=CaptureQualityStatus.READY,
                page_count=1,
                native_ready_page_count=0,
                visual_required_page_count=1,
                visual_ready_page_count=0,
                failed_page_count=0,
                pages=(pending,),
            )
        with self.assertRaisesRegex(ValidationError, "trusted selectable"):
            CaptureQualityPageV1(
                page_number=1,
                readable_mode=PageReadableMode.NATIVE,
                capture_status=PageCaptureStatus.NOT_REQUIRED,
                native_character_count=0,
                native_cjk_character_count=0,
                native_selectable=False,
                required_capability=RequiredVisualCapability.NONE,
            )

    def test_visual_capture_page_limit_is_consistent_and_structured(self) -> None:
        required_pages = tuple(range(1, 258))
        generation_id = stable_visual_capture_generation_id(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            base_capture_id=self.capture.capture_id,
            capture_profile_id="reading-visual-capture/v1",
            risk_policy_version="pdf-native-readability-risk/v1",
            renderer_id="pymupdf-annotation-free-page-render",
            renderer_version="2x/v1",
            provider_id="layout-provider",
            provider_version="layout-provider/1",
            required_page_numbers=required_pages,
        )
        generation = VisualCaptureGenerationV1(
            generation_id=generation_id,
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            source_sha256=self.document["source_sha256"],
            base_capture_id=self.capture.capture_id,
            base_source_ir_id=self.capture.source_ir.source_ir_id,
            provider_id="layout-provider",
            provider_version="layout-provider/1",
            required_page_numbers=required_pages,
            created_at_utc=_NOW,
        )
        self.assertEqual(len(generation.required_page_numbers), 257)

        oversized_ir = self.capture.source_ir.model_copy(
            update={
                "diagnostics": (
                    *self.capture.source_ir.diagnostics,
                    SourceDiagnostic(
                        code="oversized-document-boundary",
                        message="test-only page-count boundary",
                        native_locator=NativeLocator(
                            format=NativeDocumentFormat.PDF,
                            page_number=MAX_VISUAL_CAPTURE_PAGES + 1,
                            block_index=0,
                        ),
                    ),
                )
            }
        )
        oversized_capture = SourceCaptureRecord(
            capture_id=self.capture.capture_id,
            project_id=self.capture.project_id,
            sor_document_id=self.capture.sor_document_id,
            source_sha256=self.capture.source_sha256,
            source_ir=oversized_ir,
            created_at_utc=self.capture.created_at_utc,
        )
        with self.assertRaises(ApplicationError) as exceeded:
            visual_capture_app.build_capture_quality_manifest(
                oversized_capture,
                self.visual_repository,
            )
        self.assertEqual(exceeded.exception.code, "visual_capture_page_limit_exceeded")
        self.assertEqual(
            exceeded.exception.detail["maximum_page_count"],
            MAX_VISUAL_CAPTURE_PAGES,
        )

    def test_layout_table_cells_become_selectable_canonical_visual_text(self) -> None:
        source_sha256 = "a" * 64
        image_sha256 = "b" * 64
        bbox = (0.0, 0.0, 400.0, 180.0)
        observation_id = stable_visual_text_observation_id(
            source_sha256=source_sha256,
            page_image_sha256=image_sha256,
            provider_id="layout-provider",
            provider_version="layout-provider/1",
            page_number=1,
            observation_index=0,
            bbox=bbox,
        )
        observation = VisualTextObservation(
            observation_id=observation_id,
            observation_index=0,
            source_sha256=source_sha256,
            page_number=1,
            bbox=bbox,
            raw_text="",
            content_type="TABLE",
            layout_table=VisualLayoutTable(
                row_count=2,
                column_count=2,
                header_row_count=1,
                cells=(
                    VisualLayoutTableCell(row=0, column=0, raw_text="参数"),
                    VisualLayoutTableCell(row=0, column=1, raw_text="限值"),
                    VisualLayoutTableCell(row=1, column=0, raw_text="电压"),
                    VisualLayoutTableCell(row=1, column=1, raw_text="14 V"),
                ),
            ),
            confidence=0.98,
            provider_id="layout-provider",
            provider_version="layout-provider/1",
            page_image_sha256=image_sha256,
        )
        expected = "参数\t限值\n电压\t14 V"
        self.assertEqual(visual_observation_text(observation), expected)
        run = visual_text_run_from_observation(observation)
        self.assertEqual(run.text, expected)
        self.assertEqual(run.observation_id, observation_id)
        self.assertEqual(run.bbox, bbox)

    def test_full_page_scan_with_only_page_number_is_visual_risk_but_native_body_is_not(self) -> None:
        low_path = self._write_full_page_image_text_pdf(
            "scan-page-number.pdf",
            lines=("1",),
        )
        native_path = self._write_full_page_image_text_pdf(
            "background-with-native-body.pdf",
            lines=tuple(
                f"Native selectable body line {index}: operating condition and acceptance."
                for index in range(1, 9)
            ),
        )

        low_ir = build_source_ir(low_path, clock=lambda: _NOW)
        native_ir = build_source_ir(native_path, clock=lambda: _NOW)
        risk_code = "pdf_full_page_image_with_low_native_text_coverage"
        self.assertIn(risk_code, {item.code for item in low_ir.diagnostics})
        self.assertNotIn(risk_code, {item.code for item in native_ir.diagnostics})
        self.assertEqual(low_ir.status.value, "COMPLETE")
        self.assertEqual(low_ir.visual_text_snapshots, ())
        low_capture = SourceCaptureRecord(
            capture_id="capture-low-native-text",
            project_id=self.project["id"],
            sor_document_id="low-native-text-document",
            source_sha256=low_ir.source_sha256,
            source_ir=low_ir,
            created_at_utc=_NOW,
        )
        native_capture = SourceCaptureRecord(
            capture_id="capture-native-body",
            project_id=self.project["id"],
            sor_document_id="native-body-document",
            source_sha256=native_ir.source_sha256,
            source_ir=native_ir,
            created_at_utc=_NOW,
        )
        low_manifest = visual_capture_app.build_capture_quality_manifest(
            low_capture,
            self.visual_repository,
        )
        native_manifest = visual_capture_app.build_capture_quality_manifest(
            native_capture,
            self.visual_repository,
        )
        self.assertEqual(
            low_manifest.pages[0].required_capability,
            RequiredVisualCapability.OCR,
        )
        self.assertEqual(low_manifest.pages[0].readable_mode, PageReadableMode.NONE)
        self.assertEqual(
            native_manifest.pages[0].required_capability,
            RequiredVisualCapability.NONE,
        )
        self.assertEqual(
            native_manifest.pages[0].readable_mode,
            PageReadableMode.NATIVE,
        )

    def test_reading_visual_provider_ignores_extraction_route_settings(self) -> None:
        pdf_provider = build_reading_visual_provider(
            environment={
                "REQPILOT_READING_ALIYUN_API_KEY": "reading-owned-key",
                "REQPILOT_READING_ALIYUN_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                "REQPILOT_EXTRACTION_VISUAL_PROVIDER": "forbidden-provider",
                "REQPILOT_PROCESSING_PROFILE": "forbidden-profile",
            }
        )
        self.assertEqual(pdf_provider._model, "qwen3.8-max")
        self.assertEqual(pdf_provider._timeout_seconds, 300.0)
        self.assertEqual(pdf_provider._max_retries, 0)
        self.assertEqual(
            pdf_provider.provider_id,
            "aliyun-bailian-qwen3.8-max-pdf-page-parsing",
        )
        self.assertIn(
            "task=pdf_understanding.single_page_parse",
            pdf_provider.provider_version,
        )

        provider = build_reading_visual_provider(
            environment={
                "REQPILOT_READING_VISUAL_PROVIDER": "bailian-qwen-page-layout",
                "REQPILOT_READING_VISUAL_MODEL": "qwen3.6-flash",
                "REQPILOT_READING_VISUAL_TIMEOUT_SECONDS": "42",
                "REQPILOT_READING_VISUAL_MAX_RETRIES": "1",
                "REQPILOT_READING_ALIYUN_API_KEY": "reading-owned-key",
                "REQPILOT_READING_ALIYUN_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                "REQPILOT_READING_AI_TIMEOUT_SECONDS": "3",
                "REQPILOT_EXTRACTION_VISUAL_PROVIDER": "forbidden-provider",
                "REQPILOT_PROCESSING_PROFILE": "forbidden-profile",
            }
        )
        self.assertEqual(provider._model, "qwen3.6-flash")
        self.assertEqual(provider._timeout_seconds, 42.0)
        self.assertEqual(provider._max_retries, 1)
        self.assertEqual(provider.provider_id, "aliyun-bailian-qwen-page-layout")
        self.assertIn("layout", provider.provider_version)

        ocr_provider = build_reading_visual_provider(
            environment={
                "REQPILOT_READING_VISUAL_PROVIDER": "bailian-qwen-ocr",
                "REQPILOT_READING_ALIYUN_API_KEY": "reading-owned-key",
                "REQPILOT_READING_ALIYUN_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
                "REQPILOT_EXTRACTION_VISUAL_PROVIDER": "forbidden-provider",
                "REQPILOT_PROCESSING_PROFILE": "forbidden-profile",
            }
        )
        self.assertEqual(ocr_provider._model, "qwen3.5-ocr")
        self.assertEqual(ocr_provider._max_retries, 1)
        self.assertEqual(ocr_provider.provider_id, "aliyun-bailian-qwen-ocr")
        self.assertIn("advanced_recognition", ocr_provider.provider_version)
        self.assertTrue(
            visual_capture_app._provider_satisfies_capability(
                ocr_provider,
                RequiredVisualCapability.OCR,
            )
        )
        self.assertFalse(
            visual_capture_app._provider_satisfies_capability(
                ocr_provider,
                RequiredVisualCapability.LAYOUT,
            )
        )
        self.assertTrue(
            visual_capture_app._provider_satisfies_capability(
                provider,
                RequiredVisualCapability.LAYOUT,
            )
        )
        self.assertTrue(
            visual_capture_app._provider_satisfies_capability(
                pdf_provider,
                RequiredVisualCapability.OCR,
            )
        )
        self.assertTrue(
            visual_capture_app._provider_satisfies_capability(
                pdf_provider,
                RequiredVisualCapability.LAYOUT,
            )
        )

        legacy_ocr_provider = build_reading_visual_provider(
            environment={
                "REQPILOT_READING_VISUAL_PROVIDER": "bailian-qwen-ocr",
                "REQPILOT_READING_VISUAL_MAX_RETRIES": "5",
                "REQPILOT_READING_ALIYUN_API_KEY": "reading-owned-key",
                "REQPILOT_READING_ALIYUN_BASE_URL": (
                    "https://dashscope.aliyuncs.com/compatible-mode/v1"
                ),
            }
        )
        self.assertEqual(legacy_ocr_provider._max_retries, 1)

        with self.assertRaises(ApplicationError) as retry_error:
            build_reading_visual_provider(
                environment={
                    "REQPILOT_READING_VISUAL_PROVIDER": (
                        "bailian-qwen3.8-max-pdf-parsing"
                    ),
                    "REQPILOT_READING_VISUAL_MAX_RETRIES": "1",
                    "REQPILOT_READING_ALIYUN_API_KEY": "reading-owned-key",
                    "REQPILOT_READING_ALIYUN_BASE_URL": (
                        "https://dashscope.aliyuncs.com/compatible-mode/v1"
                    ),
                }
            )
        self.assertEqual(
            retry_error.exception.code,
            "reading_visual_configuration_invalid",
        )

    def test_visual_risk_page_native_heading_is_withheld_from_reading_map(self) -> None:
        risk_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=1,
            block_index=0,
            bbox=(0.0, 0.0, 400.0, 500.0),
        )
        risk_heading_id = stable_node_id(
            source_sha256=self.capture.source_sha256,
            adapter_id=self.capture.source_ir.adapter_id,
            adapter_version=self.capture.source_ir.adapter_version,
            kind=SourceNodeKind.HEADING,
            native_locator=risk_locator,
        )
        native_locator = NativeLocator(
            format=NativeDocumentFormat.PDF,
            page_number=2,
            block_index=0,
            bbox=(20.0, 20.0, 300.0, 50.0),
        )
        native_heading_id = stable_node_id(
            source_sha256=self.capture.source_sha256,
            adapter_id=self.capture.source_ir.adapter_id,
            adapter_version=self.capture.source_ir.adapter_version,
            kind=SourceNodeKind.HEADING,
            native_locator=native_locator,
        )
        source_ir = self.capture.source_ir.model_copy(
            update={
                "nodes": (
                    SourceNode(
                        source_node_id=risk_heading_id,
                        kind=SourceNodeKind.HEADING,
                        source_order=0,
                        raw_text="1 扫描伪章节",
                        native_locator=risk_locator,
                    ),
                    SourceNode(
                        source_node_id=native_heading_id,
                        kind=SourceNodeKind.HEADING,
                        source_order=1,
                        raw_text="2 Native trusted chapter",
                        native_locator=native_locator,
                        parent_source_node_id=risk_heading_id,
                    ),
                )
            }
        )
        capture = SourceCaptureRecord(
            capture_id=self.capture.capture_id,
            project_id=self.capture.project_id,
            sor_document_id=self.capture.sor_document_id,
            source_sha256=self.capture.source_sha256,
            source_ir=source_ir,
            created_at_utc=self.capture.created_at_utc,
        )
        service = ReadingService(
            repository=SqlReadingRepository(),
            provider_factory=lambda: _FakeReadingProvider(),
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **_kwargs: capture,
            clock=lambda: _NOW,
        )
        context = service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )

        self.assertNotIn(
            "扫描伪章节",
            {item.title for item in context.overview.section_candidates},
        )
        self.assertNotIn(
            risk_heading_id,
            {item.source_node_id for item in context.structure_map.nodes},
        )
        self.assertEqual(len(context.structure_map.nodes), 1)
        self.assertIsNone(context.structure_map.nodes[0].parent_map_node_id)
        self.assertEqual(context.structure_map.status.value, "PARTIAL")
        self.assertIn(
            "VISUAL_STRUCTURE_NOT_PROJECTED",
            context.structure_map.reason_codes,
        )
        self.assertEqual(context.overview.coverage.status.value, "PARTIAL")
        self.assertIn(
            "VISUAL_STRUCTURE_NOT_PROJECTED",
            context.overview.coverage.reason_codes,
        )

    def test_reading_effective_source_is_partial_then_reprepared_and_visual_grounded(self) -> None:
        visual_provider = _FakeVisualProvider()
        visual_service = self._service(visual_provider)
        reading_provider = _FakeReadingProvider()
        reading_repository = SqlReadingRepository()
        reading_service = ReadingService(
            repository=reading_repository,
            provider_factory=lambda: reading_provider,
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            clock=lambda: _NOW,
        )
        context_before = reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        self.assertEqual(context_before.status.value, "PARTIAL")
        old_bundle = context_before.effective_bundle_id
        old_conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="旧捕获对话"),
        )
        with self.assertRaises(ApplicationError) as incomplete:
            reading_service.send_message(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                conversation_id=old_conversation.conversation_id,
                request=ReadingMessageCreateRequestV1(
                    source_sha256=self.document["source_sha256"],
                    content="概括整份文档",
                ),
            )
        self.assertEqual(incomplete.exception.code, "reading_capture_incomplete")
        self.assertEqual(reading_provider.chat_calls, [])

        native_node = next(
            node
            for node in self.capture.source_ir.nodes
            if node.native_locator.page_number == 2 and node.raw_text.strip()
        )
        native_ref = ReadingSourceRefV1(
            source_node_id=native_node.source_node_id,
            start_offset=0,
            end_offset=len(native_node.raw_text),
        )
        selected_turn = reading_service.send_message(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            conversation_id=old_conversation.conversation_id,
            request=ReadingMessageCreateRequestV1(
                source_sha256=self.document["source_sha256"],
                content="解释所选内容",
                source_ref=native_ref,
            ),
        )
        self.assertEqual(selected_turn.retrieval.coverage_mode, "SELECTED_CONTEXT")
        self.assertEqual(reading_provider.chat_calls[-1]["coverage_mode"], "SELECTED_CONTEXT")
        self.assertEqual(
            {chunk.page_number for chunk in reading_provider.chat_calls[-1]["context_chunks"]},
            {2},
        )

        visual_service.capture_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        with self.assertRaises(ApplicationError) as changed:
            reading_service.context(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
            )
        self.assertEqual(changed.exception.code, "reading_context_bundle_changed")
        context_after = reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        self.assertNotEqual(context_after.effective_bundle_id, old_bundle)
        self.assertIsNotNone(context_after.visual_generation_id)
        listed = reading_service.list_conversations(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
        )
        stale = next(
            item
            for item in listed.items
            if item.conversation_id == old_conversation.conversation_id
        )
        self.assertEqual(stale.status.value, "STALE")
        old_messages = reading_service.list_messages(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            conversation_id=old_conversation.conversation_id,
        )
        self.assertEqual(len(old_messages.items), 2)
        self.assertTrue(all(item.status.value == "STALE" for item in old_messages.items))
        before_stale_send = len(reading_provider.chat_calls)
        with self.assertRaises(ApplicationError) as stale_send:
            reading_service.send_message(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                conversation_id=old_conversation.conversation_id,
                request=ReadingMessageCreateRequestV1(
                    source_sha256=self.document["source_sha256"],
                    content="继续",
                ),
            )
        self.assertEqual(stale_send.exception.code, "reading_conversation_not_current")
        self.assertEqual(len(reading_provider.chat_calls), before_stale_send)

        internal_page = self.visual_repository.list_pages(
            context_after.visual_generation_id or ""
        )[0]
        run = internal_page.runs[0]
        visual_ref = ReadingSourceRefV1(
            kind="VISUAL_TEXT",
            visual_snapshot_id=internal_page.visual_snapshot_id,
            observation_id=run.observation_id,
            page_number=internal_page.page_number,
            bbox=run.bbox,
            page_bbox=internal_page.page_bbox,
            page_image_sha256=internal_page.page_image_sha256,
            provider_id=internal_page.provider_id,
            provider_version=internal_page.provider_version,
            start_offset=0,
            end_offset=len(run.text),
        )
        page = reading_service.source_page(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            page_number=1,
        )
        self.assertEqual(page.readable_mode, PageReadableMode.VISUAL)
        self.assertEqual(page.nodes, ())
        self.assertEqual(page.visual_runs, internal_page.runs)
        artifact = reading_service.create_action(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingActionRequestV1(
                action=ReadingAction.EXPLANATION,
                source_sha256=self.document["source_sha256"],
                source_ref=visual_ref,
            ),
        )
        self.assertEqual(reading_provider.action_calls[-1][1], run.text)
        self.assertEqual(artifact.source_ref, visual_ref)

        forged_ref = ReadingSourceRefV1.model_validate(
            visual_ref.model_copy(
                update={"page_image_sha256": "f" * 64}
            ).model_dump(mode="python")
        )
        action_call_count = len(reading_provider.action_calls)
        with self.assertRaises(ApplicationError) as forged:
            reading_service.create_action(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                request=ReadingActionRequestV1(
                    action=ReadingAction.EXPLANATION,
                    source_sha256=self.document["source_sha256"],
                    source_ref=forged_ref,
                ),
            )
        self.assertEqual(forged.exception.code, "reading_source_ref_invalid")
        self.assertEqual(len(reading_provider.action_calls), action_call_count)

        current_conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="当前捕获对话"),
        )
        visual_turn = reading_service.send_message(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            conversation_id=current_conversation.conversation_id,
            request=ReadingMessageCreateRequestV1(
                source_sha256=self.document["source_sha256"],
                content="解释视觉选区",
                source_ref=visual_ref,
            ),
        )
        self.assertEqual(visual_turn.retrieval.coverage_mode, "FULL_EFFECTIVE_TEXT")
        self.assertEqual(
            visual_turn.retrieval.strategy,
            "effective-source-full-effective-text/v1",
        )
        self.assertIn(FULL_EFFECTIVE_TEXT_DISCLOSURE, visual_turn.assistant_message.content)
        citation = visual_turn.assistant_message.citations[0]
        self.assertEqual(citation.locator_kind, "VISUAL_TEXT")
        self.assertEqual(citation.page_bbox, internal_page.page_bbox)
        self.assertEqual(citation.bbox, run.bbox)
        self.assertEqual(citation.page_image_sha256, internal_page.page_image_sha256)
        self.assertEqual(citation.provider_id, internal_page.provider_id)

    def test_visual_bundle_change_during_model_call_never_persists_late_turn(self) -> None:
        visual_service = self._service(_FakeVisualProvider())
        reading_provider = _BlockingReadingProvider()
        reading_repository = SqlReadingRepository()
        reading_service = ReadingService(
            repository=reading_repository,
            provider_factory=lambda: reading_provider,
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            clock=lambda: _NOW,
        )
        reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="并发视觉捕获"),
        )
        native_node = next(
            node
            for node in self.capture.source_ir.nodes
            if node.native_locator.page_number == 2 and node.raw_text.strip()
        )
        request = ReadingMessageCreateRequestV1(
            source_sha256=self.document["source_sha256"],
            content="解释所选内容",
            source_ref=ReadingSourceRefV1(
                source_node_id=native_node.source_node_id,
                start_offset=0,
                end_offset=len(native_node.raw_text),
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(
                reading_service.send_message,
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                conversation_id=conversation.conversation_id,
                request=request,
            )
            self.assertTrue(reading_provider.entered.wait(timeout=5))
            visual_service.capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )
            reading_provider.release.set()
            with self.assertRaises(ApplicationError) as late:
                future.result(timeout=5)
        self.assertEqual(late.exception.code, "reading_context_bundle_changed")
        self.assertEqual(
            reading_repository.list_messages(conversation.conversation_id),
            (),
        )

    def test_repository_bundle_fence_blocks_after_check_chat_artifact_and_note(self) -> None:
        repository = _BundleSwitchingReadingRepository()
        reading_provider = _FakeReadingProvider()
        reading_service = ReadingService(
            repository=repository,
            provider_factory=lambda: reading_provider,
            source_facts_reader=SqlSourceFactsReader(),
            visual_facts_reader=self.visual_repository,
            source_capture=lambda **kwargs: capture_current_document_source(
                **kwargs,
                repository=self.source_repository,
                object_store=self.object_store,
            ),
            clock=lambda: _NOW,
        )
        context = reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        native_node = next(
            node
            for node in self.capture.source_ir.nodes
            if node.native_locator.page_number == 2 and node.raw_text.strip()
        )
        native_ref = ReadingSourceRefV1(
            source_node_id=native_node.source_node_id,
            start_offset=0,
            end_offset=len(native_node.raw_text),
        )

        def switch_to(version: str):
            return lambda: self._service(
                _FakeVisualProvider(provider_version=version)
            ).capture_page(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                page_number=1,
            )

        repository.before_artifact = switch_to("bundle-fence/A")
        with self.assertRaises(ApplicationError) as artifact_failure:
            reading_service.create_action(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                request=ReadingActionRequestV1(
                    action=ReadingAction.EXPLANATION,
                    source_sha256=self.document["source_sha256"],
                    source_ref=native_ref,
                ),
            )
        self.assertEqual(
            artifact_failure.exception.code,
            "reading_context_bundle_changed",
        )
        self.assertEqual(repository.list_artifacts(context.reading_session_id), ())

        context = reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        repository.before_note = switch_to("bundle-fence/B")
        with self.assertRaises(ApplicationError) as note_failure:
            reading_service.create_note(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                request=ReadingNoteCreateRequestV1(
                    source_sha256=self.document["source_sha256"],
                    source_ref=native_ref,
                    content="不应落库",
                ),
            )
        self.assertEqual(
            note_failure.exception.code,
            "reading_context_bundle_changed",
        )
        self.assertEqual(repository.list_notes(context.reading_session_id), ())

        reading_service.prepare(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingPrepareRequestV1(
                expected_source_sha256=self.document["source_sha256"]
            ),
        )
        conversation = reading_service.create_conversation(
            project_id=self.project["id"],
            sor_document_id=self.document["id"],
            request=ReadingConversationCreateRequestV1(title="bundle fence"),
        )
        repository.before_chat = switch_to("bundle-fence/C")
        with self.assertRaises(ApplicationError) as chat_failure:
            reading_service.send_message(
                project_id=self.project["id"],
                sor_document_id=self.document["id"],
                conversation_id=conversation.conversation_id,
                request=ReadingMessageCreateRequestV1(
                    source_sha256=self.document["source_sha256"],
                    content="请继续",
                ),
            )
        self.assertEqual(chat_failure.exception.code, "reading_conversation_changed")
        self.assertEqual(repository.list_messages(conversation.conversation_id), ())

    def _write_mixed_pdf(self) -> Path:
        image = PILImage.new("RGB", (800, 1000), "white")
        image_bytes = BytesIO()
        image.save(image_bytes, format="PNG")
        path = self.root / "mixed.pdf"
        pdf = fitz.open()
        risk_page = pdf.new_page(width=400, height=500)
        risk_page.insert_image(risk_page.rect, stream=image_bytes.getvalue())
        native_page = pdf.new_page(width=400, height=500)
        native_page.insert_text((72, 90), "Native selectable source text")
        pdf.save(path)
        pdf.close()
        return path

    def _write_full_page_image_text_pdf(
        self,
        file_name: str,
        *,
        lines: tuple[str, ...],
    ) -> Path:
        image = PILImage.new("RGB", (800, 1000), "white")
        image_bytes = BytesIO()
        image.save(image_bytes, format="PNG")
        path = self.root / file_name
        pdf = fitz.open()
        page = pdf.new_page(width=400, height=500)
        page.insert_image(page.rect, stream=image_bytes.getvalue())
        for index, line in enumerate(lines):
            page.insert_text((40, 60 + index * 32), line, fontsize=11)
        pdf.save(path)
        pdf.close()
        return path

    def _add_document(
        self, path: Path, *, file_name: str | None = None
    ) -> dict:
        payload = path.read_bytes()
        return db.add_sor_document(
            self.project["id"],
            file_name or path.name,
            "PDF",
            str(path),
            "",
            source_sha256=hashlib.sha256(payload).hexdigest(),
            source_size=len(payload),
            declared_extension="pdf",
            detected_format="pdf",
            preflight_schema_version="document-preflight/v1",
        )


@unittest.skipUnless(
    os.getenv("REQPILOT_RUN_LOCAL_CORPUS_TESTS") == "1",
    "local customer corpus test is opt-in",
)
class LocalVisualCaptureCorpusTests(unittest.TestCase):
    def test_pef_and_qjly_risk_plans_are_stable_without_visual_calls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        pef = next(root.glob("runtime-data/objects/sor-documents/**/PRINT_21033420*.pdf"))
        qjly = next(root.glob("runtime-data/objects/sor-documents/**/*J7111029E*.pdf"))
        provider_calls = []
        visual_repository = _EmptyVisualRepository()
        expected = (
            (pef, tuple([1, 2, *range(6, 31)]), 30),
            (qjly, (), 137),
        )
        for source_path, expected_risks, expected_pages in expected:
            source_ir = build_source_ir(source_path)
            capture = SourceCaptureRecord(
                capture_id=f"capture-{source_ir.source_sha256[:16]}",
                project_id="project",
                sor_document_id=source_path.name,
                source_sha256=source_ir.source_sha256,
                source_ir=source_ir,
                created_at_utc=_NOW,
            )
            service = SourceVisualCaptureService(
                visual_repository=visual_repository,
                source_facts_reader=type(
                    "Reader",
                    (),
                    {"get_capture": lambda _self, **_kwargs: capture},
                )(),
                current_document=lambda **_kwargs: {
                    "source_sha256": capture.source_sha256
                },
                provider_factory=lambda: provider_calls.append("called"),
            )
            manifest = service.quality(
                project_id="project",
                sor_document_id=source_path.name,
            )
            actual_risks = tuple(
                page.page_number
                for page in manifest.pages
                if page.required_capability is not RequiredVisualCapability.NONE
            )
            self.assertEqual(manifest.page_count, expected_pages)
            self.assertEqual(actual_risks, expected_risks)
            if source_path == pef:
                self.assertEqual(
                    manifest.pages[17].required_capability,
                    RequiredVisualCapability.LAYOUT,
                )
        self.assertEqual(provider_calls, [])


if __name__ == "__main__":
    unittest.main()
